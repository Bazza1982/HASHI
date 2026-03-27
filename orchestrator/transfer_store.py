from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class TransferStore:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._init_schema()

    def _init_schema(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS transfers (
                transfer_id TEXT PRIMARY KEY,
                source_agent TEXT NOT NULL,
                source_instance TEXT NOT NULL,
                target_agent TEXT NOT NULL,
                target_instance TEXT NOT NULL,
                status TEXT NOT NULL,
                package_json TEXT NOT NULL,
                request_id TEXT,
                ack_text TEXT,
                error_code TEXT,
                error_text TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS transfer_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                transfer_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                details_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )
        self._conn.commit()

    def create_transfer(self, package: dict[str, Any], *, status: str) -> None:
        now = _utc_now()
        with self._lock:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO transfers(
                    transfer_id, source_agent, source_instance, target_agent, target_instance,
                    status, package_json, request_id, ack_text, error_code, error_text, created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, NULL, ?, ?)
                """,
                (
                    package["transfer_id"],
                    package["source_agent"],
                    package["source_instance"],
                    package["target_agent"],
                    package["target_instance"],
                    status,
                    json.dumps(package, ensure_ascii=True),
                    now,
                    now,
                ),
            )
            self._conn.commit()

    def update_transfer(
        self,
        transfer_id: str,
        *,
        status: str,
        request_id: str | None = None,
        ack_text: str | None = None,
        error_code: str | None = None,
        error_text: str | None = None,
    ) -> None:
        with self._lock:
            self._conn.execute(
                """
                UPDATE transfers
                SET status = ?,
                    request_id = COALESCE(?, request_id),
                    ack_text = COALESCE(?, ack_text),
                    error_code = COALESCE(?, error_code),
                    error_text = COALESCE(?, error_text),
                    updated_at = ?
                WHERE transfer_id = ?
                """,
                (status, request_id, ack_text, error_code, error_text, _utc_now(), transfer_id),
            )
            self._conn.commit()

    def update_package(self, transfer_id: str, package: dict[str, Any]) -> None:
        with self._lock:
            self._conn.execute(
                """
                UPDATE transfers
                SET package_json = ?,
                    updated_at = ?
                WHERE transfer_id = ?
                """,
                (json.dumps(package, ensure_ascii=True), _utc_now(), transfer_id),
            )
            self._conn.commit()

    def append_event(self, transfer_id: str, event_type: str, details: dict[str, Any] | None = None) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO transfer_events(transfer_id, event_type, details_json, created_at)
                VALUES(?, ?, ?, ?)
                """,
                (transfer_id, event_type, json.dumps(details or {}, ensure_ascii=True), _utc_now()),
            )
            self._conn.commit()

    def get_transfer(self, transfer_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM transfers WHERE transfer_id = ?", (transfer_id,)).fetchone()
            event_rows = self._conn.execute(
                "SELECT * FROM transfer_events WHERE transfer_id = ? ORDER BY id ASC",
                (transfer_id,),
            ).fetchall()
        if row is None:
            return None
        return {
            "transfer_id": row["transfer_id"],
            "source_agent": row["source_agent"],
            "source_instance": row["source_instance"],
            "target_agent": row["target_agent"],
            "target_instance": row["target_instance"],
            "status": row["status"],
            "package": json.loads(row["package_json"]),
            "request_id": row["request_id"],
            "ack_text": row["ack_text"],
            "error_code": row["error_code"],
            "error_text": row["error_text"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "events": [
                {
                    "id": event["id"],
                    "event_type": event["event_type"],
                    "details": json.loads(event["details_json"]),
                    "created_at": event["created_at"],
                }
                for event in event_rows
            ],
        }
