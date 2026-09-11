"""BOM-tolerant reads and private, revision-checked configuration publication.

This is a Function-layer file primitive, not a configuration/schema owner.
Readers have no side effects. Writers sharing this primitive serialize on a
stable OS lock file; a read/modify/write snapshot rejects stale publication.
Legacy plain-dict replacements remain explicit whole-document writes and do
not acquire a revision retrospectively. They must not be used for stale RMW.
"""
from __future__ import annotations

import errno
import hashlib
import json
import os
import stat
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from orchestrator.process_resources import path_lock

_UNSPECIFIED = object()


class ConfigConflictError(RuntimeError):
    """The source document changed after the caller read it; nothing was written."""


class ConfigDurabilityError(OSError):
    """Publication succeeded, but its directory durability could not be confirmed."""

    committed = True


class ConfigDocument(dict):
    """A normal JSON object with out-of-band, non-serialized source metadata."""

    def __init__(self, value: dict, *, source: Path, revision: str):
        super().__init__(value)
        self.source = source
        self.revision = revision

    def copy(self) -> ConfigDocument:
        return type(self)(self, source=self.source, revision=self.revision)


def _revision(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def read_config_json(path: str | Path) -> ConfigDocument:
    source = Path(path).expanduser().resolve()
    raw = source.read_bytes()
    value = json.loads(raw.decode("utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError("configuration must be a JSON object")
    return ConfigDocument(value, source=source, revision=_revision(raw))


@contextmanager
def _write_lock(path: Path, timeout: float) -> Iterator[None]:
    """Keep the lock inode: unlinking it would allow overlapping writers."""
    if not 0 <= timeout <= 60:
        raise ValueError("configuration lock timeout must be between 0 and 60 seconds")
    deadline = time.monotonic() + timeout
    local = path_lock(path)
    if not local.acquire(timeout=timeout):
        raise TimeoutError("configuration write lock timed out")
    descriptor = None
    locked = False
    try:
        lock_path = path.with_name(f".{path.name}.lock")
        if lock_path.is_symlink():
            raise OSError("configuration lock must not be a symbolic link")
        descriptor = os.open(
            lock_path,
            os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise OSError("configuration lock must be a single regular file")
        if os.name == "nt":
            import msvcrt
        else:
            import fcntl
        while True:
            try:
                if os.name == "nt":
                    os.lseek(descriptor, 0, os.SEEK_SET)
                    msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
                else:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
                break
            except OSError as exc:
                if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                    raise
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("configuration write lock timed out") from exc
                time.sleep(min(0.01, remaining))
        yield
    finally:
        try:
            if descriptor is not None:
                try:
                    if locked:
                        if os.name == "nt":
                            os.lseek(descriptor, 0, os.SEEK_SET)
                            msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
                        else:
                            fcntl.flock(descriptor, fcntl.LOCK_UN)
                finally:
                    os.close(descriptor)
        finally:
            local.release()


def _protect_candidate(path: Path) -> None:
    if os.name == "nt":
        from tools.private_files import protect_private_file
        protect_private_file(path)
    else:
        path.chmod(0o600)


def _sync_directory(path: Path) -> None:
    if os.name == "nt":
        # POSIX directory fsync is not a Windows durability guarantee.
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_config_json(
    path: str | Path,
    payload: dict,
    *,
    expected_revision: str | None | object = _UNSPECIFIED,
    lock_timeout: float = 5.0,
) -> str:
    """Publish validated UTF-8/LF bytes, with no BOM or partial destination.

    A ConfigDocument carries its read revision automatically. Plain dicts are
    backwards-compatible full replacements; pass an explicit read revision for
    optimistic concurrency, or None to require an absent destination. All
    participating writers use the same lock. Unmigrated/external writers do not.
    Errors before os.replace preserve the destination. ConfigDurabilityError is
    deliberately different: committed=True means do not blindly retry/rollback.
    """
    if not isinstance(payload, dict):
        raise TypeError("configuration must be a JSON object")
    target = Path(path).expanduser().resolve()
    if isinstance(payload, ConfigDocument):
        if payload.source != target:
            raise ConfigConflictError("configuration snapshot belongs to another file")
        if expected_revision is _UNSPECIFIED:
            expected_revision = payload.revision
    if expected_revision is not _UNSPECIFIED and expected_revision is not None:
        if not isinstance(expected_revision, str) or len(expected_revision) != 64:
            raise ValueError("expected_revision must be a SHA-256 revision or None")
        if any(char not in "0123456789abcdef" for char in expected_revision):
            raise ValueError("expected_revision must be a SHA-256 revision or None")
    # Serialize completely before creating a candidate or touching the target.
    encoded = (json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
    revision = _revision(encoded)
    with _write_lock(target, lock_timeout):
        if expected_revision is not _UNSPECIFIED:
            try:
                current = _revision(target.read_bytes())
            except FileNotFoundError:
                current = None
            if current != expected_revision:
                raise ConfigConflictError("configuration changed since it was read; reload before editing")
        descriptor, name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
        candidate = Path(name)
        try:
            # In particular, apply the Windows DACL while the file is empty.
            _protect_candidate(candidate)
            with os.fdopen(descriptor, "wb") as stream:
                descriptor = -1
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(candidate, target)
            if isinstance(payload, ConfigDocument):
                payload.revision = revision
            try:
                _sync_directory(target.parent)
            except OSError as exc:
                raise ConfigDurabilityError(
                    "configuration was published, but directory synchronization failed"
                ) from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            candidate.unlink(missing_ok=True)
    return revision
