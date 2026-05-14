from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
import shutil
import time
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


logger = logging.getLogger(__name__)

MAX_ATTACHMENT_BYTES = 16 * 1024 * 1024
MAX_ATTACHMENTS_PER_MESSAGE = 4
MAX_TOTAL_ATTACHMENT_BYTES = 32 * 1024 * 1024
_SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass(slots=True)
class PendingAttachment:
    pending_upload_id: str
    message_id: str
    from_instance: str
    attachment_id: str
    filename: str
    mime_type: str
    size_bytes: int
    sha256: str
    created_at: str
    spool_path: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "pending_upload_id": self.pending_upload_id,
            "message_id": self.message_id,
            "from_instance": self.from_instance,
            "attachment_id": self.attachment_id,
            "filename": self.filename,
            "mime_type": self.mime_type,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "created_at": self.created_at,
            "spool_path": self.spool_path,
        }


class AttachmentStore:
    def __init__(self, *, root: Path, instance_id: str):
        self._instance_id = str(instance_id or "hashi").strip().lower() or "hashi"
        self._root = Path(root)
        self._base_dir = self._root / "state" / "remote_attachments" / self._instance_id
        self._pending_dir = self._base_dir / "pending"
        self._messages_dir = self._base_dir / "messages"
        self._quarantine_dir = self._base_dir / "quarantine"
        for path in (self._pending_dir, self._messages_dir, self._quarantine_dir):
            path.mkdir(parents=True, exist_ok=True)

    def _safe_filename(self, value: str, *, fallback: str) -> str:
        name = Path(str(value or "").strip()).name
        if not name:
            name = fallback
        safe = _SAFE_FILENAME_RE.sub("_", name).strip("._")
        return safe or fallback

    def _json_write_atomic(self, path: Path, payload: dict[str, Any]) -> None:
        tmp_path = path.with_name(f".{path.name}.tmp-{int(time.time() * 1000)}")
        tmp_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp_path.replace(path)

    def _message_pending_dir(self, message_id: str) -> Path:
        return self._pending_dir / str(message_id or "").strip()

    def _message_delivery_dir(self, message_id: str) -> Path:
        return self._messages_dir / str(message_id or "").strip()

    def _load_pending(self, pending_upload_id: str) -> PendingAttachment | None:
        pattern = f"*/{pending_upload_id}.json"
        matches = list(self._pending_dir.glob(pattern))
        if not matches:
            return None
        try:
            data = json.loads(matches[0].read_text(encoding="utf-8"))
            return PendingAttachment(**data)
        except Exception:
            return None

    def _decode_upload_content(self, content_b64: str, expected_sha256: str | None) -> tuple[bytes, str]:
        try:
            data = base64.b64decode(str(content_b64 or "").encode("ascii"), validate=True)
        except Exception as exc:
            raise ValueError(f"content_b64 is not valid base64: {exc}") from exc
        if len(data) > MAX_ATTACHMENT_BYTES:
            raise ValueError(f"attachment exceeds max size of {MAX_ATTACHMENT_BYTES} bytes")
        digest = hashlib.sha256(data).hexdigest()
        if expected_sha256 and str(expected_sha256).strip().lower() != digest:
            raise ValueError("sha256 mismatch")
        return data, digest

    def upload_pending(
        self,
        *,
        message_id: str,
        from_instance: str,
        attachment_id: str,
        filename: str,
        mime_type: str | None,
        content_b64: str,
        sha256: str | None,
    ) -> dict[str, Any]:
        clean_message_id = str(message_id or "").strip()
        clean_from = str(from_instance or "").strip().upper()
        clean_attachment_id = str(attachment_id or "").strip()
        if not clean_message_id:
            raise ValueError("message_id is required")
        if not clean_from:
            raise ValueError("from_instance is required")
        if not clean_attachment_id:
            raise ValueError("attachment_id is required")

        data, digest = self._decode_upload_content(content_b64, sha256)
        pending_upload_id = f"pu-{clean_message_id}-{clean_attachment_id}"
        pending_dir = self._message_pending_dir(clean_message_id)
        pending_dir.mkdir(parents=True, exist_ok=True)
        safe_name = self._safe_filename(filename, fallback=f"{clean_attachment_id}.bin")
        spool_path = pending_dir / f"{clean_attachment_id}__{safe_name}"
        meta_path = pending_dir / f"{pending_upload_id}.json"

        tmp_path = spool_path.with_name(f".{spool_path.name}.tmp-{int(time.time() * 1000)}")
        tmp_path.write_bytes(data)
        tmp_path.replace(spool_path)

        record = PendingAttachment(
            pending_upload_id=pending_upload_id,
            message_id=clean_message_id,
            from_instance=clean_from,
            attachment_id=clean_attachment_id,
            filename=safe_name,
            mime_type=str(mime_type or "application/octet-stream").strip() or "application/octet-stream",
            size_bytes=len(data),
            sha256=digest,
            created_at=datetime.now(timezone.utc).isoformat(),
            spool_path=str(spool_path),
        )
        self._json_write_atomic(meta_path, record.to_dict())
        logger.info(
            "Attachment upload staged: message_id=%s attachment_id=%s bytes=%d",
            clean_message_id,
            clean_attachment_id,
            len(data),
        )
        return record.to_dict()

    def commit_message(
        self,
        *,
        message_id: str,
        from_instance: str,
        attachments: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        clean_message_id = str(message_id or "").strip()
        clean_from = str(from_instance or "").strip().upper()
        if not clean_message_id:
            raise ValueError("message_id is required")
        if not clean_from:
            raise ValueError("from_instance is required")
        if not attachments:
            raise ValueError("attachments are required")
        if len(attachments) > MAX_ATTACHMENTS_PER_MESSAGE:
            raise ValueError(f"attachment count exceeds max of {MAX_ATTACHMENTS_PER_MESSAGE}")

        pending_records: list[tuple[PendingAttachment, dict[str, Any]]] = []
        total_bytes = 0
        for item in attachments:
            pending_upload_id = str((item or {}).get("pending_upload_id") or "").strip()
            if not pending_upload_id:
                raise ValueError("pending_upload_id is required for each attachment")
            pending = self._load_pending(pending_upload_id)
            if pending is None:
                raise ValueError(f"pending upload not found: {pending_upload_id}")
            if pending.message_id != clean_message_id:
                raise ValueError(f"pending upload belongs to a different message: {pending_upload_id}")
            if pending.from_instance != clean_from:
                raise ValueError(f"pending upload belongs to a different sender: {pending_upload_id}")
            total_bytes += int(pending.size_bytes)
            pending_records.append((pending, dict(item or {})))

        if total_bytes > MAX_TOTAL_ATTACHMENT_BYTES:
            raise ValueError(f"total attachment size exceeds max of {MAX_TOTAL_ATTACHMENT_BYTES} bytes")

        final_dir = self._message_delivery_dir(clean_message_id)
        if final_dir.exists():
            raise ValueError("message attachments already committed")
        temp_dir = final_dir.with_name(f".{final_dir.name}.tmp-{int(time.time() * 1000)}")
        temp_dir.mkdir(parents=True, exist_ok=True)

        normalized: list[dict[str, Any]] = []
        try:
            for pending, requested in pending_records:
                src = Path(pending.spool_path)
                if not src.exists():
                    raise ValueError(f"pending file missing: {pending.pending_upload_id}")
                target_name = self._safe_filename(pending.filename, fallback=f"{pending.attachment_id}.bin")
                dest = temp_dir / target_name
                if dest.exists():
                    raise ValueError(f"duplicate attachment filename: {target_name}")
                shutil.copy2(src, dest)
                normalized.append(
                    {
                        "attachment_id": pending.attachment_id,
                        "pending_upload_id": pending.pending_upload_id,
                        "filename": target_name,
                        "mime_type": pending.mime_type,
                        "size_bytes": pending.size_bytes,
                        "sha256": pending.sha256,
                        "stored_path": str(dest),
                        "received_at": datetime.now(timezone.utc).isoformat(),
                        "caption": str(requested.get("caption") or "").strip() or None,
                    }
                )
            self._json_write_atomic(
                temp_dir / "manifest.json",
                {
                    "message_id": clean_message_id,
                    "from_instance": clean_from,
                    "attachments": normalized,
                    "committed_at": datetime.now(timezone.utc).isoformat(),
                },
            )
            temp_dir.replace(final_dir)
            for item in normalized:
                item["stored_path"] = str(final_dir / str(item.get("filename") or "attachment"))
            self._json_write_atomic(
                final_dir / "manifest.json",
                {
                    "message_id": clean_message_id,
                    "from_instance": clean_from,
                    "attachments": normalized,
                    "committed_at": datetime.now(timezone.utc).isoformat(),
                },
            )
        except Exception:
            shutil.rmtree(temp_dir, ignore_errors=True)
            raise

        for pending, _requested in pending_records:
            meta_path = self._message_pending_dir(clean_message_id) / f"{pending.pending_upload_id}.json"
            try:
                Path(pending.spool_path).unlink(missing_ok=True)
                meta_path.unlink(missing_ok=True)
            except Exception:
                logger.warning("Failed cleaning pending attachment %s", pending.pending_upload_id)
        with suppress(Exception):
            self._message_pending_dir(clean_message_id).rmdir()
        logger.info("Attachment message committed: message_id=%s attachments=%d", clean_message_id, len(normalized))
        return normalized

    def get_message_manifest(self, message_id: str) -> dict[str, Any] | None:
        manifest_path = self._message_delivery_dir(message_id) / "manifest.json"
        if not manifest_path.exists():
            return None
        try:
            return json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception:
            return None
