"""Stable import-purity enforcement, independent of candidate product code."""

from __future__ import annotations
import asyncio
import atexit
import builtins
import concurrent.futures
import contextlib
import io
import multiprocessing.process
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any


class CandidateImportError(RuntimeError):
    pass


@contextlib.contextmanager
def candidate_import_guard(*, error_type=CandidateImportError) -> Iterator[None]:
    """Reject import-time I/O and process-state mutation."""

    originals: list[tuple[Any, str, Any]] = []
    staging_thread = threading.get_ident()

    def patch(owner: Any, attribute: str, replacement: Any) -> None:
        if hasattr(owner, attribute):
            originals.append((owner, attribute, getattr(owner, attribute)))
            setattr(owner, attribute, replacement)

    def patch_blocked(owner: Any, attribute: str, label: str) -> None:
        if not hasattr(owner, attribute):
            return
        original = getattr(owner, attribute)

        def reject(*args: Any, **kwargs: Any) -> Any:
            if threading.get_ident() == staging_thread:
                raise error_type(
                    f"Function modules must be import-pure; blocked {label}"
                )
            return original(*args, **kwargs)

        patch(owner, attribute, reject)

    def is_null_sink(value: Any) -> bool:
        try:
            return Path(value).resolve() == Path(os.devnull).resolve()
        except (OSError, TypeError, ValueError):
            return False

    original_open = builtins.open

    def guarded_open(file: Any, mode: str = "r", *args: Any, **kwargs: Any):
        if (
            threading.get_ident() == staging_thread
            and any(flag in str(mode) for flag in ("w", "a", "x", "+"))
            and not is_null_sink(file)
        ):
            raise error_type(
                f"Function modules must be import-pure; blocked file write: {file}"
            )
        return original_open(file, mode, *args, **kwargs)

    original_io_open = io.open

    def guarded_io_open(file: Any, mode: str = "r", *args: Any, **kwargs: Any):
        if (
            threading.get_ident() == staging_thread
            and any(flag in str(mode) for flag in ("w", "a", "x", "+"))
            and not is_null_sink(file)
        ):
            raise error_type(
                f"Function modules must be import-pure; blocked file write: {file}"
            )
        return original_io_open(file, mode, *args, **kwargs)

    original_os_open = os.open

    def guarded_os_open(path: Any, flags: int, *args: Any, **kwargs: Any):
        write_flags = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
        if (
            threading.get_ident() == staging_thread
            and flags & write_flags
            and not is_null_sink(path)
        ):
            raise error_type(
                f"Function modules must be import-pure; blocked file write: {path}"
            )
        return original_os_open(path, flags, *args, **kwargs)

    original_popen = subprocess.Popen

    def guarded_popen(*args: Any, **kwargs: Any):
        command = args[0] if args else kwargs.get("args")
        normalized = (
            tuple(str(part) for part in command)
            if isinstance(command, (list, tuple))
            else ()
        )
        if normalized == ("/sbin/ldconfig", "-p"):
            return original_popen(*args, **kwargs)
        if threading.get_ident() == staging_thread:
            raise error_type(
                "Function modules must be import-pure; blocked process start"
            )
        return original_popen(*args, **kwargs)

    old_dont_write_bytecode = sys.dont_write_bytecode
    patch(builtins, "open", guarded_open)
    patch(io, "open", guarded_io_open)
    patch(os, "open", guarded_os_open)
    for attribute in ("write_text", "write_bytes", "touch", "mkdir", "unlink"):
        patch_blocked(Path, attribute, f"Path.{attribute}")
    for attribute in ("rename", "replace"):
        patch_blocked(Path, attribute, f"Path.{attribute}")
    for attribute in (
        "mkdir",
        "makedirs",
        "remove",
        "unlink",
        "rename",
        "replace",
        "rmdir",
        "removedirs",
        "chdir",
        "system",
        "fork",
        "forkpty",
        "posix_spawn",
        "posix_spawnp",
        "putenv",
        "unsetenv",
    ):
        patch_blocked(os, attribute, f"os.{attribute}")
    for attribute in ("copy", "copy2", "copyfile", "copytree", "move", "rmtree"):
        patch_blocked(shutil, attribute, f"shutil.{attribute}")
    patch(subprocess, "Popen", guarded_popen)
    for attribute in ("run", "call", "check_call", "check_output"):
        patch_blocked(subprocess, attribute, f"subprocess.{attribute}")
    patch_blocked(multiprocessing.process.BaseProcess, "start", "process start")
    patch_blocked(threading.Thread, "start", "thread start")
    patch_blocked(asyncio, "create_task", "asyncio.create_task")
    patch_blocked(asyncio.BaseEventLoop, "create_task", "event-loop task creation")
    patch_blocked(concurrent.futures.ThreadPoolExecutor, "submit", "thread work")
    patch_blocked(concurrent.futures.ProcessPoolExecutor, "submit", "process work")
    patch_blocked(socket, "create_connection", "network connection")
    patch_blocked(signal, "signal", "signal mutation")
    patch_blocked(atexit, "register", "exit handler registration")
    patch_blocked(os._Environ, "__setitem__", "environment mutation")
    patch_blocked(os._Environ, "__delitem__", "environment mutation")
    sys.dont_write_bytecode = True
    try:
        yield
    finally:
        sys.dont_write_bytecode = old_dont_write_bytecode
        for owner, attribute, original in reversed(originals):
            setattr(owner, attribute, original)
