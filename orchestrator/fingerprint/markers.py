"""One qualified inline adapter; other formats use portable projections."""
from __future__ import annotations

import ast
import hashlib
import os
from pathlib import Path
import re
import stat
import tempfile

from .execution import Observation, observe

MARKER = re.compile(rb"(?m)^# HASHI-Fingerprint: (hfp_[0-9a-f]{32})[ \t]*\r?$")
EXCLUDED = re.compile(rb"(?i)(auto.?generated|do not edit|generated file|signed|signature|snapshot|byte.?exact)")


def _write_all(stream, payload: bytes) -> None:
    remaining = memoryview(payload)
    while remaining:
        written = stream.write(remaining)
        if not written:
            raise OSError("marker candidate write made no progress")
        remaining = remaining[written:]


def _replace(source: Path, destination: Path) -> None:
    os.replace(source, destination)


def _restore(source: Path, destination: Path) -> None:
    os.replace(source, destination)


def _link_guard(path: Path) -> Path:
    """Pin the version being replaced so an in-place writer remains observable."""
    descriptor, name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".guard", dir=path.parent
    )
    os.close(descriptor)
    guard = Path(name)
    try:
        guard.unlink()
        os.link(path, guard, follow_symlinks=False)
    except BaseException:
        guard.unlink(missing_ok=True)
        raise
    return guard


def _guard_matches(path: Path, guard: Path, observation: Observation) -> bool:
    """Check the pinned inode and path still contain the observed bytes."""
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    with os.fdopen(descriptor, "rb") as stream:
        before = os.fstat(stream.fileno())
        data = stream.read()
        after = os.fstat(stream.fileno())
    current = path.stat(follow_symlinks=False)
    pinned = guard.stat(follow_symlinks=False)
    identity = lambda info: (info.st_dev, info.st_ino)
    return bool(
        stat.S_ISREG(before.st_mode)
        and before.st_nlink == after.st_nlink == current.st_nlink == pinned.st_nlink == 2
        and identity(before) == identity(after) == identity(current) == identity(pinned)
        and identity(before) == observation.file_id
        and data == observation.data
        and hashlib.sha256(data).hexdigest() == observation.version.sha256
    )


def _same_observation(left: Observation, right: Observation) -> bool:
    return (
        left.version == right.version
        and left.file_id == right.file_id
        and left.data == right.data
    )


def _fallback_after_failure(path: Path, observation: Observation) -> tuple[str, Observation, bool]:
    try:
        current = observe(path)
    except (OSError, ValueError, NotImplementedError):
        # No business bytes were touched by a failed candidate publication.
        # The existing observation remains suitable for a Manifest projection.
        return "manifest", observation, False
    if _same_observation(current, observation):
        return "manifest", observation, False
    return "conflict", current, False


def inline_candidate(path: Path, data: bytes, artifact_id: str) -> bytes | None:
    if path.suffix.casefold() != ".py" or any(part.casefold() in {"snapshots", "__snapshots__", "generated", "vendor", ".git"} for part in path.parts):
        return None
    if len(data) > 1024 * 1024 or EXCLUDED.search(data[:4096]):
        return None
    markers = MARKER.findall(data)
    if markers:
        return data if markers == [artifact_id.encode()] else None
    try:
        # UTF-8/BOM only; other encoding declarations use a manifest.
        text = data.decode("utf-8-sig")
        original = ast.dump(ast.parse(text), include_attributes=False)
        if re.search(r"coding[:=]\s*(?!utf-?8\b)[-\w.]+", text[:300], re.I):
            return None
        newline = b"\r\n" if b"\r\n" in data else b"\n"
        candidate = data + (b"" if not data or data.endswith((b"\n", b"\r")) else newline)
        candidate += b"# HASHI-Fingerprint: " + artifact_id.encode("ascii") + newline
        if ast.dump(ast.parse(candidate.decode("utf-8-sig")), include_attributes=False) != original:
            return None
        return candidate
    except (SyntaxError, UnicodeError, ValueError):
        return None


def mark(path: Path, observation: Observation, artifact_id: str) -> tuple[str, Observation, bool]:
    if observation.version.state != "present" or observation.data is None:
        return "manifest", observation, False
    candidate = inline_candidate(path, observation.data, artifact_id)
    if candidate is None:
        return "manifest", observation, False
    if candidate == observation.data:
        return "inline", observation, False
    candidate_hash = hashlib.sha256(candidate).hexdigest()
    temporary: Path | None = None
    guard: Path | None = None
    descriptor = -1
    try:
        info = path.stat(follow_symlinks=False)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or (info.st_dev, info.st_ino) != observation.file_id):
            return "conflict", observe(path), False
        descriptor, name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        temporary = Path(name)
        mode = stat.S_IMODE(info.st_mode)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            _write_all(stream, candidate)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        # The candidate is complete and durable before the business path is
        # touched. Recheck the exact version immediately before publication.
        current = observe(path)
        if not _same_observation(current, observation):
            return "conflict", current, False
        # A hard-link guard pins the exact inode about to be replaced. Common
        # editors that write in place therefore update the guard too, allowing
        # their bytes to be detected and restored after the atomic publish.
        # Filesystems without same-directory hard links safely use Manifest.
        guard = _link_guard(path)
        if not _guard_matches(path, guard, observation):
            guard.unlink(missing_ok=True)
            guard = None
            return "conflict", observe(path), False
        try:
            _replace(temporary, path)
        except (OSError, ValueError) as replace_error:
            # Replacement APIs are expected to be atomic, but wrappers or
            # transports can report failure after the filesystem committed it.
            # Preserve the guard and reconcile the displaced version below.
            try:
                published = observe(path)
            except Exception:
                raise replace_error
            if not (published.version.state == "present"
                    and published.version.sha256 == candidate_hash
                    and published.data == candidate):
                raise replace_error
        temporary = None
    except (OSError, ValueError, NotImplementedError):
        if guard is not None:
            guard.unlink(missing_ok=True)
            guard = None
        return _fallback_after_failure(path, observation)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    try:
        assert guard is not None
        displaced = observe(guard)
        if not _same_observation(displaced, observation):
            final = observe(path)
            if (final.version.state == "present" and final.data == candidate
                    and final.version.sha256 == candidate_hash):
                try:
                    _restore(guard, path)
                except OSError:
                    # Keep the displaced external bytes recoverable rather than
                    # deleting them when the OS refuses an immediate restore.
                    guard = None
                    return "conflict", final, True
                guard = None
                return "conflict", observe(path), False
            # Another writer also changed the published path. Do not overwrite
            # that newer path; retain the pinned displaced version for recovery.
            guard = None
            return "conflict", final, True
        guard.unlink(missing_ok=True)
        guard = None
    finally:
        if guard is not None:
            guard.unlink(missing_ok=True)
    final = observe(path)
    if final.version.sha256 != candidate_hash:
        return "conflict", final, True
    return "inline", final, True
