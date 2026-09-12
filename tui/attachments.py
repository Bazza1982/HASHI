"""Local TUI attachment snapshots and path parsing."""
from __future__ import annotations

import hashlib
import mimetypes
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

TUI_ATTACHMENT_MAX_BYTES = 25 * 1024 * 1024


class TuiAttachmentError(ValueError):
    pass


@dataclass(frozen=True)
class PendingAttachment:
    filename: str
    media_type: str
    content: bytes
    sha256: str
    generation: int
    instance_id: str
    agent: str

    def wire_payload(self) -> dict:
        import base64

        return {
            "filename": self.filename,
            "media_type": self.media_type,
            "content_b64": base64.b64encode(self.content).decode("ascii"),
            "size_bytes": len(self.content),
            "sha256": self.sha256,
        }


def _native_path(value: str) -> Path:
    raw = str(value or "").strip().strip('"')
    if not raw:
        raise TuiAttachmentError("a file path is required")
    if os.name != "nt" and re.match(r"^[A-Za-z]:[\\/]", raw):
        try:
            converted = subprocess.run(
                ["wslpath", "-u", raw],
                check=True,
                capture_output=True,
                text=True,
                timeout=3,
            ).stdout.strip()
            if converted:
                raw = converted
        except (OSError, subprocess.SubprocessError):
            raise TuiAttachmentError("the Windows file path could not be resolved") from None
    return Path(raw).expanduser()


def snapshot_path(
    value: str,
    *,
    generation: int,
    instance_id: str,
    agent: str,
    max_bytes: int = TUI_ATTACHMENT_MAX_BYTES,
) -> PendingAttachment:
    path = _native_path(value)
    if path.is_dir():
        raise TuiAttachmentError("directories are not attached recursively")
    if not path.is_file():
        raise TuiAttachmentError("the attachment is not a readable file")
    try:
        size = path.stat().st_size
        if size > max_bytes:
            raise TuiAttachmentError("the attachment exceeds the 25 MiB TUI limit")
        content = path.read_bytes()
    except OSError:
        raise TuiAttachmentError("the attachment could not be read") from None
    if len(content) != size:
        raise TuiAttachmentError("the attachment changed while it was being read")
    filename = path.name or "attachment"
    return PendingAttachment(
        filename=filename,
        media_type=mimetypes.guess_type(filename)[0] or "application/octet-stream",
        content=content,
        sha256=hashlib.sha256(content).hexdigest(),
        generation=int(generation),
        instance_id=str(instance_id).upper(),
        agent=str(agent).casefold(),
    )


def snapshot_bytes(
    content: bytes,
    *,
    filename: str,
    media_type: str,
    generation: int,
    instance_id: str,
    agent: str,
    max_bytes: int = TUI_ATTACHMENT_MAX_BYTES,
) -> PendingAttachment:
    payload = bytes(content)
    if not payload:
        raise TuiAttachmentError("the clipboard does not contain an image")
    if len(payload) > max_bytes:
        raise TuiAttachmentError("the attachment exceeds the 25 MiB TUI limit")
    return PendingAttachment(
        filename=Path(filename).name or "clipboard.png",
        media_type=str(media_type or "application/octet-stream"),
        content=payload,
        sha256=hashlib.sha256(payload).hexdigest(),
        generation=int(generation),
        instance_id=str(instance_id).upper(),
        agent=str(agent).casefold(),
    )


__all__ = [
    "PendingAttachment",
    "TUI_ATTACHMENT_MAX_BYTES",
    "TuiAttachmentError",
    "snapshot_bytes",
    "snapshot_path",
]
