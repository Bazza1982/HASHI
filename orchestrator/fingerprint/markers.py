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


def _same_observation(left: Observation, right: Observation) -> bool:
    return (
        left.version == right.version
        and left.file_id == right.file_id
        and left.data == right.data
    )


def _fallback_after_failure(path: Path, observation: Observation) -> tuple[str, Observation, bool]:
    try:
        current = observe(path)
    except (OSError, ValueError):
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
    temporary: Path | None = None
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
        _replace(temporary, path)
        temporary = None
    except (OSError, ValueError):
        return _fallback_after_failure(path, observation)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    final = observe(path)
    if final.version.sha256 != hashlib.sha256(candidate).hexdigest():
        return "conflict", final, True
    return "inline", final, True
