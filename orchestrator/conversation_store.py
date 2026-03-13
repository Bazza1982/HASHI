from __future__ import annotations
import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ConversationStore:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._init_schema()

    def _init_schema(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS threads (
                thread_id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                created_by TEXT NOT NULL,
                participants_json TEXT NOT NULL,
                status TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS messages (
                message_id TEXT PRIMARY KEY,
                thread_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                intent TEXT,
                from_agent TEXT NOT NULL,
                to_agent TEXT NOT NULL,
                in_reply_to TEXT,
                payload_json TEXT NOT NULL,
                status TEXT NOT NULL,
                result_text TEXT,
                error_text TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(thread_id) REFERENCES threads(thread_id)
            );

            CREATE TABLE IF NOT EXISTS permissions_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id TEXT NOT NULL,
                decision TEXT NOT NULL,
                reason TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS spawn_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                request_message_id TEXT NOT NULL,
                target_agent TEXT NOT NULL,
                result TEXT NOT NULL,
                details_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )
        self._conn.commit()

    def ensure_thread(self, thread_id: str, created_by: str, participants: list[str], status: str = "open") -> None:
        now = _utc_now()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO threads(thread_id, created_at, created_by, participants_json, status)
                VALUES(?, ?, ?, ?, ?)
                ON CONFLICT(thread_id) DO NOTHING
                """,
                (thread_id, now, created_by, json.dumps(participants, ensure_ascii=True), status),
            )
            self._conn.commit()

    def update_thread_status(self, thread_id: str, status: str) -> None:
        with self._lock:
            self._conn.execute("UPDATE threads SET status = ? WHERE thread_id = ?", (status, thread_id))
            self._conn.commit()

    def save_message(
        self,
        payload: dict[str, Any],
        *,
        status: str,
        result_text: str | None = None,
        error_text: str | None = None,
    ) -> None:
        now = _utc_now()
        with self._lock:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO messages(
                    message_id, thread_id, kind, intent, from_agent, to_agent,
                    in_reply_to, payload_json, status, result_text, error_text,
                    created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    payload["message_id"],
                    payload["thread_id"],
                    payload["kind"],
                    payload.get("intent"),
                    payload["from_agent"],
                    payload["to_agent"],
                    payload.get("in_reply_to"),
                    json.dumps(payload, ensure_ascii=True),
                    status,
                    result_text,
                    error_text,
                    now,
                    now,
                ),
            )
            self._conn.commit()

    def update_message_status(self, message_id: str, status: str, *, result_text: str | None = None, error_text: str | None = None) -> None:
        with self._lock:
            self._conn.execute(
                """
                UPDATE messages
                SET status = ?, result_text = COALESCE(?, result_text), error_text = COALESCE(?, error_text), updated_at = ?
                WHERE message_id = ?
                """,
                (status, result_text, error_text, _utc_now(), message_id),
            )
            self._conn.commit()

    def record_permission_audit(self, message_id: str, decision: str, reason: str) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO permissions_audit(message_id, decision, reason, created_at)
                VALUES(?, ?, ?, ?)
                """,
                (message_id, decision, reason, _utc_now()),
            )
            self._conn.commit()

    def get_message(self, message_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM messages WHERE message_id = ?", (message_id,)).fetchone()
        if row is None:
            return None
        return self._row_to_message(row)

    def get_thread(self, thread_id: str) -> dict[str, Any] | None:
        with self._lock:
            thread_row = self._conn.execute("SELECT * FROM threads WHERE thread_id = ?", (thread_id,)).fetchone()
            message_rows = self._conn.execute(
                "SELECT * FROM messages WHERE thread_id = ? ORDER BY created_at ASC, message_id ASC",
                (thread_id,),
            ).fetchall()
        if thread_row is None:
            return None
        return {
            "thread_id": thread_row["thread_id"],
            "created_at": thread_row["created_at"],
            "created_by": thread_row["created_by"],
            "participants": json.loads(thread_row["participants_json"]),
            "status": thread_row["status"],
            "messages": [self._row_to_message(row) for row in message_rows],
        }

    def _row_to_message(self, row: sqlite3.Row) -> dict[str, Any]:
        payload = json.loads(row["payload_json"])
        payload["status"] = row["status"]
        payload["result_text"] = row["result_text"]
        payload["error_text"] = row["error_text"]
        payload["created_at"] = row["created_at"]
        payload["updated_at"] = row["updated_at"]
        return payload
