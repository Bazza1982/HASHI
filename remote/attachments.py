from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import threading
import time
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from orchestrator.hchat_attachment_contract import (
    HCHAT_ATTACHMENT_CHUNK_BYTES,
    HCHAT_MAX_ATTACHMENT_BYTES,
    HCHAT_MAX_ATTACHMENTS_PER_MESSAGE,
    HCHAT_MAX_TOTAL_ATTACHMENT_BYTES,
)


logger = logging.getLogger(__name__)

MAX_ATTACHMENT_BYTES = 16 * 1024 * 1024
MAX_ATTACHMENTS_PER_MESSAGE = 4
MAX_TOTAL_ATTACHMENT_BYTES = 32 * 1024 * 1024
STREAM_ATTACHMENT_CHUNK_BYTES = HCHAT_ATTACHMENT_CHUNK_BYTES
STREAM_MAX_ATTACHMENT_BYTES = HCHAT_MAX_ATTACHMENT_BYTES
STREAM_MAX_ATTACHMENTS_PER_MESSAGE = HCHAT_MAX_ATTACHMENTS_PER_MESSAGE
STREAM_MAX_TOTAL_ATTACHMENT_BYTES = HCHAT_MAX_TOTAL_ATTACHMENT_BYTES
PENDING_ATTACHMENT_TTL_SECONDS = 24 * 60 * 60
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
    streaming: bool = False
    complete: bool = True
    received_bytes: int | None = None

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
            "streaming": self.streaming,
            "complete": self.complete,
            "received_bytes": self.received_bytes,
        }


class AttachmentStore:
    def __init__(self, *, root: Path, instance_id: str):
        self._lock = threading.RLock()
        self._instance_id = str(instance_id or "hashi").strip().lower() or "hashi"
        self._root = Path(root)
        self._base_dir = self._root / "state" / "remote_attachments" / self._instance_id
        self._pending_dir = self._base_dir / "pending"
        self._messages_dir = self._base_dir / "messages"
        self._quarantine_dir = self._base_dir / "quarantine"
        for path in (self._pending_dir, self._messages_dir, self._quarantine_dir):
            path.mkdir(parents=True, exist_ok=True)
        self.sweep_expired_pending()

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
        return self._pending_dir / self._safe_storage_key(message_id)

    def _message_delivery_dir(self, message_id: str) -> Path:
        return self._messages_dir / self._safe_storage_key(message_id)

    @staticmethod
    def _safe_storage_key(value: str) -> str:
        clean = str(value or "").strip()
        if (
            clean
            and clean not in {".", ".."}
            and len(clean) <= 160
            and all(character.isalnum() or character in "._-" for character in clean)
        ):
            return clean
        return "opaque-" + hashlib.sha256(clean.encode("utf-8")).hexdigest()

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

    def _generate_pending_upload_id(self) -> str:
        return f"pu-{int(time.time() * 1000)}-{secrets.token_hex(8)}"

    def _cleanup_pending_record(self, pending: PendingAttachment) -> None:
        meta_path = self._message_pending_dir(pending.message_id) / f"{pending.pending_upload_id}.json"
        try:
            Path(pending.spool_path).unlink(missing_ok=True)
            meta_path.unlink(missing_ok=True)
        except Exception:
            logger.warning("Failed cleaning pending attachment %s", pending.pending_upload_id)
        with suppress(Exception):
            self._message_pending_dir(pending.message_id).rmdir()

    def sweep_expired_pending(self, *, max_age_seconds: int = PENDING_ATTACHMENT_TTL_SECONDS) -> int:
        cutoff = time.time() - max(1, int(max_age_seconds))
        removed = 0
        for meta_path in self._pending_dir.glob("*/*.json"):
            try:
                data = json.loads(meta_path.read_text(encoding="utf-8"))
                created_at = datetime.fromisoformat(str(data.get("created_at") or ""))
                if created_at.replace(tzinfo=created_at.tzinfo or timezone.utc).timestamp() > cutoff:
                    continue
                pending = PendingAttachment(**data)
            except Exception:
                continue
            self._cleanup_pending_record(pending)
            removed += 1
        if removed:
            logger.info("Attachment pending sweep removed %d expired upload(s)", removed)
        return removed

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
        pending_dir = self._message_pending_dir(clean_message_id)
        pending_dir.mkdir(parents=True, exist_ok=True)
        safe_name = self._safe_filename(filename, fallback=f"{clean_attachment_id}.bin")
        for _attempt in range(8):
            pending_upload_id = self._generate_pending_upload_id()
            spool_path = pending_dir / f"{pending_upload_id}__{safe_name}"
            meta_path = pending_dir / f"{pending_upload_id}.json"
            if not spool_path.exists() and not meta_path.exists():
                break
        else:
            raise ValueError("failed to allocate unique pending upload id")

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

    def begin_stream_upload(
        self,
        *,
        message_id: str,
        from_instance: str,
        attachment_id: str,
        filename: str,
        mime_type: str | None,
        size_bytes: int,
        sha256: str,
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
        declared_size = int(size_bytes)
        if declared_size < 0:
            raise ValueError("attachment size must be non-negative")
        if declared_size > STREAM_MAX_ATTACHMENT_BYTES:
            raise ValueError(
                f"attachment exceeds max size of {STREAM_MAX_ATTACHMENT_BYTES} bytes"
            )
        digest = str(sha256 or "").strip().lower()
        if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
            raise ValueError("sha256 must be 64 lowercase hexadecimal characters")

        with self._lock:
            pending_dir = self._message_pending_dir(clean_message_id)
            pending_dir.mkdir(parents=True, exist_ok=True)
            existing: list[PendingAttachment] = []
            for meta_path in pending_dir.glob("*.json"):
                try:
                    existing.append(
                        PendingAttachment(**json.loads(meta_path.read_text(encoding="utf-8")))
                    )
                except Exception:
                    continue
            if len(existing) >= STREAM_MAX_ATTACHMENTS_PER_MESSAGE:
                raise ValueError(
                    f"attachment count exceeds max of {STREAM_MAX_ATTACHMENTS_PER_MESSAGE}"
                )
            if any(item.attachment_id == clean_attachment_id for item in existing):
                raise ValueError(f"duplicate attachment_id: {clean_attachment_id}")
            declared_total = sum(int(item.size_bytes) for item in existing) + declared_size
            if declared_total > STREAM_MAX_TOTAL_ATTACHMENT_BYTES:
                raise ValueError(
                    "total attachment size exceeds max of "
                    f"{STREAM_MAX_TOTAL_ATTACHMENT_BYTES} bytes"
                )

            safe_name = self._safe_filename(
                filename, fallback=f"{clean_attachment_id}.bin"
            )
            for _attempt in range(8):
                pending_upload_id = self._generate_pending_upload_id()
                spool_path = pending_dir / f"{pending_upload_id}__{safe_name}"
                meta_path = pending_dir / f"{pending_upload_id}.json"
                if not spool_path.exists() and not meta_path.exists():
                    break
            else:
                raise ValueError("failed to allocate unique pending upload id")
            try:
                spool_path.touch(exist_ok=False)
                try:
                    os.chmod(spool_path, 0o600)
                except OSError:
                    pass
                record = PendingAttachment(
                    pending_upload_id=pending_upload_id,
                    message_id=clean_message_id,
                    from_instance=clean_from,
                    attachment_id=clean_attachment_id,
                    filename=safe_name,
                    mime_type=str(mime_type or "application/octet-stream").strip()
                    or "application/octet-stream",
                    size_bytes=declared_size,
                    sha256=digest,
                    created_at=datetime.now(timezone.utc).isoformat(),
                    spool_path=str(spool_path),
                    streaming=True,
                    complete=False,
                    received_bytes=0,
                )
                self._json_write_atomic(meta_path, record.to_dict())
            except Exception:
                spool_path.unlink(missing_ok=True)
                meta_path.unlink(missing_ok=True)
                raise
        return record.to_dict()

    def append_stream_chunk(
        self,
        *,
        pending_upload_id: str,
        from_instance: str,
        offset: int,
        payload: bytes,
    ) -> dict[str, Any]:
        if not isinstance(payload, bytes):
            raise ValueError("attachment chunk must be bytes")
        if not payload:
            raise ValueError("attachment chunk must not be empty")
        if len(payload) > STREAM_ATTACHMENT_CHUNK_BYTES:
            raise ValueError(
                f"attachment chunk exceeds max of {STREAM_ATTACHMENT_CHUNK_BYTES} bytes"
            )
        clean_from = str(from_instance or "").strip().upper()
        with self._lock:
            pending = self._load_pending(str(pending_upload_id or "").strip())
            if pending is None:
                raise ValueError("pending upload not found")
            if not pending.streaming:
                raise ValueError("pending upload is not a streaming upload")
            if pending.from_instance != clean_from:
                raise ValueError("pending upload belongs to a different sender")
            if pending.complete:
                raise ValueError("pending upload is already complete")
            received = int(pending.received_bytes or 0)
            if int(offset) != received:
                raise ValueError(f"chunk offset mismatch: expected {received}")
            if received + len(payload) > int(pending.size_bytes):
                raise ValueError("attachment chunk exceeds declared size")
            spool_path = Path(pending.spool_path)
            try:
                with spool_path.open("ab") as handle:
                    handle.write(payload)
                    handle.flush()
            except OSError as exc:
                raise ValueError("attachment chunk could not be stored") from exc
            pending.received_bytes = received + len(payload)
            meta_path = self._message_pending_dir(pending.message_id) / (
                f"{pending.pending_upload_id}.json"
            )
            self._json_write_atomic(meta_path, pending.to_dict())
            return pending.to_dict()

    def finish_stream_upload(
        self,
        *,
        message_id: str,
        from_instance: str,
        pending_upload_id: str,
    ) -> dict[str, Any]:
        clean_message_id = str(message_id or "").strip()
        clean_from = str(from_instance or "").strip().upper()
        with self._lock:
            pending = self._load_pending(str(pending_upload_id or "").strip())
            if pending is None:
                raise ValueError("pending upload not found")
            if not pending.streaming:
                raise ValueError("pending upload is not a streaming upload")
            if pending.message_id != clean_message_id:
                raise ValueError("pending upload belongs to a different message")
            if pending.from_instance != clean_from:
                raise ValueError("pending upload belongs to a different sender")
            if pending.complete:
                return pending.to_dict()
            received = int(pending.received_bytes or 0)
            if received != int(pending.size_bytes):
                raise ValueError(
                    f"attachment is incomplete: received {received} of {pending.size_bytes} bytes"
                )
            spool_path = Path(pending.spool_path)
            digest = hashlib.sha256()
            try:
                with spool_path.open("rb") as handle:
                    while chunk := handle.read(STREAM_ATTACHMENT_CHUNK_BYTES):
                        digest.update(chunk)
            except OSError as exc:
                raise ValueError("pending file is unavailable") from exc
            if digest.hexdigest() != pending.sha256:
                raise ValueError("sha256 mismatch")
            pending.complete = True
            meta_path = self._message_pending_dir(pending.message_id) / (
                f"{pending.pending_upload_id}.json"
            )
            self._json_write_atomic(meta_path, pending.to_dict())
            return pending.to_dict()

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
        with self._lock:
            pending_records: list[tuple[PendingAttachment, dict[str, Any]]] = []
            total_bytes = 0
            seen_pending: set[str] = set()
            seen_attachment_ids: set[str] = set()
            for item in attachments:
                requested = dict(item or {})
                pending_upload_id = str(requested.get("pending_upload_id") or "").strip()
                if not pending_upload_id:
                    raise ValueError("pending_upload_id is required for each attachment")
                if pending_upload_id in seen_pending:
                    raise ValueError(f"duplicate pending upload: {pending_upload_id}")
                seen_pending.add(pending_upload_id)
                pending = self._load_pending(pending_upload_id)
                if pending is None:
                    raise ValueError(f"pending upload not found: {pending_upload_id}")
                if pending.message_id != clean_message_id:
                    raise ValueError(f"pending upload belongs to a different message: {pending_upload_id}")
                if pending.from_instance != clean_from:
                    raise ValueError(f"pending upload belongs to a different sender: {pending_upload_id}")
                if pending.streaming and not pending.complete:
                    raise ValueError(f"pending upload is incomplete: {pending_upload_id}")
                if pending.attachment_id in seen_attachment_ids:
                    raise ValueError(f"duplicate attachment_id: {pending.attachment_id}")
                seen_attachment_ids.add(pending.attachment_id)
                requested_attachment_id = str(requested.get("attachment_id") or "").strip()
                if requested_attachment_id and requested_attachment_id != pending.attachment_id:
                    raise ValueError("attachment_id does not match pending upload")
                requested_sha = str(requested.get("sha256") or "").strip().lower()
                if requested_sha and requested_sha != pending.sha256:
                    raise ValueError("sha256 does not match pending upload")
                requested_size = requested.get("size_bytes")
                if requested_size is not None and int(requested_size) != pending.size_bytes:
                    raise ValueError("size_bytes does not match pending upload")
                total_bytes += int(pending.size_bytes)
                pending_records.append((pending, requested))

            uses_streaming = any(pending.streaming for pending, _ in pending_records)
            max_count = (
                STREAM_MAX_ATTACHMENTS_PER_MESSAGE
                if uses_streaming
                else MAX_ATTACHMENTS_PER_MESSAGE
            )
            max_total = (
                STREAM_MAX_TOTAL_ATTACHMENT_BYTES
                if uses_streaming
                else MAX_TOTAL_ATTACHMENT_BYTES
            )
            if len(attachments) > max_count:
                raise ValueError(f"attachment count exceeds max of {max_count}")
            if total_bytes > max_total:
                raise ValueError(f"total attachment size exceeds max of {max_total} bytes")

            final_dir = self._message_delivery_dir(clean_message_id)
            if final_dir.exists():
                raise ValueError("message attachments already committed")
            temp_dir = final_dir.with_name(
                f".{final_dir.name}.tmp-{int(time.time() * 1000)}-{secrets.token_hex(4)}"
            )
            temp_dir.mkdir(parents=True, exist_ok=False)

            normalized: list[dict[str, Any]] = []
            committed_at = datetime.now(timezone.utc).isoformat()
            try:
                for pending, requested in pending_records:
                    src = Path(pending.spool_path)
                    if not src.is_file() or src.stat().st_size != pending.size_bytes:
                        raise ValueError(f"pending file missing or changed: {pending.pending_upload_id}")
                    observed = hashlib.sha256()
                    with src.open("rb") as handle:
                        while chunk := handle.read(STREAM_ATTACHMENT_CHUNK_BYTES):
                            observed.update(chunk)
                    if observed.hexdigest() != pending.sha256:
                        raise ValueError(f"pending file digest changed: {pending.pending_upload_id}")
                    target_name = self._safe_filename(
                        pending.filename, fallback=f"{pending.attachment_id}.bin"
                    )
                    dest = temp_dir / target_name
                    if dest.exists():
                        raise ValueError(f"duplicate attachment filename: {target_name}")
                    try:
                        os.link(src, dest)
                    except OSError:
                        shutil.copy2(src, dest)
                    normalized.append(
                        {
                            "attachment_id": pending.attachment_id,
                            "pending_upload_id": pending.pending_upload_id,
                            "filename": target_name,
                            "mime_type": pending.mime_type,
                            "size_bytes": pending.size_bytes,
                            "sha256": pending.sha256,
                            "received_at": datetime.now(timezone.utc).isoformat(),
                            "caption": str(requested.get("caption") or "").strip() or None,
                            "stored_path": str(final_dir / target_name),
                        }
                    )
                self._json_write_atomic(
                    temp_dir / "manifest.json",
                    {
                        "message_id": clean_message_id,
                        "from_instance": clean_from,
                        "attachments": normalized,
                        "committed_at": committed_at,
                    },
                )
                temp_dir.replace(final_dir)
            except Exception:
                shutil.rmtree(temp_dir, ignore_errors=True)
                raise

            for pending, _requested in pending_records:
                self._cleanup_pending_record(pending)
            logger.info(
                "Attachment message committed: message_id=%s attachments=%d",
                clean_message_id,
                len(normalized),
            )
            return normalized

    def rollback_message(self, message_id: str) -> bool:
        final_dir = self._message_delivery_dir(message_id)
        with self._lock:
            if not final_dir.exists():
                return False
            shutil.rmtree(final_dir)
            return True

    def get_message_manifest(self, message_id: str) -> dict[str, Any] | None:
        manifest_path = self._message_delivery_dir(message_id) / "manifest.json"
        if not manifest_path.exists():
            return None
        try:
            return json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception:
            return None

    def cancel_pending_uploads(
        self,
        *,
        message_id: str,
        from_instance: str,
        pending_upload_ids: list[str],
    ) -> int:
        clean_message_id = str(message_id or "").strip()
        clean_from = str(from_instance or "").strip().upper()
        if not clean_message_id or not clean_from:
            raise ValueError("message_id and from_instance are required")
        removed = 0
        with self._lock:
            for pending_upload_id in pending_upload_ids:
                pending = self._load_pending(str(pending_upload_id or "").strip())
                if pending is None:
                    continue
                if pending.message_id != clean_message_id or pending.from_instance != clean_from:
                    raise ValueError(f"pending upload belongs to a different message or sender: {pending.pending_upload_id}")
                self._cleanup_pending_record(pending)
                removed += 1
        return removed
