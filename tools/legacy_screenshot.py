
"""Bounded pages for legacy screenshot strings, preserving source order."""

from __future__ import annotations

import base64
import io
import os
import re
import warnings
from typing import Any

LEGACY_SCREENSHOT_TOOLS = frozenset({
    "browser_screenshot", "browser_session", "desktop_screenshot", "windows_screenshot",
})
LEGACY_SCREENSHOT_PATTERN = re.compile(
    r"(?P<data_url>data:(?P<mime>image/[a-z0-9.+-]+);base64,"
    r"(?P<data_payload>[A-Za-z0-9+/=]*))|"
    r"(?P<label>(?:^screenshot:|\[screenshot\]\s+base64:))"
    r"(?P<label_payload>[A-Za-z0-9+/=]*)",
    flags=re.IGNORECASE | re.MULTILINE,
)


def _positive_setting(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, ""))
        return value if value > 0 else default
    except (TypeError, ValueError):
        return default


def normalize_legacy_screenshot(payload: str, declared_mime: str | None = None) -> tuple[dict[str, str], int]:
    from PIL import Image
    from tools.media_read import IMAGE_MAX_BYTES, MediaReadError, _jpeg_bytes

    if len(payload) > ((IMAGE_MAX_BYTES + 2) // 3) * 4:
        raise MediaReadError("encoded screenshot exceeds the source safety limit")
    try:
        raw = base64.b64decode(payload, validate=True)
    except ValueError as exc:
        raise MediaReadError("screenshot returned malformed base64") from exc
    if not raw or len(raw) > IMAGE_MAX_BYTES:
        raise MediaReadError("screenshot is empty or exceeds the source safety limit")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as image:
                actual_mime = (Image.MIME.get(image.format or "") or "").lower()
                expected_mime = (declared_mime or "").lower()
                if expected_mime == "image/jpg":
                    expected_mime = "image/jpeg"
                if expected_mime and actual_mime != expected_mime:
                    raise MediaReadError("screenshot MIME does not match the decoded image")
                image.seek(0)
                encoded, _size = _jpeg_bytes(image.copy())
    except MediaReadError:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise MediaReadError("screenshot dimensions exceed the decode safety limit") from exc
    except Exception as exc:
        raise MediaReadError(f"screenshot decode failed: {exc}") from exc
    return (
        {"type": "image", "mimeType": "image/jpeg", "data": base64.b64encode(encoded).decode("ascii")},
        len(encoded),
    )


def legacy_screenshot_page(
    tool_name: str, output: str, *, offset: int = 0, limit: int | None = None,
    frame_budget: int | None = None, byte_budget: int | None = None,
) -> dict[str, Any] | None:
    """Build one page; offsets count source frames, including malformed ones."""
    if tool_name not in LEGACY_SCREENSHOT_TOOLS:
        return None
    matches = list(LEGACY_SCREENSHOT_PATTERN.finditer(output))
    if not matches:
        return None
    if type(offset) is not int or offset < 0 or offset > len(matches):
        raise ValueError("invalid screenshot frame offset")
    if limit is not None and (type(limit) is not int or limit < 1):
        raise ValueError("invalid screenshot page limit")
    from tools.media_read import MAX_TOTAL_IMAGE_BYTES

    frame_limit = (frame_budget if type(frame_budget) is int and frame_budget > 0
                   else _positive_setting("HASHI_MCP_IMAGE_PAGE_COUNT", 6))
    if limit is not None:
        frame_limit = min(frame_limit, limit)
    byte_limit = (byte_budget if type(byte_budget) is int and byte_budget > 0
                  else _positive_setting("HASHI_MCP_IMAGE_PAGE_BYTES", MAX_TOTAL_IMAGE_BYTES))
    content: list[dict[str, Any]] = []
    outcomes: dict[int, bool] = {}
    cursor = 0 if offset == 0 else matches[offset - 1].end()
    image_count = 0
    total_bytes = 0
    end = offset

    for index in range(offset, len(matches)):
        if index - offset >= frame_limit:
            break
        match = matches[index]
        payload = match.group("data_payload") or match.group("label_payload") or ""
        try:
            image, image_bytes = normalize_legacy_screenshot(payload, match.group("mime"))
            if total_bytes + image_bytes > byte_limit:
                if end > offset:
                    break
                raise ValueError("screenshot exceeds the configured single-page image-byte budget")
            before = output[cursor:match.start()].strip()
            if before:
                content.append({"type": "text", "text": before})
            content.append(image)
            image_count += 1
            total_bytes += image_bytes
            outcomes[index] = True
        except ValueError as exc:
            before = output[cursor:match.start()].strip()
            if before:
                content.append({"type": "text", "text": before})
            content.append({"type": "text", "text": f"Screenshot unavailable: {exc}"})
            outcomes[index] = False
        cursor = match.end()
        end = index + 1

    if end == len(matches):
        trailing = output[cursor:].strip()
        if trailing:
            content.append({"type": "text", "text": trailing})
    return {
        "content": content, "offset": offset, "next_offset": end if end < len(matches) else None,
        "total": len(matches), "image_count": image_count, "outcomes": outcomes,
        "page_count_budget": frame_limit, "page_byte_budget": byte_limit,
        "is_error": image_count == 0 and any(not valid for valid in outcomes.values()),
    }



def indexed_screenshot_page(
    total: int, characters: int, frame_at, read_range, *,
    offset: int = 0, limit: int | None = None,
    frame_budget: int | None = None, byte_budget: int | None = None,
) -> dict[str, Any]:
    """Read just the requested legacy frames from a retained disk result."""
    if type(offset) is not int or offset < 0 or offset > total:
        raise ValueError("invalid screenshot frame offset")
    if limit is not None and (type(limit) is not int or limit < 1):
        raise ValueError("invalid screenshot page limit")
    from tools.media_read import IMAGE_MAX_BYTES, MAX_TOTAL_IMAGE_BYTES

    frame_limit = (frame_budget if type(frame_budget) is int and frame_budget > 0
                   else _positive_setting("HASHI_MCP_IMAGE_PAGE_COUNT", 6))
    if limit is not None:
        frame_limit = min(frame_limit, limit)
    byte_limit = (byte_budget if type(byte_budget) is int and byte_budget > 0
                  else _positive_setting("HASHI_MCP_IMAGE_PAGE_BYTES", MAX_TOTAL_IMAGE_BYTES))
    content: list[dict[str, Any]] = []
    outcomes: dict[int, bool] = {}
    cursor = 0 if offset == 0 else frame_at(offset - 1)[1]
    image_count = 0
    total_bytes = 0
    end = offset

    for index in range(offset, min(total, offset + frame_limit)):
        start, finish, payload_start, payload_end, mime_start, mime_end = frame_at(index)
        try:
            if payload_end - payload_start > ((IMAGE_MAX_BYTES + 2) // 3) * 4:
                raise ValueError("encoded screenshot exceeds the source safety limit")
            payload = read_range(payload_start, payload_end)
            mime = None if mime_start < 0 else read_range(mime_start, mime_end)
            image, image_bytes = normalize_legacy_screenshot(payload, mime)
            if total_bytes + image_bytes > byte_limit:
                if end > offset:
                    break
                raise ValueError("screenshot exceeds the configured single-page image-byte budget")
            before = read_range(cursor, start).strip()
            if before:
                content.append({"type": "text", "text": before})
            content.append(image)
            image_count += 1
            total_bytes += image_bytes
            outcomes[index] = True
        except ValueError as exc:
            if str(exc) == "retained output is corrupt or unavailable":
                raise
            before = read_range(cursor, start).strip()
            if before:
                content.append({"type": "text", "text": before})
            content.append({"type": "text", "text": f"Screenshot unavailable: {exc}"})
            outcomes[index] = False
        cursor = finish
        end = index + 1

    if end == total:
        trailing = read_range(cursor, characters).strip()
        if trailing:
            content.append({"type": "text", "text": trailing})
    return {
        "content": content, "offset": offset, "next_offset": end if end < total else None,
        "total": total, "image_count": image_count, "outcomes": outcomes,
        "page_count_budget": frame_limit, "page_byte_budget": byte_limit,
        "is_error": image_count == 0 and any(not valid for valid in outcomes.values()),
    }


def continuation_note(reference: str | None, offset: int, total: int) -> str:
    if reference:
        return (
            f"Screenshot page incomplete: {total - offset} frame(s) remain. "
            f"Use tool_output_read with reference {reference} and offset {offset} in this Run; "
            "the reference lasts while this Run and its retention remain available."
        )
    return (
        f"Screenshot page incomplete: {total - offset} frame(s) remain, but no Run-bound "
        "continuation could be retained. The remaining frames are unavailable."
    )
