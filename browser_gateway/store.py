from __future__ import annotations

import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock
from typing import Any
from uuid import uuid4
import hashlib


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass
class PairRequest:
    device_id: str
    pairing_code: str
    expires_at: str


class BrowserGatewayStore:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS devices (
                    device_id TEXT PRIMARY KEY,
                    device_label TEXT NOT NULL,
                    token_hash TEXT,
                    created_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    scopes_json TEXT NOT NULL,
                    recovery_state TEXT NOT NULL DEFAULT 'active',
                    last_pairing_version INTEGER NOT NULL DEFAULT 1,
                    recovery_code_hash TEXT NOT NULL DEFAULT '',
                    recovery_payload_json TEXT NOT NULL DEFAULT '',
                    recovery_updated_at TEXT NOT NULL DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS pair_requests (
                    device_id TEXT PRIMARY KEY,
                    pairing_code TEXT NOT NULL,
                    device_label TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS threads (
                    thread_id TEXT PRIMARY KEY,
                    device_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    instance_id TEXT NOT NULL DEFAULT 'HASHI1',
                    title TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_message_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    agent_transcript_checkpoint TEXT DEFAULT '',
                    FOREIGN KEY(device_id) REFERENCES devices(device_id)
                );

                CREATE TABLE IF NOT EXISTS messages (
                    message_id TEXT PRIMARY KEY,
                    thread_id TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    status TEXT NOT NULL,
                    text_preview TEXT DEFAULT '',
                    source_tag TEXT DEFAULT '',
                    created_at TEXT NOT NULL,
                    completed_at TEXT DEFAULT '',
                    FOREIGN KEY(thread_id) REFERENCES threads(thread_id)
                );

                CREATE TABLE IF NOT EXISTS attachments (
                    attachment_id TEXT PRIMARY KEY,
                    thread_id TEXT NOT NULL,
                    device_id TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    mime_type TEXT NOT NULL,
                    plaintext_bytes INTEGER NOT NULL DEFAULT 0,
                    ciphertext_bytes INTEGER NOT NULL DEFAULT 0,
                    storage_relpath TEXT NOT NULL,
                    encryption_json TEXT NOT NULL DEFAULT '',
                    note TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'stored',
                    FOREIGN KEY(thread_id) REFERENCES threads(thread_id),
                    FOREIGN KEY(device_id) REFERENCES devices(device_id)
                );
                """
            )
            self._ensure_column(conn, "devices", "recovery_code_hash", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(conn, "devices", "recovery_payload_json", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(conn, "devices", "recovery_updated_at", "TEXT NOT NULL DEFAULT ''")

    def _ensure_column(self, conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
        existing = {
            row["name"]
            for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if column in existing:
            return
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")

    def create_pair_request(self, device_label: str, ttl_minutes: int = 10) -> PairRequest:
        device_id = f"device-{uuid4().hex[:12]}"
        pairing_code = secrets.token_hex(4)
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(minutes=ttl_minutes)
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO pair_requests(device_id, pairing_code, device_label, created_at, expires_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (device_id, pairing_code, device_label, now.isoformat(), expires_at.isoformat()),
            )
        return PairRequest(device_id=device_id, pairing_code=pairing_code, expires_at=expires_at.isoformat())

    def complete_pair(self, device_id: str, pairing_code: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM pair_requests WHERE device_id = ? AND pairing_code = ?",
                (device_id, pairing_code),
            ).fetchone()
            if row is None:
                return None
            if datetime.fromisoformat(row["expires_at"]) < datetime.now(timezone.utc):
                conn.execute("DELETE FROM pair_requests WHERE device_id = ?", (device_id,))
                return None
            access_token = f"oll_{secrets.token_urlsafe(24)}"
            now = _utc_now()
            conn.execute(
                """
                INSERT OR REPLACE INTO devices(
                    device_id, device_label, token_hash, created_at, last_seen_at, status, scopes_json,
                    recovery_state, last_pairing_version
                ) VALUES (?, ?, ?, COALESCE((SELECT created_at FROM devices WHERE device_id = ?), ?), ?, 'active', ?, 'active', 1)
                """,
                (
                    device_id,
                    row["device_label"],
                    _hash_token(access_token),
                    device_id,
                    now,
                    now,
                    '["chat"]',
                ),
            )
            conn.execute("DELETE FROM pair_requests WHERE device_id = ?", (device_id,))
        return {"device_id": device_id, "access_token": access_token}

    def authenticate(self, access_token: str) -> dict[str, Any] | None:
        token_hash = _hash_token(access_token)
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM devices WHERE token_hash = ? AND status = 'active'",
                (token_hash,),
            ).fetchone()
            if row is None:
                return None
            now = _utc_now()
            conn.execute("UPDATE devices SET last_seen_at = ? WHERE device_id = ?", (now, row["device_id"]))
            result = dict(row)
            result["last_seen_at"] = now
            return result

    def refresh_token(self, device_id: str) -> dict[str, Any] | None:
        access_token = f"oll_{secrets.token_urlsafe(24)}"
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM devices WHERE device_id = ? AND status = 'active'", (device_id,)).fetchone()
            if row is None:
                return None
            now = _utc_now()
            conn.execute(
                "UPDATE devices SET token_hash = ?, last_seen_at = ? WHERE device_id = ?",
                (_hash_token(access_token), now, device_id),
            )
        return {"device_id": device_id, "access_token": access_token}

    def revoke_token(self, device_id: str) -> bool:
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "UPDATE devices SET token_hash = NULL, status = 'revoked' WHERE device_id = ?",
                (device_id,),
            )
            return cur.rowcount > 0

    def create_thread(self, device_id: str, agent_id: str, title: str = "", instance_id: str = "HASHI1") -> dict[str, Any]:
        thread_id = f"thread-{uuid4().hex[:12]}"
        now = _utc_now()
        title = title.strip() or f"{agent_id}@{instance_id}"
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO threads(
                    thread_id, device_id, agent_id, instance_id, title, created_at, updated_at,
                    last_message_at, status, agent_transcript_checkpoint
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', '')
                """,
                (thread_id, device_id, agent_id, instance_id, title, now, now, now),
            )
        return self.get_thread(thread_id, device_id)

    def list_threads(self, device_id: str) -> list[dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM threads WHERE device_id = ? ORDER BY updated_at DESC",
                (device_id,),
            ).fetchall()
            return [dict(row) for row in rows]

    def get_thread(self, thread_id: str, device_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM threads WHERE thread_id = ? AND device_id = ?",
                (thread_id, device_id),
            ).fetchone()
            return dict(row) if row else None

    def append_message(
        self,
        thread_id: str,
        direction: str,
        status: str,
        text_preview: str = "",
        source_tag: str = "",
    ) -> str:
        message_id = f"msg-{uuid4().hex[:12]}"
        now = _utc_now()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO messages(message_id, thread_id, direction, status, text_preview, source_tag, created_at, completed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, '')
                """,
                (message_id, thread_id, direction, status, text_preview[:500], source_tag, now),
            )
            conn.execute(
                "UPDATE threads SET updated_at = ?, last_message_at = ? WHERE thread_id = ?",
                (now, now, thread_id),
            )
        return message_id

    def complete_message(self, message_id: str, status: str, text_preview: str = "") -> None:
        now = _utc_now()
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE messages SET status = ?, text_preview = ?, completed_at = ? WHERE message_id = ?",
                (status, text_preview[:500], now, message_id),
            )

    def set_thread_checkpoint(self, thread_id: str, checkpoint: str) -> None:
        now = _utc_now()
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE threads SET agent_transcript_checkpoint = ?, updated_at = ? WHERE thread_id = ?",
                (checkpoint, now, thread_id),
            )

    def device_status(self, device_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM devices WHERE device_id = ?", (device_id,)).fetchone()
            return dict(row) if row else None

    def set_device_recovery(self, device_id: str, recovery_code_hash: str, recovery_payload_json: str) -> bool:
        now = _utc_now()
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                """
                UPDATE devices
                SET recovery_code_hash = ?, recovery_payload_json = ?, recovery_updated_at = ?, recovery_state = 'backed_up'
                WHERE device_id = ? AND status = 'active'
                """,
                (recovery_code_hash, recovery_payload_json, now, device_id),
            )
            return cur.rowcount > 0

    def get_device_recovery(self, device_id: str, recovery_code_hash: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT device_id, device_label, recovery_payload_json, recovery_updated_at
                FROM devices
                WHERE device_id = ? AND recovery_code_hash = ? AND status = 'active'
                """,
                (device_id, recovery_code_hash),
            ).fetchone()
            return dict(row) if row else None

    def create_attachment(
        self,
        *,
        attachment_id: str | None = None,
        thread_id: str,
        device_id: str,
        filename: str,
        mime_type: str,
        plaintext_bytes: int,
        ciphertext_bytes: int,
        storage_relpath: str,
        encryption_json: str,
        note: str = "",
    ) -> dict[str, Any]:
        attachment_id = attachment_id or f"att-{uuid4().hex[:12]}"
        now = _utc_now()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO attachments(
                    attachment_id, thread_id, device_id, filename, mime_type, plaintext_bytes,
                    ciphertext_bytes, storage_relpath, encryption_json, note, created_at, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'stored')
                """,
                (
                    attachment_id,
                    thread_id,
                    device_id,
                    filename[:255],
                    mime_type[:255],
                    max(0, int(plaintext_bytes)),
                    max(0, int(ciphertext_bytes)),
                    storage_relpath,
                    encryption_json,
                    note[:1000],
                    now,
                ),
            )
            conn.execute(
                "UPDATE threads SET updated_at = ?, last_message_at = ? WHERE thread_id = ?",
                (now, now, thread_id),
            )
        return self.get_attachment(attachment_id, device_id)

    def get_attachment(self, attachment_id: str, device_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM attachments WHERE attachment_id = ? AND device_id = ?",
                (attachment_id, device_id),
            ).fetchone()
            return dict(row) if row else None

    def list_attachments(self, thread_id: str, device_id: str) -> list[dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM attachments
                WHERE thread_id = ? AND device_id = ?
                ORDER BY created_at ASC
                """,
                (thread_id, device_id),
            ).fetchall()
            return [dict(row) for row in rows]
