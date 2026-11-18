"""Read-only inspection and authoritative-workspace verification commands."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import ExitStack
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from orchestrator.process_execution import (
    decode_process_output,
    process_group_kwargs,
    resolve_argv_invocation,
    terminate_windows_process_tree,
)
from tools.builtins import BuiltinExecutionResult

_IGNORED_COPY_DIRS = frozenset(
    {
        ".git",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".venv",
        ".aws",
        ".gnupg",
        ".ssh",
        "__pycache__",
        "backend_state",
        "logs",
        "media",
        "node_modules",
        "private",
        "state",
        "tmp",
        "venv",
        "workspaces",
    }
)
_MAX_OUTPUT_CHARS = 80_000
# Historical compatibility constant; it is not a snapshot or search ceiling.
_MAX_ARTIFACT_HASH_BYTES = 512 * 1024 * 1024
_MAX_PYTHON_SEARCH_FILE_BYTES = 32 * 1024 * 1024
_HASHI_VERIFICATION_POLICY_ARGUMENT = "_hashi_verification_policy"
_DEFAULT_DIRECT_TIMEOUT_S = 1800.0
_DEFAULT_MINIMUM_TIMEOUT_S = 300.0
_DEFAULT_EXECUTION_TIMEOUT_MULTIPLIER = 1.5
_DEFAULT_TIMEOUT_GRACE_S = 300.0
_MIN_EXECUTION_TIMEOUT_MULTIPLIER = 1.0
_MIN_TIMEOUT_GRACE_S = 60.0


def _result(output: str, **details: Any) -> BuiltinExecutionResult:
    return BuiltinExecutionResult(str(output), details)


def _workspace_path(workspace_dir: Path, raw_path: Any = ".") -> Path:
    root = Path(workspace_dir).resolve()
    candidate = Path(str(raw_path or "."))
    candidate = (
        (root / candidate).resolve()
        if not candidate.is_absolute()
        else candidate.resolve()
    )
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError(
            f"inspection path {candidate} is outside review workspace {root}"
        ) from exc
    return candidate


async def _run_read_only(
    argv: Sequence[str], *, cwd: Path, capture=None, capture_errors=None, capture_sizes=None,
    environment=None,
) -> subprocess.CompletedProcess[bytes]:
    """Run one inspection child with cancellable process-tree cleanup."""
    from tools.builtins import _bash_process_group_id, _cleanup_bash_process, _shield_bash_cleanup

    arguments = [str(item) for item in argv]
    process_options = process_group_kwargs()
    if environment is not None:
        process_options["env"] = environment
    proc = await asyncio.create_subprocess_exec(
        *arguments, cwd=str(cwd), stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        **process_options,
    )
    group = _bash_process_group_id(proc)
    if capture_sizes is None:
        task = asyncio.create_task(proc.communicate())
    else:
        from tools.tool_activity import bounded_communicate

        task = asyncio.create_task(bounded_communicate(
            proc, limit=_MAX_OUTPUT_CHARS * 2, sinks=capture,
            capture_errors=capture_errors, capture_sizes=capture_sizes,
        ))
    try:
        stdout, stderr = await asyncio.shield(task)
    except asyncio.CancelledError as exc:
        cleanup = await _shield_bash_cleanup(_cleanup_bash_process(proc, task, pgid=group))
        setattr(exc, "hashi_tool_details", {
            "foreground_cleanup": cleanup, "coverage_complete": False,
        })
        raise
    return subprocess.CompletedProcess(arguments, proc.returncode, stdout, stderr)


async def _inspect_command_output(
    argv: Sequence[str], *, cwd: Path, retained_output, success_codes: set[int],
    error_prefix: str, empty_text: str, environment=None,
) -> tuple[subprocess.CompletedProcess[bytes], str, dict[str, Any]]:
    """Keep fixed-command inspection previews small while retaining complete output."""
    with ExitStack() as files:
        capture = None
        errors: dict[str, str] = {}
        sizes: dict[str, int] = {}
        if retained_output is not None:
            try:
                capture = {
                    "stdout_bytes": files.enter_context(retained_output[0].temporary_capture_file()),
                    "stderr_bytes": files.enter_context(retained_output[0].temporary_capture_file()),
                }
            except OSError as exc:
                files.close()
                errors["capture"] = type(exc).__name__
        kwargs = {"environment": environment} if environment is not None else {}
        completed = await _run_read_only(
            argv, cwd=cwd, capture=capture, capture_errors=errors, capture_sizes=sizes, **kwargs,
        )
        success = completed.returncode in success_codes
        selected = "stdout_bytes" if success else "stderr_bytes"
        raw = completed.stdout if success else completed.stderr
        prefix = "" if success else error_prefix
        output = prefix + (_bounded_text(raw) or (empty_text if success else ""))
        large = sizes.get(selected, 0) > 20000
        details: dict[str, Any] = {}
        if large:
            reference = None
            line_count = None
            if capture is not None and not errors:
                from tools.builtins import _decoded_stream

                counter = {"lines": 0, "last": ""}

                def chunks():
                    if prefix:
                        yield prefix
                    for chunk in _decoded_stream(capture[selected], "utf-8"):
                        counter["lines"] += chunk.count("\n")
                        if chunk:
                            counter["last"] = chunk[-1]
                        yield chunk

                try:
                    reference = await asyncio.to_thread(
                        retained_output[0].retain_text_chunks, retained_output[1], retained_output[2], chunks(),
                    )
                    line_count = counter["lines"] + (1 if counter["last"] and counter["last"] != "\n" else 0)
                except (OSError, ValueError, TimeoutError):
                    reference = None
            details = {
                "complete_content": False,
                "output_continuation": reference,
                "output_retention": retained_output[0].retention if reference else "unavailable",
                "output_line_count": line_count,
            }
            low, high = 0, min(len(output), 12000)
            while low < high:
                middle = (low + high + 1) // 2
                if len(json.dumps(output[:middle], ensure_ascii=False)) <= 12000:
                    low = middle
                else:
                    high = middle - 1
            if reference:
                output = (output[:low] + f"\n[truncated by HASHI; use tool_output_read "
                          f"reference={reference} offset=0; retained while this Run is active]")
            else:
                output = (output[:low]
                          + "\n[truncated by HASHI; complete-result retention is unavailable for this Run]")
        return completed, output, details


def _inside_protected(path: Path, protected_paths: tuple[Path, ...]) -> bool:
    if not protected_paths:
        return False
    try:
        resolved = path.resolve()
        return any(resolved == item or resolved.is_relative_to(item) for item in protected_paths)
    except OSError:
        return True


def _has_protected_descendant(path: Path, protected_paths: tuple[Path, ...]) -> bool:
    try:
        resolved = path.resolve()
        return any(item == resolved or item.is_relative_to(resolved) for item in protected_paths)
    except OSError:
        return bool(protected_paths)


class _WorkspaceInspectionCancelled(Exception):
    pass


def _check_inspection_cancelled(cancelled: threading.Event | None) -> None:
    if cancelled is not None and cancelled.is_set():
        raise _WorkspaceInspectionCancelled()


def _sha256_file(path: Path, *, cancelled: threading.Event | None = None) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            _check_inspection_cancelled(cancelled)
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _null_delimited_names(stream, *, cancelled=None):
    stream.seek(0)
    pending = b""
    while chunk := stream.read(64 * 1024):
        _check_inspection_cancelled(cancelled)
        parts = (pending + chunk).split(b"\0")
        pending = parts.pop()
        yield from parts
    if pending:
        raise ValueError("Git returned an incomplete untracked-file inventory")


def _hash_git_untracked(
    digest, names, git_root: Path, root: Path, *, cancelled=None,
    protected_paths: tuple[Path, ...] = (),
) -> int:
    count = 0
    for raw_name in _null_delimited_names(names, cancelled=cancelled):
        _check_inspection_cancelled(cancelled)
        if not raw_name:
            continue
        relative = Path(raw_name.decode("utf-8", errors="surrogateescape"))
        path = (git_root / relative).resolve()
        if _inside_protected(path, protected_paths):
            continue
        try:
            path.relative_to(root)
        except ValueError:
            continue
        count += 1
        digest.update(b"untracked\0" + raw_name + b"\0")
        if path.is_file():
            before = path.stat()
            digest.update(_sha256_file(path, cancelled=cancelled).encode("ascii"))
            after = path.stat()
            if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
                after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns
            ):
                raise ValueError(f"file changed during snapshot: {relative}")
    return count


def _hash_git_captures(
    captures, git_root: Path, root: Path, *, cancelled=None,
    protected_paths: tuple[Path, ...] = (),
):
    digest = hashlib.sha256()
    for label in ("head", "status", "unstaged", "staged"):
        _check_inspection_cancelled(cancelled)
        digest.update(label.encode("ascii") + b"\0")
        stream = captures[label]
        stream.seek(0)
        while chunk := stream.read(1024 * 1024):
            _check_inspection_cancelled(cancelled)
            digest.update(chunk)
        digest.update(b"\0")
    count = _hash_git_untracked(
        digest, captures["untracked"], git_root, root,
        cancelled=cancelled, protected_paths=protected_paths,
    )
    return digest.hexdigest(), count


async def _git_snapshot(
    root: Path, *, protected_paths: tuple[Path, ...] = (), capture_factory=None,
) -> tuple[str, dict[str, Any]] | None:
    try:
        top = await _run_read_only(["git", "rev-parse", "--show-toplevel"], cwd=root)
    except FileNotFoundError:
        return None
    if top.returncode != 0:
        return None
    git_root = Path(top.stdout.decode("utf-8", errors="replace").strip()).resolve()
    try:
        relative_root = root.relative_to(git_root)
    except ValueError:
        return None

    relative_root_text = relative_root.as_posix()
    if relative_root_text != ".":
        ignored = await _run_read_only(
            ["git", "check-ignore", "--quiet", "--", relative_root_text],
            cwd=git_root,
        )
        if ignored.returncode != 1:
            # An ignored workspace cannot be represented by Git status/diff.
            return None

    excluded = list(dict.fromkeys(
        path for path in protected_paths if path.is_relative_to(root) and path != root
    ))
    excluded_specs = [":(exclude,literal)" + path.relative_to(git_root).as_posix()
                      for path in excluded]
    pathspec = (["--", relative_root_text, *excluded_specs]
                if relative_root_text != "." or excluded_specs else [])
    commands = {
        "head": ["git", "rev-parse", "HEAD"],
        "status": ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all", *pathspec],
        "unstaged": ["git", "diff", "--no-ext-diff", "--binary", *pathspec],
        "staged": ["git", "diff", "--cached", "--no-ext-diff", "--binary", *pathspec],
        "untracked": ["git", "ls-files", "--others", "--exclude-standard", "-z", *pathspec],
    }
    make_capture = capture_factory or (lambda: tempfile.TemporaryFile(mode="w+b"))
    with ExitStack() as files:
        captures = {}
        results = {}
        sizes = {}
        try:
            for name, argv in commands.items():
                captures[name] = files.enter_context(make_capture())
                errors: dict[str, str] = {}
                observed: dict[str, int] = {}
                result = await _run_read_only(
                    argv, cwd=git_root, capture={"stdout_bytes": captures[name]},
                    capture_errors=errors, capture_sizes=observed,
                )
                if result.returncode != 0 or errors:
                    return None
                results[name] = result
                sizes[name] = observed.get("stdout_bytes", 0)
        except OSError:
            return None

        snapshot, untracked_count = await _run_inspection_thread(
            _hash_git_captures, captures, git_root, root, protected_paths=protected_paths,
        )
        return snapshot, {
            "vcs": "git",
            "head": results["head"].stdout.decode("utf-8", errors="replace").strip(),
            "dirty": sizes["status"] > 0,
            "untracked_files": untracked_count,
            "coverage_complete": False,
            "coverage_scope": "git_tracked_and_nonignored",
            "coverage_limitations": (["git_ignored_paths", "external_symlink_targets"]
                                     + (["reported_excluded_paths"] if excluded else [])),
            "excluded_count": len(excluded),
            "excluded_paths": [path.relative_to(root).as_posix() for path in excluded],
            "excluded_paths_truncated": False,
        }


def _filesystem_snapshot(
    root: Path, *, cancelled: threading.Event | None = None, collect_all: bool = False,
    protected_paths: tuple[Path, ...] = (),
) -> tuple[str, dict[str, Any]]:
    if not root.is_dir():
        raise ValueError("workspace snapshot root is not a directory")
    digest = hashlib.sha256()
    file_count = 0
    byte_count = 0
    excluded_count = 0
    excluded_paths: list[str] = []

    def excluded(path):
        nonlocal excluded_count
        excluded_count += 1
        if collect_all or len(excluded_paths) < 20:
            excluded_paths.append(path.relative_to(root).as_posix())

    def walk_error(error):
        raise error

    for current, dirs, files in os.walk(root, followlinks=False, onerror=walk_error):
        _check_inspection_cancelled(cancelled)
        current_path = Path(current)
        descend = []
        for name in sorted(dirs):
            path = current_path / name
            if name in _IGNORED_COPY_DIRS or _inside_protected(path, protected_paths):
                excluded(path)
            elif path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
                relative = path.relative_to(root).as_posix()
                digest.update(os.fsencode(f"link\0{relative}\0{os.readlink(path)}\0"))
            else:
                descend.append(name)
        dirs[:] = descend
        for name in sorted(files):
            _check_inspection_cancelled(cancelled)
            path = current_path / name
            if _inside_protected(path, protected_paths):
                excluded(path)
                continue
            relative = path.relative_to(root).as_posix()
            entry = path.lstat()
            if stat.S_ISLNK(entry.st_mode):
                digest.update(os.fsencode(f"link\0{relative}\0{os.readlink(path)}\0"))
                continue
            if not stat.S_ISREG(entry.st_mode):
                excluded(path)
                continue
            before = path.stat()
            digest.update(os.fsencode(f"file\0{relative}\0{before.st_size}\0"))
            digest.update(_sha256_file(path, cancelled=cancelled).encode("ascii"))
            after = path.stat()
            if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
                after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns
            ):
                raise ValueError(f"file changed during snapshot: {relative}")
            file_count += 1
            byte_count += before.st_size
    return digest.hexdigest(), {
        "vcs": "filesystem",
        "dirty": None,
        "file_count": file_count,
        "bytes_hashed": byte_count,
        "coverage_complete": excluded_count == 0,
        "coverage_scope": "filesystem_without_reported_exclusions",
        "excluded_count": excluded_count,
        "excluded_paths": excluded_paths,
        "excluded_paths_truncated": excluded_count > len(excluded_paths),
        "symlink_targets_followed": False,
    }


async def _run_inspection_thread(function, *args, **kwargs):
    cancelled = threading.Event()
    task = asyncio.create_task(asyncio.to_thread(
        function, *args, cancelled=cancelled, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError as exc:
        cancelled.set()
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=5)
        except (Exception, asyncio.CancelledError):
            pass
        setattr(exc, "hashi_tool_details", {
            "foreground_cleanup": {"status": "stopped" if task.done() else "unverified"},
            "coverage_complete": False,
        })
        raise


async def _run_workspace_snapshot(
    root: Path, *, filesystem_only: bool = False, collect_all: bool = False,
    protected_paths: tuple[Path, ...] = (), capture_factory=None,
):
    if not filesystem_only:
        git_value = await _git_snapshot(
            root, protected_paths=protected_paths, capture_factory=capture_factory,
        )
        if git_value is not None:
            return git_value
    return await _run_inspection_thread(
        _filesystem_snapshot, root, collect_all=collect_all, protected_paths=protected_paths,
    )


def _bounded_text(raw: bytes) -> str:
    text = decode_process_output(raw).replace("\r\n", "\n").replace("\r", "\n")
    if len(text) > _MAX_OUTPUT_CHARS:
        return text[:_MAX_OUTPUT_CHARS] + "\n...[truncated]"
    return text


class _WorkspaceSearchCancelled(Exception):
    pass


def _python_search_in_memory_limit() -> int | None:
    """Bound the fallback's whole-file reads; zero opts out of the memory budget."""
    raw = os.environ.get("HASHI_WORKSPACE_SEARCH_MEMORY_BYTES")
    if raw is None:
        return _MAX_PYTHON_SEARCH_FILE_BYTES
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError("HASHI_WORKSPACE_SEARCH_MEMORY_BYTES must be a nonnegative integer") from exc
    if value < 0:
        raise ValueError("HASHI_WORKSPACE_SEARCH_MEMORY_BYTES must be a nonnegative integer")
    return value or None


def _python_workspace_search(
    target: Path,
    *,
    query: str,
    regex: bool,
    cancelled: threading.Event | None = None,
    collect_all: bool = False,
    output_sink=None,
    omission_sink=None,
    protected_paths: tuple[Path, ...] = (),
) -> tuple[str, int, dict[str, Any]]:
    """Portable search with streaming for large literal files and truthful gaps."""

    from tools.literal_search import scan_literal

    matcher = re.compile(query).search if regex else lambda line: query in line
    memory_limit = _python_search_in_memory_limit()
    if target.is_file():
        paths = iter((target,))
        include_path = False
        root = target.parent
    elif target.is_dir():
        root = target
        include_path = True
        def iter_paths():
            for current, dirs, files in os.walk(target, followlinks=False, onerror=walk_error):
                current_path = Path(current)
                retained = []
                for name in sorted(dirs):
                    path = current_path / name
                    if _inside_protected(path, protected_paths):
                        omit(path, "protected_read_scope")
                    elif name in _IGNORED_COPY_DIRS:
                        omit(path, "directory_excluded")
                    elif path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
                        omit(path, "symlink_not_followed")
                    else:
                        retained.append(name)
                dirs[:] = retained
                for name in sorted(files):
                    yield current_path / name
        paths = iter_paths()
    else:
        return "", 0, {"coverage_complete": False, "omitted_count": 1,
                       "omitted": [{"path": str(target), "reason": "path_unavailable"}],
                       "omitted_paths_truncated": False, "large_files_scanned": 0,
                       "excerpt_count": 0, "output_limited": False}

    rows: list[str] = []
    output_chars = 0
    omitted = []
    omitted_count = 0
    large_file_count = 0
    excerpt_count = 0
    output_limited = False
    matched_count = 0
    capture_failed = False
    omission_capture_failed = False

    def check_cancel(*_args):
        if cancelled is not None and cancelled.is_set():
            raise _WorkspaceSearchCancelled()

    def omit(path, reason):
        nonlocal omitted_count, omission_sink, omission_capture_failed
        omitted_count += 1
        item = {"path": str(path), "reason": reason}
        if omission_sink is not None:
            try:
                encoded = json.dumps(item, ensure_ascii=True, separators=(",", ":")).encode("ascii")
                omission_sink.write(b"O" + encoded + b"\n")
            except OSError:
                omission_capture_failed = True
                omission_sink = None
        if collect_all or len(omitted) < 20:
            omitted.append(item)

    def walk_error(exc):
        omit(getattr(exc, "filename", target), type(exc).__name__)

    def add(row):
        nonlocal output_chars, output_limited, output_sink, capture_failed, matched_count
        if output_sink is not None:
            try:
                output_sink.write((row + "\n").encode("utf-8"))
            except OSError:
                capture_failed = True
                output_sink = None
        if output_chars + len(row) + 1 > _MAX_OUTPUT_CHARS:
            output_limited = True
            if output_sink is None:
                return False
            matched_count += 1
            return True
        rows.append(row)
        output_chars += len(row) + 1
        matched_count += 1
        return True

    for path in paths:
        check_cancel()
        try:
            if _inside_protected(path, protected_paths):
                omit(path, "protected_read_scope")
                continue
            entry = path.lstat()
            if stat.S_ISLNK(entry.st_mode):
                omit(path, "symlink_not_followed")
                continue
            if not stat.S_ISREG(entry.st_mode):
                omit(path, "non_regular_file")
                continue
            before = path.stat()
            with path.open("rb") as probe:
                binary = b"\0" in probe.read(8192)
            if binary:
                continue
            prefix = f"{path.relative_to(root).as_posix()}:" if include_path else ""
            if memory_limit is not None and before.st_size > memory_limit:
                if regex:
                    omit(path, "large_regex_requires_file_search")
                    continue
                large_file_count += 1
                # The old line-oriented search never matched a literal newline.
                if "\n" in query or "\r" in query:
                    continue
                state = None
                previous_line = None
                encoding_warned = False
                while True:
                    check_cancel()
                    page = scan_literal(
                        path, terms=(query,), case_sensitive=True, max_results=100,
                        context_chars=160, state=state, character_budget=8 * 1024 * 1024,
                        progress=check_cancel,
                    )
                    for match in page["matches"]:
                        if match["line"] == previous_line:
                            continue
                        previous_line = match["line"]
                        excerpt = match["excerpt"][:400].replace("\r", "\\r").replace("\n", "\\n")
                        if not add(f"{prefix}{match['line']}:[excerpt] {excerpt}"):
                            break
                        excerpt_count += 1
                    if page["encoding_warning"] and not encoding_warned:
                        omit(path, "invalid_utf8_replaced")
                        encoding_warned = True
                    state = page["continuation"]
                    if (output_limited and output_sink is None) or state is None:
                        break
            else:
                raw = path.read_bytes()
                try:
                    text = raw.decode("utf-8", errors="strict")
                except UnicodeDecodeError:
                    text = raw.decode("utf-8", errors="replace")
                    omit(path, "invalid_utf8_replaced")
                for line_number, line in enumerate(text.splitlines(), start=1):
                    if line_number % 1024 == 0:
                        check_cancel()
                    if matcher(line) and not add(f"{prefix}{line_number}:{line}"):
                        break
            after = path.stat()
            if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
                after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns
            ):
                omit(path, "file_changed_during_search")
        except OSError as exc:
            omit(path, type(exc).__name__)
        if output_limited and output_sink is None:
            break

    coverage = {
        "coverage_complete": not omitted_count and not (output_limited and output_sink is None),
        "omitted_count": omitted_count,
        "omitted": omitted,
        "omitted_paths_truncated": omitted_count > len(omitted),
        "large_files_scanned": large_file_count,
        "excerpt_count": excerpt_count,
        "output_limited": output_limited,
        "capture_failed": capture_failed,
        "omission_capture_failed": omission_capture_failed,
    }
    if output_limited and output_sink is None:
        rows.append("...[truncated; use file_search for paged results]")
    return ("\n".join(rows) + "\n" if rows else ""), matched_count, coverage


async def _run_python_workspace_regex(
    target: Path, *, query: str, collect_all: bool, output_sink=None, omission_sink=None,
    protected_paths: tuple[Path, ...] = (),
):
    from tools.builtins import _bash_process_group_id, _cleanup_bash_process, _shield_bash_cleanup

    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-B", str(Path(__file__).with_name("workspace_search_process.py")),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, **process_group_kwargs(),
    )
    group = _bash_process_group_id(proc)
    request = json.dumps({"path": str(target), "query": query, "collect_all": collect_all,
                          "stream_results": output_sink is not None,
                          "stream_omissions": omission_sink is not None,
                          "protected_paths": [str(path) for path in protected_paths]},
                         ensure_ascii=True).encode("utf-8")
    capture_errors: dict[str, str] = {}
    if output_sink is None:
        task = asyncio.create_task(proc.communicate(request))
    else:
        from tools.tool_activity import bounded_communicate

        async def run_streaming():
            assert proc.stdin is not None
            proc.stdin.write(request)
            await proc.stdin.drain()
            proc.stdin.close()
            sinks = {"stderr_bytes": output_sink}
            if omission_sink is not None:
                sinks["stdout_bytes"] = omission_sink
            return await bounded_communicate(
                proc, limit=_MAX_OUTPUT_CHARS * 4, sinks=sinks, capture_errors=capture_errors,
            )

        task = asyncio.create_task(run_streaming())
    try:
        stdout, stderr = await asyncio.shield(task)
    except asyncio.CancelledError as exc:
        cleanup = await _shield_bash_cleanup(_cleanup_bash_process(proc, task, pgid=group))
        setattr(exc, "hashi_tool_details", {
            "foreground_cleanup": cleanup, "coverage_complete": False,
        })
        raise
    if proc.returncode != 0:
        raise ValueError("workspace regex worker failed: " + stderr.decode("utf-8", "replace")[-1000:])
    observed_omissions = None
    try:
        if omission_sink is not None and capture_errors.get("stdout_bytes"):
            # The bounded pipe tail can still carry the final summary even if
            # the separate complete omission inventory could not be spooled.
            summary = next((line for line in reversed(stdout.splitlines()) if line.startswith(b"R")), None)
            if summary is None:
                raise ValueError("workspace regex coverage summary is unavailable")
            response = json.loads(summary[1:])
        elif omission_sink is not None:
            omission_sink.seek(0)
            response = None
            observed_omissions = 0
            for line in omission_sink:
                if line.startswith(b"O") and response is None:
                    item = json.loads(line[1:])
                    if not isinstance(item, dict) or set(item) != {"path", "reason"}:
                        raise ValueError("workspace regex coverage record is invalid")
                    observed_omissions += 1
                elif line.startswith(b"R") and response is None:
                    response = json.loads(line[1:])
                else:
                    raise ValueError("workspace regex coverage event is invalid")
        else:
            response = json.loads(stdout)
    except (ValueError, UnicodeError) as exc:
        raise ValueError("workspace regex worker returned invalid data") from exc
    if not isinstance(response, dict):
        raise ValueError("workspace regex worker returned invalid data")
    if response.get("error") == "invalid_regex":
        raise re.error(str(response.get("message") or "invalid regex"))
    if response.get("error"):
        raise ValueError("workspace regex worker failed: " + str(response.get("message") or "unknown error"))
    coverage = response.get("coverage")
    if (not isinstance(response.get("output"), str) or type(response.get("matches")) is not int
            or response["matches"] < 0 or not isinstance(coverage, dict)
            or type(coverage.get("coverage_complete")) is not bool
            or not isinstance(coverage.get("omitted"), list)
            or any(type(coverage.get(key)) is not int or coverage[key] < 0
                   for key in ("omitted_count", "large_files_scanned", "excerpt_count"))
            or type(coverage.get("output_limited")) is not bool):
        raise ValueError("workspace regex worker returned invalid data")
    if capture_errors.get("stderr_bytes"):
        coverage["capture_failed"] = True
        if coverage["output_limited"]:
            coverage["coverage_complete"] = False
    if (capture_errors.get("stdout_bytes") or
            (observed_omissions is not None and observed_omissions != coverage["omitted_count"])):
        coverage["omission_capture_failed"] = True
    return response["output"], response["matches"], coverage


async def _run_python_workspace_search(
    target: Path, *, query: str, regex: bool, collect_all: bool = False, output_sink=None,
    omission_sink=None, protected_paths: tuple[Path, ...] = (),
):
    if regex:
        return await _run_python_workspace_regex(
            target, query=query, collect_all=collect_all, output_sink=output_sink,
            omission_sink=omission_sink, protected_paths=protected_paths,
        )
    cancelled = threading.Event()
    task = asyncio.create_task(asyncio.to_thread(
        _python_workspace_search, target, query=query, regex=regex, cancelled=cancelled,
        collect_all=collect_all, output_sink=output_sink, omission_sink=omission_sink,
        protected_paths=protected_paths,
    ))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError as exc:
        cancelled.set()
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=5)
        except (Exception, asyncio.CancelledError):
            pass
        setattr(exc, "hashi_tool_details", {
            "foreground_cleanup": {"status": "stopped" if task.done() else "unverified"},
            "coverage_complete": False,
        })
        raise


def _page_coverage_details(
    metadata: dict[str, Any], *, key: str, truncated_key: str, retain_coverage,
) -> None:
    details = metadata.get(key)
    if not isinstance(details, list) or len(details) <= 20:
        return
    encoded = json.dumps({"version": 1, key: details}, ensure_ascii=True, separators=(",", ":"))
    try:
        reference = retain_coverage(encoded) if retain_coverage is not None else None
    except Exception:
        reference = None
    metadata[key] = details[:20]
    metadata[truncated_key] = True
    if reference:
        metadata[key + "_reference"] = {
            "reference": reference, "read_tool": "tool_output_read",
            "sha256": hashlib.sha256(encoded.encode("utf-8")).hexdigest(), "format": "json",
        }
    else:
        metadata[key + "_reference_unavailable"] = True


async def _retain_streamed_omissions(coverage, omission_capture, retained_output) -> None:
    """Retain a large omission inventory without assembling it in memory."""
    if coverage["omitted_count"] <= 20:
        return
    if omission_capture is None or retained_output is None or coverage.get("omission_capture_failed"):
        coverage["omitted_reference_unavailable"] = True
        return

    def records():
        omission_capture.seek(0)
        finished = False
        for raw in omission_capture:
            if raw.startswith(b"O") and not finished:
                item = json.loads(raw[1:])
                if not isinstance(item, dict) or set(item) != {"path", "reason"}:
                    raise ValueError("invalid omission record")
                yield raw[1:].rstrip(b"\r\n").decode("ascii")
            elif raw.startswith(b"R") and not finished:
                finished = True
            else:
                raise ValueError("invalid omission event")

    try:
        if sum(1 for _ in records()) != coverage["omitted_count"]:
            raise ValueError("incomplete omission inventory")
        digest = hashlib.sha256()

        def chunks():
            def record(value):
                digest.update(value.encode("utf-8"))
                return value

            yield record('{"version":1,"omitted":[')
            first = True
            for item in records():
                if not first:
                    yield record(",")
                yield record(item)
                first = False
            yield record("]}")

        reference = await asyncio.to_thread(
            retained_output[0].retain_text_chunks, retained_output[1], retained_output[2], chunks(),
        )
    except (OSError, ValueError, TimeoutError, UnicodeError):
        reference = None
    if reference:
        coverage["omitted_reference"] = {
            "reference": reference, "read_tool": "tool_output_read",
            "sha256": digest.hexdigest(), "format": "json",
        }
    else:
        coverage["omitted_reference_unavailable"] = True


async def _inspect_python_search(
    target: Path, *, query: str, regex: bool, retain_coverage, retained_output,
    protected_paths: tuple[Path, ...] = (),
) -> BuiltinExecutionResult:
    """Use private Run-bound spooling for the portable workspace-search fallback."""
    with ExitStack() as files:
        capture = None
        omission_capture = None
        if retained_output is not None:
            try:
                capture = files.enter_context(retained_output[0].temporary_capture_file())
                omission_capture = files.enter_context(retained_output[0].temporary_capture_file())
            except OSError:
                files.close()
                capture = None
                omission_capture = None
        try:
            output, matches, coverage = await _run_python_workspace_search(
                target, query=query, regex=regex,
                collect_all=retain_coverage is not None and retained_output is None,
                output_sink=capture, omission_sink=omission_capture, protected_paths=protected_paths,
            )
        except re.error as exc:
            return _result(f"Error: workspace search failed: invalid regex: {exc}",
                           operation="search", exit_code=2, search_backend="python")
        except (OSError, ValueError) as exc:
            return _result(f"Error: workspace search failed: {exc}",
                           operation="search", exit_code=2, search_backend="python")

        if retained_output is not None:
            await _retain_streamed_omissions(coverage, omission_capture, retained_output)
        else:
            _page_coverage_details(coverage, key="omitted", truncated_key="omitted_paths_truncated",
                                   retain_coverage=retain_coverage)
        reference = None
        if coverage["output_limited"] and capture is not None and not coverage.get("capture_failed"):
            from tools.builtins import _decoded_stream

            try:
                reference = await asyncio.to_thread(
                    retained_output[0].retain_text_chunks, retained_output[1], retained_output[2],
                    _decoded_stream(capture, "utf-8"),
                )
            except (OSError, ValueError, TimeoutError):
                reference = None
        if coverage["output_limited"] and not reference:
            coverage["coverage_complete"] = False

        extra: dict[str, Any] = {}
        if not coverage["coverage_complete"] or coverage["large_files_scanned"] or coverage["output_limited"]:
            extra = {**coverage, "complete_content": coverage["coverage_complete"]
                     and not coverage["excerpt_count"] and not coverage["output_limited"]}
        if not coverage["coverage_complete"]:
            extra["recovery"] = "Use file_search for paged results or large regex files"
        empty = ("No matches." if coverage["coverage_complete"] else
                 "No matches in inspected files; search coverage is incomplete.")
        rendered = output or empty
        if coverage["output_limited"]:
            low, high = 0, min(len(rendered), 12000)
            while low < high:
                middle = (low + high + 1) // 2
                if len(json.dumps(rendered[:middle], ensure_ascii=False)) <= 12000:
                    low = middle
                else:
                    high = middle - 1
            if reference:
                rendered = (rendered[:low] + f"\n[truncated by HASHI; use tool_output_read "
                            f"reference={reference} offset=0; retained while this Run is active]")
                extra.update(output_continuation=reference, output_retention=retained_output[0].retention)
            else:
                rendered = (rendered[:low] + "\n[search incomplete; complete-result retention is unavailable; "
                            "use file_search for recovery]\n")
                extra.update(output_continuation=None, output_retention="unavailable")
        elif not coverage["coverage_complete"] and output:
            rendered += "[search incomplete; use file_search for recovery]\n"
        if coverage.get("omitted_reference"):
            omitted_reference = coverage["omitted_reference"]["reference"]
            rendered += f"\n[Complete omitted paths: tool_output_read reference={omitted_reference} offset=0]"
        return _result(
            rendered, operation="search",
            exit_code=(2 if not coverage["coverage_complete"] else 0 if matches else 1),
            matches=matches, search_backend="python", **extra,
        )


async def execute_workspace_inspect(
    arguments: Mapping[str, Any],
    *,
    workspace_dir: Path,
    retain_coverage=None,
    retained_output=None,
    protected_read_paths=(),
    snapshot_excluded_paths=(),
) -> BuiltinExecutionResult:
    """Perform one fixed read-only inspection operation inside the workzone."""

    operation = str(arguments.get("operation") or "").strip().casefold()
    root = Path(workspace_dir).resolve()
    protected_paths = tuple(Path(path).resolve() for path in protected_read_paths)
    if _inside_protected(root, protected_paths):
        return _result("Error: workspace inspection targets protected data.", operation=operation)
    if operation == "snapshot":
        try:
            snapshot_exclusions = protected_paths + tuple(
                Path(path).resolve() for path in snapshot_excluded_paths
            )
            capture_factory = retained_output[0].temporary_capture_file if retained_output is not None else None
            digest, metadata = await _run_workspace_snapshot(
                root, collect_all=retain_coverage is not None, protected_paths=snapshot_exclusions,
                capture_factory=capture_factory,
            )
        except Exception as exc:
            return _result(
                f"Error: workspace snapshot unavailable: {exc}",
                operation=operation,
                unavailable=True,
            )
        _page_coverage_details(
            metadata, key="excluded_paths", truncated_key="excluded_paths_truncated",
            retain_coverage=retain_coverage)
        payload = {"snapshot_sha256": digest, **metadata}
        return _result(
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
            operation=operation,
            snapshot_sha256=digest,
            **metadata,
        )

    try:
        target = _workspace_path(root, arguments.get("path", "."))
    except ValueError as exc:
        return _result(f"Error: {exc}", operation=operation)
    if _inside_protected(target, protected_paths):
        return _result("Error: workspace inspection targets protected data.", operation=operation)
    excluded_pathspecs = [
        ":(exclude,literal)" + item.relative_to(root).as_posix()
        for item in protected_paths if item.is_relative_to(root) and item != root
    ]

    if operation == "status":
        completed, output, details = await _inspect_command_output(
            ["git", "status", "--short", "--branch", "--", str(target), *excluded_pathspecs],
            cwd=root, retained_output=retained_output, success_codes={0},
            error_prefix="Error: workspace status is unavailable: ", empty_text="Workspace status is clean.",
        )
        return _result(output, operation=operation, exit_code=completed.returncode, **details)

    if operation == "diff":
        cached = arguments.get("cached", False)
        if not isinstance(cached, bool):
            return _result("Error: cached must be a boolean", operation=operation)
        argv = ["git", "diff", "--no-ext-diff", "--stat", "--patch"]
        if cached:
            argv.append("--cached")
        argv.extend(["--", str(target), *excluded_pathspecs])
        completed, output, details = await _inspect_command_output(
            argv, cwd=root, retained_output=retained_output, success_codes={0},
            error_prefix="Error: workspace diff is unavailable: ", empty_text="No diff.",
        )
        return _result(output, operation=operation, exit_code=completed.returncode, cached=cached, **details)

    if operation == "search":
        query = str(arguments.get("query") or "")
        if not query:
            return _result("Error: search requires query", operation=operation)
        regex = arguments.get("regex", False)
        if not isinstance(regex, bool):
            return _result("Error: regex must be a boolean", operation=operation)
        search_backend = "rg"
        force_scoped_search = _has_protected_descendant(target, protected_paths)
        executable = None if force_scoped_search else shutil.which("rg")
        if executable:
            argv = [
                executable,
                "--line-number",
                "--no-heading",
                "--color",
                "never",
            ]
            if not regex:
                argv.append("--fixed-strings")
        else:
            search_backend = "grep"
            executable = None if force_scoped_search else shutil.which("grep")
            if not executable:
                return await _inspect_python_search(
                    target, query=query, regex=regex, retain_coverage=retain_coverage,
                    retained_output=retained_output, protected_paths=protected_paths,
                )
            argv = [
                executable,
                "--recursive",
                "--line-number",
                "--binary-files=without-match",
                "--extended-regexp" if regex else "--fixed-strings",
            ]
        argv.extend(["--", query, str(target)])
        completed, output, details = await _inspect_command_output(
            argv, cwd=root, retained_output=retained_output, success_codes={0, 1},
            error_prefix="Error: workspace search failed: ", empty_text="No matches.",
        )
        if completed.returncode not in {0, 1}:
            return _result(output, operation=operation, exit_code=completed.returncode, **details)
        matches = (0 if completed.returncode == 1 else
                   details.get("output_line_count") if details.get("complete_content") is False
                   else len(output.splitlines()))
        return _result(output, operation=operation, exit_code=completed.returncode,
                       matches=matches, search_backend=search_backend, **details)

    if operation in {"hash", "artifact"}:
        if not target.exists():
            return _result(f"Error: path not found: {target}", operation=operation)
        if target.is_file():
            digest = await _run_inspection_thread(_sha256_file, target)
            stat = target.stat()
            payload = {
                "path": str(target.relative_to(root)),
                "kind": "file",
                "size": stat.st_size,
                "sha256": digest,
            }
        elif target.is_dir():
            digest, metadata = await _run_workspace_snapshot(
                target, filesystem_only=True, collect_all=retain_coverage is not None,
                protected_paths=protected_paths)
            _page_coverage_details(
                metadata, key="excluded_paths", truncated_key="excluded_paths_truncated",
                retain_coverage=retain_coverage)
            payload = {
                "path": str(target.relative_to(root)),
                "kind": "directory",
                "sha256": digest,
                **metadata,
            }
        else:
            return _result(
                f"Error: unsupported artifact type: {target}", operation=operation
            )
        return _result(
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
            operation=operation,
            artifact_sha256=digest,
            path=payload["path"],
        )

    return _result(
        "Error: operation must be snapshot, status, diff, search, hash, or artifact",
        operation=operation,
    )


def _recipe_catalog(options: Mapping[str, Any] | None) -> dict[str, dict[str, Any]]:
    # Keep the virtual-environment entry point. Resolving the symlink would
    # silently bypass the environment's installed packages inside bwrap.
    python = str(Path(sys.executable))
    recipes: dict[str, dict[str, Any]] = {
        "pytest_core": {
            "description": "Curated deterministic pytest core gate.",
            "argv": [python, "-B", "-m", "pytest", "-q"],
            "timeout_s": 1800.0,
        },
        "pytest_offline": {
            "description": (
                "All offline pytest inventory excluding contract, live, platform, "
                "and deliberate real-wall-clock tests."
            ),
            "argv": [
                python,
                "-B",
                "-m",
                "pytest",
                "-q",
                "tests",
                "-m",
                "not contract and not live and not platform and not real_wall_clock",
            ],
            "timeout_s": 3600.0,
        },
        "python_compile": {
            "description": "Parse workspace Python sources without writing bytecode.",
            "argv": [
                python,
                "-B",
                "-c",
                (
                    "from pathlib import Path;"
                    "skip={'.git','.venv','.venv-wsl','node_modules','__pycache__'};"
                    "files=[p for p in Path('.').rglob('*.py') "
                    "if not skip.intersection(p.parts)];"
                    "[compile(p.read_bytes(),str(p),'exec') for p in files];"
                    "print(f'PYTHON_SYNTAX_OK {len(files)}')"
                ),
            ],
            "timeout_s": 900.0,
        },
    }
    raw_recipes = (options or {}).get("recipes")
    if not isinstance(raw_recipes, Mapping):
        return recipes
    for raw_name, raw in raw_recipes.items():
        name = str(raw_name or "").strip()
        if not name or not isinstance(raw, Mapping):
            continue
        argv = raw.get("argv")
        if (
            not isinstance(argv, list)
            or not argv
            or any(not isinstance(item, str) or not item for item in argv)
        ):
            continue
        try:
            timeout_s = float(raw.get("timeout_s", 1800.0))
        except (TypeError, ValueError):
            continue
        if timeout_s <= 0:
            continue
        recipes[name] = {
            "description": str(
                raw.get("description") or "Configured verification recipe."
            ),
            "argv": [python if item == "{python}" else item for item in argv],
            "timeout_s": timeout_s,
        }
    return recipes


def _finite_number(value: Any, *, default: float, minimum: float) -> float:
    if isinstance(value, bool):
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(number) or number < minimum:
        return default
    return number


def _requested_timeout(arguments: Mapping[str, Any]) -> tuple[float | None, str | None]:
    if "timeout_s" not in arguments or arguments.get("timeout_s") is None:
        return None, None
    value = arguments.get("timeout_s")
    if isinstance(value, bool):
        return None, "timeout_s must be a positive number"
    try:
        timeout_s = float(value)
    except (TypeError, ValueError):
        return None, "timeout_s must be a positive number"
    if not math.isfinite(timeout_s) or timeout_s <= 0:
        return None, "timeout_s must be a positive number"
    return timeout_s, None


def _timeout_policy(
    *,
    configured_timeout_s: float,
    requested_timeout_s: float | None,
    arguments: Mapping[str, Any],
    options: Mapping[str, Any] | None,
) -> dict[str, Any]:
    configured = dict(options or {})
    runtime_policy = arguments.get(_HASHI_VERIFICATION_POLICY_ARGUMENT)
    runtime_policy = dict(runtime_policy) if isinstance(runtime_policy, Mapping) else {}
    execution_elapsed_s = _finite_number(
        runtime_policy.get("execution_elapsed_s"), default=0.0, minimum=0.0
    )
    minimum_timeout_s = max(
        _DEFAULT_MINIMUM_TIMEOUT_S,
        _finite_number(
            configured.get("minimum_timeout_s"),
            default=_DEFAULT_MINIMUM_TIMEOUT_S,
            minimum=0.0,
        ),
    )
    multiplier = max(
        _MIN_EXECUTION_TIMEOUT_MULTIPLIER,
        _finite_number(
            configured.get("execution_timeout_multiplier"),
            default=_DEFAULT_EXECUTION_TIMEOUT_MULTIPLIER,
            minimum=0.0,
        ),
    )
    grace_s = max(
        _MIN_TIMEOUT_GRACE_S,
        _finite_number(
            configured.get("timeout_grace_s"),
            default=_DEFAULT_TIMEOUT_GRACE_S,
            minimum=0.0,
        ),
    )
    execution_floor_s = execution_elapsed_s * multiplier + grace_s
    candidates = [configured_timeout_s, minimum_timeout_s, execution_floor_s]
    if requested_timeout_s is not None:
        candidates.append(requested_timeout_s)
    effective_timeout_s = max(candidates)
    return {
        "configured_timeout_s": configured_timeout_s,
        "requested_timeout_s": requested_timeout_s,
        "execution_elapsed_s": execution_elapsed_s,
        "execution_timeout_multiplier": multiplier,
        "timeout_grace_s": grace_s,
        "minimum_timeout_s": minimum_timeout_s,
        "execution_floor_s": execution_floor_s,
        "effective_timeout_s": effective_timeout_s,
        "formula": (
            "max(configured, requested, minimum, execution_elapsed*multiplier+grace)"
        ),
    }


def _direct_argv(value: Any) -> list[str] | None:
    if (
        not isinstance(value, list)
        or not value
        or any(not isinstance(item, str) or not item for item in value)
    ):
        return None
    python = str(Path(sys.executable))
    return [python if item == "{python}" else item for item in value]


def _workspace_authority(root: Path) -> dict[str, Any]:
    return {
        "process_authority": "inherited",
        "identity_policy": "inherited",
        "filesystem_policy": "inherited",
        "environment_policy": "inherited",
        "network_policy": "inherited",
        "home_policy": "inherited",
        "workspace_access": {
            "read": os.access(root, os.R_OK),
            "write": os.access(root, os.W_OK),
            "execute": os.access(root, os.X_OK),
        },
    }


async def _run_workspace_command(
    command: Sequence[str], *, cwd: Path, timeout_s: float,
    capture=None, capture_errors=None, capture_sizes=None,
) -> tuple[int, bytes, bytes, bool, dict[str, Any]]:
    invocation = resolve_argv_invocation(command)
    child_env = dict(os.environ)
    child_env.setdefault("PYTHONIOENCODING", "utf-8")
    child_env.setdefault("PYTHONUTF8", "1")
    proc = await asyncio.create_subprocess_exec(
        *invocation.argv,
        cwd=str(cwd),
        env=child_env,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        **process_group_kwargs(),
    )
    from tools.tool_activity import bounded_communicate

    communicate_task = asyncio.create_task(bounded_communicate(
        proc, limit=_MAX_OUTPUT_CHARS * 2, sinks=capture,
        capture_errors=capture_errors, capture_sizes=capture_sizes,
    ))
    try:
        done, _pending = await asyncio.wait({communicate_task}, timeout=timeout_s)
        if communicate_task in done:
            stdout, stderr = await communicate_task
            return (
                int(proc.returncode or 0),
                stdout,
                stderr,
                False,
                {
                    "status": "normal_completion",
                    "scope": "process_group" if os.name == "posix" else "process_tree",
                    "forced": False,
                    "process_reaped": proc.returncode is not None,
                    "requested_executable": invocation.requested_executable,
                    "resolved_executable": invocation.resolved_executable,
                    "launcher": invocation.launcher,
                    "launcher_executable": invocation.launcher_executable,
                    "encoding": invocation.encoding,
                },
            )

        cleanup = await _stop_workspace_process(proc, communicate_task)
        cleanup.update(
            {
                "requested_executable": invocation.requested_executable,
                "resolved_executable": invocation.resolved_executable,
                "launcher": invocation.launcher,
                "launcher_executable": invocation.launcher_executable,
                "encoding": invocation.encoding,
            }
        )
        stdout, stderr = cleanup.pop("stdout"), cleanup.pop("stderr")
        return int(proc.returncode or 0), stdout, stderr, True, cleanup
    except asyncio.CancelledError as exc:
        cleanup = await _stop_workspace_process(proc, communicate_task)
        cleanup.update(
            {
                "requested_executable": invocation.requested_executable,
                "resolved_executable": invocation.resolved_executable,
                "launcher": invocation.launcher,
                "launcher_executable": invocation.launcher_executable,
                "encoding": invocation.encoding,
            }
        )
        cleanup.pop("stdout", None)
        cleanup.pop("stderr", None)
        setattr(exc, "hashi_tool_details", {"foreground_cleanup": cleanup})
        raise


async def _stop_workspace_process(
    proc: asyncio.subprocess.Process,
    communicate_task: asyncio.Task,
) -> dict[str, Any]:
    """Terminate and reap the whole validation process group."""

    errors: list[str] = []
    forced = False
    scope = "process_group" if os.name == "posix" else "process_tree"
    if proc.returncode is None:
        try:
            if os.name == "nt" and proc.pid:
                await terminate_windows_process_tree(proc.pid, force=True)
                forced = True
            elif os.name == "posix" and proc.pid:
                os.killpg(proc.pid, signal.SIGTERM)
            else:
                proc.terminate()
        except ProcessLookupError:
            pass
        except Exception as exc:
            errors.append(f"terminate:{type(exc).__name__}:{exc}")
    done, _pending = await asyncio.wait({communicate_task}, timeout=5.0)
    if communicate_task not in done:
        forced = True
        try:
            if os.name == "nt" and proc.pid:
                await terminate_windows_process_tree(proc.pid, force=True)
            elif os.name == "posix" and proc.pid:
                os.killpg(proc.pid, signal.SIGKILL)
            else:
                proc.kill()
        except ProcessLookupError:
            pass
        except Exception as exc:
            errors.append(f"kill:{type(exc).__name__}:{exc}")
    try:
        stdout, stderr = await communicate_task
    except Exception as exc:
        errors.append(f"communicate:{type(exc).__name__}:{exc}")
        stdout, stderr = b"", b""
    return {
        "status": "forced" if forced else "terminated",
        "scope": scope,
        "forced": forced,
        "process_reaped": proc.returncode is not None,
        "errors": errors,
        "stdout": stdout,
        "stderr": stderr,
    }


async def execute_verification_run(
    arguments: Mapping[str, Any],
    *,
    workspace_dir: Path,
    options: Mapping[str, Any] | None = None,
    retained_output=None,
) -> BuiltinExecutionResult:
    """List recipes or run a direct argv validation in the real workspace."""

    operation = str(arguments.get("operation") or "").strip().casefold()
    recipes = _recipe_catalog(options)
    source = Path(workspace_dir).resolve()
    runtime_policy = arguments.get(_HASHI_VERIFICATION_POLICY_ARGUMENT)
    policy_arguments = {
        _HASHI_VERIFICATION_POLICY_ARGUMENT: (
            dict(runtime_policy) if isinstance(runtime_policy, Mapping) else {}
        )
    }
    if operation == "list":
        visible_recipes = {
            name: {
                "description": item["description"],
                "configured_timeout_s": item["timeout_s"],
                "effective_timeout_s": _timeout_policy(
                    configured_timeout_s=float(item["timeout_s"]),
                    requested_timeout_s=None,
                    arguments=policy_arguments,
                    options=options,
                )["effective_timeout_s"],
            }
            for name, item in sorted(recipes.items())
        }
        direct_policy = _timeout_policy(
            configured_timeout_s=_finite_number(
                (options or {}).get("direct_timeout_s"),
                default=_DEFAULT_DIRECT_TIMEOUT_S,
                minimum=0.000001,
            ),
            requested_timeout_s=None,
            arguments=policy_arguments,
            options=options,
        )
        visible = {
            "execution_scope": "authoritative_current_workspace",
            "workspace_copied": False,
            "direct_argv_supported": True,
            "shell": False,
            "authority": _workspace_authority(source),
            "timeout_policy": direct_policy,
            "recipes": visible_recipes,
        }
        return _result(
            json.dumps(visible, ensure_ascii=False, sort_keys=True, indent=2),
            operation="list",
            recipe_count=len(visible_recipes),
            execution_scope="workspace",
            workspace_copied=False,
            direct_argv_supported=True,
            effective_timeout_s=direct_policy["effective_timeout_s"],
        )
    if operation != "run":
        return _result("Error: operation must be list or run", operation=operation)
    if "command" in arguments:
        return _result(
            "Error: verification_run does not accept implicit-shell command text; "
            "use argv or a configured recipe",
            operation="run",
        )

    recipe_name = str(arguments.get("recipe") or "").strip()
    raw_argv = arguments.get("argv")
    if recipe_name and raw_argv is not None:
        return _result(
            "Error: verification_run accepts either recipe or argv, not both",
            operation="run",
            recipe=recipe_name,
        )
    recipe = recipes.get(recipe_name) if recipe_name else None
    argv_registered = recipe is not None
    if recipe_name and recipe is None:
        return _result(
            f"Error: unknown verification recipe {recipe_name!r}; call list first",
            operation="run",
            recipe=recipe_name,
            unavailable=True,
        )
    if recipe is not None:
        command = list(recipe["argv"])
        configured_timeout_s = float(recipe["timeout_s"])
        command_ref = recipe_name
    else:
        command = _direct_argv(raw_argv)
        if command is None:
            return _result(
                "Error: verification_run operation=run requires a registered recipe "
                "or a non-empty argv string array",
                operation="run",
                unavailable=True,
            )
        configured_timeout_s = _finite_number(
            (options or {}).get("direct_timeout_s"),
            default=_DEFAULT_DIRECT_TIMEOUT_S,
            minimum=0.000001,
        )
        command_ref = "direct_argv"

    requested_timeout_s, timeout_error = _requested_timeout(arguments)
    if timeout_error is not None:
        return _result(
            f"Error: verification {timeout_error}",
            operation="run",
            recipe=recipe_name or None,
        )
    timeout_policy = _timeout_policy(
        configured_timeout_s=configured_timeout_s,
        requested_timeout_s=requested_timeout_s,
        arguments=arguments,
        options=options,
    )
    if not source.is_dir():
        return _result(
            f"Error: verification workspace is unavailable: {source}",
            operation="run",
            recipe=recipe_name,
            unavailable=True,
        )

    started_at = time.monotonic()
    with ExitStack() as capture_files:
        capture = None
        capture_errors: dict[str, str] = {}
        capture_sizes: dict[str, int] = {}
        if retained_output is not None:
            try:
                capture = {
                    "stdout_bytes": capture_files.enter_context(retained_output[0].temporary_capture_file()),
                    "stderr_bytes": capture_files.enter_context(retained_output[0].temporary_capture_file()),
                }
            except OSError as exc:
                capture_files.close()
                capture = None
                capture_errors["capture"] = type(exc).__name__
        try:
            capture_kwargs = ({
                "capture": capture, "capture_errors": capture_errors, "capture_sizes": capture_sizes,
            } if retained_output is not None else {})
            exit_code, stdout, stderr, timed_out, cleanup = await _run_workspace_command(
                command,
                cwd=source,
                timeout_s=float(timeout_policy["effective_timeout_s"]),
                **capture_kwargs,
            )
        except Exception as exc:
            return _result(
                f"Error: verification command could not start: {exc}",
                operation="run",
                recipe=recipe_name or None,
                unavailable=True,
                execution_scope="workspace",
                workspace_copied=False,
                argv_registered=argv_registered,
                timeout_policy=timeout_policy,
            )

        combined = _bounded_text(stdout + (b"\n" if stdout and stderr else b"") + stderr)
        final_output = (
            combined or f"Verification {command_ref} exited with code {exit_code}."
        )
        prefix = ""
        if timed_out:
            prefix = (f"Error: verification {command_ref} timed out after "
                      f"{timeout_policy['effective_timeout_s']:g} seconds\n")
        elif exit_code != 0:
            prefix = f"Error: verification {command_ref} failed with exit code {exit_code}\n"
        final_output = prefix + final_output
        capture_details: dict[str, Any] = {}
        output_was_large = (sum(capture_sizes.values()) > 20000 if retained_output is not None
                            else len(stdout) + len(stderr) > _MAX_OUTPUT_CHARS)
        if output_was_large:
            reference = None
            if capture is not None and not capture_errors:
                from tools.builtins import _retain_shell_capture
                try:
                    reference, _ = await asyncio.to_thread(
                        _retain_shell_capture, retained_output, capture["stdout_bytes"],
                        capture["stderr_bytes"], str(cleanup.get("encoding") or "utf-8"),
                        None, prefix, "",
                    )
                except (OSError, ValueError, TimeoutError):
                    reference = None
            capture_details = {
                "complete_content": False,
                "output_continuation": reference,
                "output_retention": retained_output[0].retention if reference else "unavailable",
            }
            # Ensure the registry cannot replace the full-output reference with
            # a reference to an already truncated preview.
            low, high = 0, min(len(final_output), 12000)
            while low < high:
                middle = (low + high + 1) // 2
                if len(json.dumps(final_output[:middle], ensure_ascii=False)) <= 12000:
                    low = middle
                else:
                    high = middle - 1
            if reference:
                final_output = (final_output[:low] + f"\n[truncated by HASHI; use tool_output_read "
                                f"reference={reference} offset=0; retained while this Run is active]")
            else:
                final_output = (final_output[:low]
                                + "\n[truncated by HASHI; complete-result retention is unavailable for this Run]")
        authority = _workspace_authority(source)
        return _result(
            final_output,
            operation="run",
            recipe=recipe_name or None,
            command_source="registered_recipe" if argv_registered else "direct_argv",
            exit_code=exit_code,
            timed_out=timed_out,
            elapsed_s=round(max(0.0, time.monotonic() - started_at), 6),
            timeout_s=timeout_policy["effective_timeout_s"],
            timeout_policy=timeout_policy,
            execution_scope="workspace",
            workspace_root=str(source),
            workspace_copied=False,
            argv_registered=argv_registered,
            argv_sha256=hashlib.sha256(
                json.dumps(command, ensure_ascii=False).encode("utf-8")
            ).hexdigest(),
            shell=False,
            process_isolated=False,
            foreground_cleanup=cleanup,
            **capture_details,
            **authority,
        )
