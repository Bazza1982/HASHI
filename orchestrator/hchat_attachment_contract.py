"""Shared, transport-neutral contract for one HChat attachment message."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from pathlib import Path, PureWindowsPath
from typing import Any


HCHAT_ATTACHMENT_CLAIM_KEY = "_hchat_attachment_manifest"
HCHAT_ATTACHMENT_CHUNK_BYTES = 8 * 1024 * 1024
HCHAT_MAX_ATTACHMENT_BYTES = 1024 * 1024 * 1024
HCHAT_MAX_ATTACHMENTS_PER_MESSAGE = 10
HCHAT_MAX_TOTAL_ATTACHMENT_BYTES = 1024 * 1024 * 1024

_ATTACHMENT_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,160}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def canonical_hchat_attachment_manifest(value: Any) -> list[dict[str, Any]]:
    """Return the exact signed manifest admitted by the local Workbench hop.

    File type is deliberately not an admission criterion. Unknown or malformed
    MIME declarations become opaque binary files; bytes are never executed or
    extracted by this contract.
    """

    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise ValueError("HChat attachments must be an array")
    if not value:
        raise ValueError("HChat attachments must not be empty")
    if len(value) > HCHAT_MAX_ATTACHMENTS_PER_MESSAGE:
        raise ValueError(
            f"HChat attachment count exceeds {HCHAT_MAX_ATTACHMENTS_PER_MESSAGE}"
        )

    normalized: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    seen_paths: set[str] = set()
    total_bytes = 0
    for raw in value:
        if not isinstance(raw, Mapping):
            raise ValueError("each HChat attachment must be an object")
        attachment_id = str(raw.get("attachment_id") or "").strip()
        if not _ATTACHMENT_ID_RE.fullmatch(attachment_id):
            raise ValueError("HChat attachment_id is invalid")
        if attachment_id in seen_ids:
            raise ValueError(f"duplicate HChat attachment_id: {attachment_id}")
        seen_ids.add(attachment_id)

        filename = str(raw.get("filename") or "").strip()
        if (
            not filename
            or len(filename.encode("utf-8")) > 255
            or "\x00" in filename
            or Path(filename).name != filename
            or PureWindowsPath(filename).name != filename
        ):
            raise ValueError("HChat attachment filename is invalid")

        declared_size = raw.get("size_bytes")
        if isinstance(declared_size, bool):
            raise ValueError("HChat attachment size is invalid")
        try:
            size_bytes = int(declared_size)
        except (TypeError, ValueError) as exc:
            raise ValueError("HChat attachment size is invalid") from exc
        if size_bytes < 0 or size_bytes > HCHAT_MAX_ATTACHMENT_BYTES:
            raise ValueError(
                f"HChat attachment exceeds {HCHAT_MAX_ATTACHMENT_BYTES} bytes"
            )
        total_bytes += size_bytes
        if total_bytes > HCHAT_MAX_TOTAL_ATTACHMENT_BYTES:
            raise ValueError(
                f"HChat attachment total exceeds {HCHAT_MAX_TOTAL_ATTACHMENT_BYTES} bytes"
            )

        digest = str(raw.get("sha256") or "").strip().casefold()
        if not _SHA256_RE.fullmatch(digest):
            raise ValueError("HChat attachment sha256 is invalid")

        stored_path = str(raw.get("stored_path") or "").strip()
        if (
            not stored_path
            or len(stored_path.encode("utf-8")) > 4096
            or "\x00" in stored_path
            or stored_path in seen_paths
        ):
            raise ValueError("HChat attachment stored_path is invalid")
        seen_paths.add(stored_path)

        mime_type = (
            str(raw.get("mime_type") or "application/octet-stream")
            .split(";", 1)[0]
            .strip()
            .casefold()
        )
        if (
            not mime_type
            or "/" not in mime_type
            or len(mime_type) > 255
            or any(ord(character) < 33 or ord(character) > 126 for character in mime_type)
        ):
            mime_type = "application/octet-stream"
        caption = str(raw.get("caption") or "").strip()
        if len(caption.encode("utf-8")) > 16 * 1024:
            raise ValueError("HChat attachment caption is too large")

        normalized.append(
            {
                "attachment_id": attachment_id,
                "filename": filename,
                "mime_type": mime_type,
                "size_bytes": size_bytes,
                "sha256": digest,
                "caption": caption or None,
                "stored_path": stored_path,
            }
        )
    return normalized


__all__ = [
    "HCHAT_ATTACHMENT_CLAIM_KEY",
    "HCHAT_ATTACHMENT_CHUNK_BYTES",
    "HCHAT_MAX_ATTACHMENT_BYTES",
    "HCHAT_MAX_ATTACHMENTS_PER_MESSAGE",
    "HCHAT_MAX_TOTAL_ATTACHMENT_BYTES",
    "canonical_hchat_attachment_manifest",
]
