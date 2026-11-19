"""Bounded, resumable reads of authorized text files.

A cursor is a position hint, never permission to read a path. The caller must
resolve and authorize the path on each request.
"""

from __future__ import annotations

import asyncio
import base64
import codecs
import hashlib
import io
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
        "v": 2,
        "path": _path_id(path),
        "file": _file_version(info),
        # Position is a character offset in the normalized UTF-8 text stream.
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
    if not isinstance(value, dict) or value.get("path") != _path_id(path):
        raise ValueError("file read cursor does not match this path")
    if value.get("v") == 1:
        # The old cursor's digest could describe different bytes from its first
        # page. It cannot establish a consistent sequence after an upgrade.
        raise ValueError("file read cursor was issued before consistent paging; start a new read")
    if value.get("v") != 2:
        raise ValueError("invalid file read cursor")
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


async def read_file_page(path: Path, offset: int, limit: int, cursor: Any = None) -> tuple[str, str | None]:
    """Read and hash one byte stream so every returned page belongs to that hash."""
    display_path = str(path)
    if len(display_path) > 2048:
        display_path = "…" + display_path[-2047:]
    max_chars = 15_000

    with path.open("rb") as handle:
        before = path.stat()
        # On Windows fstat's ctime may differ from path.stat's creation time.
        if _file_version(before)[:4] != _file_version(os.fstat(handle.fileno()))[:4]:
            raise ValueError("file changed during the read; start a new read")

        expected_digest = None
        cursor_position = None
        start_line = offset
        if cursor is not None:
            cursor_position, start_line, expected_digest = _decode_cursor(cursor, path, before)

        # A UTF-8 decoder with universal newlines has the same text semantics as
        # the original text-mode file reader, including invalid byte replacement.
        decoder = io.IncrementalNewlineDecoder(
            codecs.getincrementaldecoder("utf-8")(errors="replace"), translate=True,
        )
        digest = hashlib.sha256()
        stream_position = 0
        lines_to_skip = offset - 1 if cursor_position is None else 0
        started = lines_to_skip == 0 and cursor_position is None
        page_finished = False
        has_more = False
        selected: list[str] = []
        selected_chars = 0
        complete_lines = 0
        current_line = start_line
        last_line = start_line - 1
        next_position = 0

        while True:
            raw = handle.read(65536)
            if raw:
                digest.update(raw)
                text = decoder.decode(raw)
            else:
                text = decoder.decode(b"", final=True)

            index = 0
            if not started:
                if cursor_position is not None:
                    skipped = min(len(text), max(0, cursor_position - stream_position))
                    stream_position += skipped
                    index += skipped
                    started = stream_position == cursor_position
                else:
                    while index < len(text) and lines_to_skip:
                        newline = text.find("\n", index)
                        if newline < 0:
                            stream_position += len(text) - index
                            index = len(text)
                            break
                        stream_position += newline + 1 - index
                        index = newline + 1
                        lines_to_skip -= 1
                    started = lines_to_skip == 0

            if started and index < len(text):
                if page_finished:
                    has_more = True
                else:
                    end = min(len(text), index + max_chars - selected_chars)
                    remaining_lines = limit - complete_lines
                    newline_index = index
                    for _ in range(remaining_lines):
                        newline_index = text.find("\n", newline_index, end)
                        if newline_index < 0:
                            break
                        newline_index += 1
                    else:
                        end = newline_index

                    part = text[index:end]
                    if part:
                        line_breaks = part.count("\n")
                        last_line = current_line + line_breaks - int(part.endswith("\n"))
                        current_line += line_breaks
                        complete_lines += line_breaks
                        selected_chars += len(part)
                        stream_position += len(part)
                        selected.append(part)
                        next_position = stream_position
                    page_finished = selected_chars == max_chars or complete_lines == limit
                    if page_finished and end < len(text):
                        has_more = True

            if not raw:
                break
            await _yield_hash_progress()

        actual_digest = digest.hexdigest()
        if expected_digest is not None and actual_digest != expected_digest:
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
        if not started:
            if cursor_position is not None:
                raise ValueError("invalid file read cursor position")
            return f"[{display_path}] No lines at offset {offset}", None

    header = f"[{display_path}] lines {start_line}-{last_line}"
    content = "".join(selected)
    next_cursor = None
    if has_more:
        next_cursor = _encode_cursor(path, after, next_position, current_line, actual_digest)
        content += f"\n[truncated by HASHI; next_cursor={next_cursor}]"
    return f"{header}\n{content}", next_cursor
# HASHI-Fingerprint: hfp_4f1a55e629bb4d108b86c35608c07bd9
