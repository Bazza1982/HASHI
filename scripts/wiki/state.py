"""SQLite state management for the HASHI wiki redesign pipeline."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterable


SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS classification_assignment (
        consolidated_id  INTEGER NOT NULL,
        topic_id         TEXT    NOT NULL,
        confidence       REAL    NOT NULL DEFAULT 1.0,
        classified_at    TEXT    NOT NULL,
        classifier_model TEXT    NOT NULL,
        status           TEXT    NOT NULL DEFAULT 'ok',
        PRIMARY KEY (consolidated_id, topic_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS classification_run (
        consolidated_id  INTEGER PRIMARY KEY,
        agent_id         TEXT    NOT NULL,
        batch_id         TEXT    NOT NULL,
        status           TEXT    NOT NULL,
        classified_at    TEXT    NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS run_state (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_classification_assignment_topic
    ON classification_assignment(topic_id, confidence, consolidated_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_classification_run_status
    ON classification_run(status, consolidated_id)
    """,
)


class WikiState:
    """Small wrapper around the wiki classifier state database."""

    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "WikiState":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def init_schema(self) -> None:
        with self.conn:
            for statement in SCHEMA_STATEMENTS:
                self.conn.execute(statement)
            self.conn.execute(
                "INSERT OR IGNORE INTO run_state(key, value) VALUES('last_classified_id', '0')"
            )

    def get_run_state(self, key: str, default: str | None = None) -> str | None:
        row = self.conn.execute("SELECT value FROM run_state WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set_run_state(self, key: str, value: str) -> None:
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO run_state(key, value)
                VALUES(?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, value),
            )

    def get_last_classified_id(self) -> int:
        value = self.get_run_state("last_classified_id", "0")
        return int(value or "0")

    def count_rows(self, table: str) -> int:
        if table not in {"classification_assignment", "classification_run", "run_state"}:
            raise ValueError(f"Unsupported table: {table}")
        return int(self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])

    def existing_runs(self, consolidated_ids: Iterable[int]) -> set[int]:
        ids = list(consolidated_ids)
        if not ids:
            return set()
        placeholders = ",".join("?" for _ in ids)
        rows = self.conn.execute(
            f"SELECT consolidated_id FROM classification_run WHERE consolidated_id IN ({placeholders})",
            ids,
        ).fetchall()
        return {int(row["consolidated_id"]) for row in rows}
