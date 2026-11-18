"""Durable, device-bound metadata for resumable encrypted Browser uploads."""
from __future__ import annotations

import json
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from browser_gateway.store import BrowserGatewayStore


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class UploadBusy(RuntimeError):
    pass


class UploadSessions:
    def __init__(self, store: BrowserGatewayStore, project_root: Path):
        self.store = store
        self.project_root = Path(project_root)
        self.partial_root = self.project_root / "state" / "oll_uploads" / ".partial"
        self.partial_root.mkdir(parents=True, exist_ok=True)
        with store._lock, store._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS browser_upload_sessions (
                    upload_id TEXT PRIMARY KEY,
                    device_id TEXT NOT NULL,
                    thread_id TEXT NOT NULL,
                    attachment_id TEXT NOT NULL UNIQUE,
                    ciphertext_bytes INTEGER NOT NULL,
                    ciphertext_sha256 TEXT NOT NULL,
                    received_bytes INTEGER NOT NULL DEFAULT 0,
                    metadata_json TEXT NOT NULL,
                    storage_relpath TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open',
                    notification_status TEXT NOT NULL,
                    notification_result_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)

    @contextmanager
    def exclusive(self, row: dict[str, Any]):
        """A nonblocking OS lock prevents multiple gateway processes racing."""
        path = self.partial_root / f"{row['upload_id']}.lock"
        with path.open("a+b") as handle:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise UploadBusy("upload is being modified by another gateway") from exc
            try:
                yield
            finally:
                handle.seek(0)
                if os.name == "nt":
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def partial_path(self, row: dict[str, Any]) -> Path:
        return self.partial_root / f"{row['upload_id']}.part"

    def final_path(self, row: dict[str, Any]) -> Path:
        return self.project_root / "state" / row["storage_relpath"]

    def create(self, *, device_id: str, thread_id: str, metadata: dict[str, Any],
               ciphertext_bytes: int, ciphertext_sha256: str) -> dict[str, Any]:
        upload_id = uuid4().hex
        attachment_id = f"att-{uuid4().hex}"
        now = _now()
        storage_relpath = f"oll_uploads/{now[:10].replace('-', '')}/{attachment_id}.bin"
        row = {
            "upload_id": upload_id, "device_id": device_id, "thread_id": thread_id,
            "attachment_id": attachment_id, "ciphertext_bytes": ciphertext_bytes,
            "ciphertext_sha256": ciphertext_sha256, "received_bytes": 0,
            "metadata_json": json.dumps(metadata, ensure_ascii=False),
            "storage_relpath": storage_relpath, "status": "open",
            "notification_status": "pending" if metadata["notify_agent"] else "not_requested",
            "notification_result_json": None, "created_at": now, "updated_at": now,
        }
        partial = self.partial_path(row)
        with partial.open("xb"):
            pass
        try:
            with self.store._lock, self.store._connect() as conn:
                conn.execute("""
                    INSERT INTO browser_upload_sessions (
                        upload_id, device_id, thread_id, attachment_id,
                        ciphertext_bytes, ciphertext_sha256, received_bytes,
                        metadata_json, storage_relpath, status, notification_status,
                        notification_result_json, created_at, updated_at
                    ) VALUES (
                        :upload_id, :device_id, :thread_id, :attachment_id,
                        :ciphertext_bytes, :ciphertext_sha256, :received_bytes,
                        :metadata_json, :storage_relpath, :status, :notification_status,
                        :notification_result_json, :created_at, :updated_at
                    )
                """, row)
        except Exception:
            partial.unlink(missing_ok=True)
            raise
        return row

    def get(self, upload_id: str, device_id: str) -> dict[str, Any] | None:
        with self.store._lock, self.store._connect() as conn:
            row = conn.execute(
                "SELECT * FROM browser_upload_sessions WHERE upload_id=? AND device_id=?",
                (upload_id, device_id),
            ).fetchone()
            return dict(row) if row else None

    def advance(self, row: dict[str, Any], received_bytes: int) -> bool:
        with self.store._lock, self.store._connect() as conn:
            result = conn.execute("""
                UPDATE browser_upload_sessions SET received_bytes=?, updated_at=?
                WHERE upload_id=? AND device_id=? AND status='open' AND received_bytes=?
            """, (received_bytes, _now(), row["upload_id"], row["device_id"], row["received_bytes"]))
            return result.rowcount == 1

    def finish(self, row: dict[str, Any]) -> dict[str, Any]:
        """Publish an already verified final file and session in one DB commit."""
        metadata = json.loads(row["metadata_json"])
        with self.store._lock, self.store._connect() as conn:
            current = conn.execute(
                "SELECT * FROM browser_upload_sessions WHERE upload_id=? AND device_id=?",
                (row["upload_id"], row["device_id"]),
            ).fetchone()
            if current is None:
                raise ValueError("upload no longer exists")
            if current["status"] == "open":
                if current["received_bytes"] != current["ciphertext_bytes"]:
                    raise ValueError("upload is incomplete")
                conn.execute("""
                    INSERT INTO attachments(
                        attachment_id, thread_id, device_id, filename, mime_type,
                        plaintext_bytes, ciphertext_bytes, storage_relpath,
                        encryption_json, note, created_at, status
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'stored')
                """, (
                    current["attachment_id"], current["thread_id"], current["device_id"],
                    metadata["filename"][:255], metadata["mime_type"][:255],
                    metadata["plaintext_bytes"], current["ciphertext_bytes"],
                    current["storage_relpath"], json.dumps(metadata["encryption"], ensure_ascii=False),
                    metadata["note"][:1000], _now(),
                ))
                conn.execute("UPDATE threads SET updated_at=?, last_message_at=? WHERE thread_id=?",
                             (_now(), _now(), current["thread_id"]))
                conn.execute(
                    "UPDATE browser_upload_sessions SET status='completed', updated_at=? WHERE upload_id=?",
                    (_now(), current["upload_id"]),
                )
        result = self.get(row["upload_id"], row["device_id"])
        if result is None:
            raise ValueError("upload no longer exists")
        return result

    def claim_notification(self, row: dict[str, Any]) -> bool:
        with self.store._lock, self.store._connect() as conn:
            result = conn.execute("""
                UPDATE browser_upload_sessions SET notification_status='sending', updated_at=?
                WHERE upload_id=? AND device_id=? AND status='completed'
                  AND notification_status='pending'
            """, (_now(), row["upload_id"], row["device_id"]))
            return result.rowcount == 1

    def finish_notification(self, row: dict[str, Any], status: str, result: dict[str, Any]) -> None:
        if status not in {"sent", "failed", "unknown"}:
            raise ValueError("invalid notification status")
        with self.store._lock, self.store._connect() as conn:
            conn.execute("""
                UPDATE browser_upload_sessions
                SET notification_status=?, notification_result_json=?, updated_at=?
                WHERE upload_id=? AND device_id=? AND notification_status='sending'
            """, (status, json.dumps(result, ensure_ascii=False), _now(),
                  row["upload_id"], row["device_id"]))

    def cancel(self, row: dict[str, Any]) -> bool:
        with self.store._lock, self.store._connect() as conn:
            current = conn.execute(
                "SELECT status FROM browser_upload_sessions WHERE upload_id=? AND device_id=?",
                (row["upload_id"], row["device_id"]),
            ).fetchone()
            if current is None or current["status"] != "open":
                return False
            self.partial_path(row).unlink(missing_ok=True)
            self.final_path(row).unlink(missing_ok=True)
            result = conn.execute(
                "DELETE FROM browser_upload_sessions WHERE upload_id=? AND device_id=? AND status='open'",
                (row["upload_id"], row["device_id"]),
            )
            return result.rowcount == 1
# HASHI-Fingerprint: hfp_df10a97ff5214466881e499f8c563150
