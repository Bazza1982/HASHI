"""One qualified inline adapter; other formats use portable projections."""
from __future__ import annotations

import ast
import hashlib
import os
from pathlib import Path
import re
import stat

from .execution import Observation, observe

MARKER = re.compile(rb"(?m)^# HASHI-Fingerprint: (hfp_[0-9a-f]{32})[ \t]*\r?$")
EXCLUDED = re.compile(rb"(?i)(auto.?generated|do not edit|generated file|signed|signature|snapshot|byte.?exact)")


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
    descriptor = os.open(path, os.O_RDWR | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "r+b") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or (info.st_dev, info.st_ino) != observation.file_id
                or stream.read(len(observation.data) + 1) != observation.data):
            return "conflict", observe(path), False
        # Preserve the header, permissions, encoding/BOM and existing newline bytes.
        stream.seek(0)
        stream.write(candidate)
        stream.truncate()
        stream.flush()
        os.fsync(stream.fileno())
    final = observe(path)
    if final.version.sha256 != hashlib.sha256(candidate).hexdigest():
        return "conflict", final, True
    return "inline", final, True
