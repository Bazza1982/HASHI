"""Bounded, resumable reads of authorized text files.

A cursor is a position hint, never permission to read a path.  The caller must
resolve and authorize the path on each request.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any


def _file_version(info: os.stat_result) -> list[int]:
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]


def _path_id(path: Path) -> str:
    return hashlib.sha256(os.fsencode(str(path))).hexdigest()


def _encode_cursor(path: Path, info: os.stat_result, position: int, line: int, digest: str) -> str:
    value = {
        "v": 1,
        "path": _path_id(path),
        "file": _file_version(info),
        "position": position,
        "line": line,
        "sha256": digest,
    }
    data = json.dumps(value, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _decode_cursor(token: Any, path: Path, info: os.stat_result) -> tuple[int, int, str]:
    if not isinstance(token, str) or len(token) > 2048 or not re.fullmatch(r"[A-Za-z0-9_-]+", token):
        raise ValueError("invalid file read cursor")
    try:
        data = base64.b64decode(token + "=" * (-len(token) % 4), altchars=b"-_", validate=True)
        value = json.loads(data)
    except (ValueError, UnicodeError):
        raise ValueError("invalid file read cursor") from None
    if not isinstance(value, dict) or value.get("v") != 1 or value.get("path") != _path_id(path):
        raise ValueError("file read cursor does not match this path")
    if value.get("file") != _file_version(info):
        raise ValueError("file changed since the cursor was issued; start a new read")
    position, line = value.get("position"), value.get("line")
    if type(position) is not int or position < 0 or type(line) is not int or line < 1:
        raise ValueError("invalid file read cursor")
    digest = value.get("sha256")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("invalid file read cursor")
    return position, line, digest


async def _yield_hash_progress() -> None:
    await asyncio.sleep(0)


async def _content_digest(path: Path, before: os.stat_result) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        if _file_version(before)[:4] != _file_version(os.fstat(handle.fileno()))[:4]:
            raise ValueError("file changed during the read; start a new read")
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
            await _yield_hash_progress()
        if _file_version(before)[:4] != _file_version(os.fstat(handle.fileno()))[:4]:
            raise ValueError("file changed during the read; start a new read")
    return digest.hexdigest()


async def read_file_page(path: Path, offset: int, limit: int, cursor: Any = None) -> tuple[str, str | None]:
    # Leave room for the header and cursor before the registry's output bound.
    display_path = str(path)
    if len(display_path) > 2048:
        display_path = "…" + display_path[-2047:]
    max_chars = 15_000

    with path.open("r", encoding="utf-8", errors="replace") as handle:
        before = path.stat()
        # On Windows fstat's ctime may differ from path.stat's creation time.
        # Compare the shared identity/version fields across the open handle.
        if _file_version(before)[:4] != _file_version(os.fstat(handle.fileno()))[:4]:
            raise ValueError("file changed during the read; start a new read")
        expected_digest = None
        if cursor is None:
            start_line = offset
            for _ in range(offset - 1):
                while True:
                    chunk = handle.readline(8192)
                    if not chunk:
                        return f"[{display_path}] No lines at offset {offset}", None
                    if chunk.endswith("\n"):
                        break
                    await _yield_hash_progress()
        else:
            position, start_line, expected_digest = _decode_cursor(cursor, path, before)
            try:
                handle.seek(position)
            except (OSError, ValueError, OverflowError):
                raise ValueError("invalid file read cursor position") from None

        current_line = start_line
        last_line = start_line - 1
        complete_lines = 0
        remaining = max_chars
        selected = []
        while remaining and complete_lines < limit:
            chunk = handle.readline(remaining)
            if not chunk:
                break
            selected.append(chunk)
            remaining -= len(chunk)
            last_line = current_line
            if chunk.endswith("\n"):
                complete_lines += 1
                current_line += 1

        next_position = handle.tell()
        has_more = bool(handle.read(1))
        # A metadata-only check cannot detect a same-size rewrite with restored
        # timestamps. Verify exact bytes whenever a continuation is involved.
        digest = await _content_digest(path, before) if has_more or expected_digest is not None else None
        if expected_digest is not None and digest != expected_digest:
            raise ValueError("file changed since the cursor was issued; start a new read")
        handle_after = os.fstat(handle.fileno())
        try:
            after = path.stat()
        except OSError:
            raise ValueError("file changed during the read; start a new read") from None
        if (
            _file_version(before) != _file_version(after)
            or _file_version(after)[:4] != _file_version(handle_after)[:4]
        ):
            raise ValueError("file changed during the read; start a new read")

    header = f"[{display_path}] lines {start_line}-{last_line}"
    content = "".join(selected)
    next_cursor = None
    if has_more:
        next_cursor = _encode_cursor(path, after, next_position, current_line, digest)
        content += f"\n[truncated by HASHI; next_cursor={next_cursor}]"
    return f"{header}\n{content}", next_cursor
# HASHI-Fingerprint: hfp_f9e04809c85b49249d5248d38005801a
