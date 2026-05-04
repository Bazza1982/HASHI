"""SQLite state management for the HASHI wiki redesign pipeline."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Iterable

if TYPE_CHECKING:
    from .classifier import ClassificationAssignment
    from .fetcher import MemoryRecord


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

    def existing_completed_runs(self, consolidated_ids: Iterable[int]) -> set[int]:
        ids = list(consolidated_ids)
        if not ids:
            return set()
        placeholders = ",".join("?" for _ in ids)
        rows = self.conn.execute(
            f"""
            SELECT consolidated_id
            FROM classification_run
            WHERE consolidated_id IN ({placeholders})
              AND status IN ('ok', 'skipped', 'redacted')
            """,
            ids,
        ).fetchall()
        return {int(row["consolidated_id"]) for row in rows}

    def record_skipped_runs(
        self,
        records: Iterable["MemoryRecord"],
        *,
        batch_id: str,
        status: str,
        classified_at: str | None = None,
    ) -> None:
        if status not in {"skipped", "redacted", "failed"}:
            raise ValueError(f"Unsupported run status: {status}")
        ts = classified_at or _utc_now()
        with self.conn:
            self.conn.executemany(
                """
                INSERT INTO classification_run(
                    consolidated_id, agent_id, batch_id, status, classified_at
                )
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(consolidated_id) DO UPDATE SET
                    batch_id = excluded.batch_id,
                    status = excluded.status,
                    classified_at = excluded.classified_at
                """,
                [(record.id, record.agent_id, batch_id, status, ts) for record in records],
            )

    def record_assignments(
        self,
        records: Iterable["MemoryRecord"],
        assignments: Iterable["ClassificationAssignment"],
        *,
        batch_id: str,
        classifier_model: str,
        classified_at: str | None = None,
    ) -> None:
        record_by_id = {record.id: record for record in records}
        assignment_list = list(assignments)
        missing = [a.consolidated_id for a in assignment_list if a.consolidated_id not in record_by_id]
        if missing:
            raise ValueError(f"Assignments contain unknown consolidated ids: {missing}")
        assigned_ids = {assignment.consolidated_id for assignment in assignment_list}
        missing_records = [record for record in record_by_id.values() if record.id not in assigned_ids]
        ts = classified_at or _utc_now()
        with self.conn:
            for assignment in assignment_list:
                record = record_by_id[assignment.consolidated_id]
                self.conn.execute(
                    """
                    INSERT INTO classification_run(
                        consolidated_id, agent_id, batch_id, status, classified_at
                    )
                    VALUES (?, ?, ?, 'ok', ?)
                    ON CONFLICT(consolidated_id) DO UPDATE SET
                        batch_id = excluded.batch_id,
                        status = excluded.status,
                        classified_at = excluded.classified_at
                    """,
                    (record.id, record.agent_id, batch_id, ts),
                )
                self.conn.execute(
                    "DELETE FROM classification_assignment WHERE consolidated_id = ?",
                    (record.id,),
                )
                self.conn.executemany(
                    """
                    INSERT INTO classification_assignment(
                        consolidated_id, topic_id, confidence, classified_at, classifier_model, status
                    )
                    VALUES (?, ?, ?, ?, ?, 'ok')
                    """,
                    [
                        (
                            record.id,
                            topic_id,
                            assignment.confidence,
                            ts,
                            classifier_model,
                        )
                        for topic_id in assignment.topics
                    ],
                )
            for record in missing_records:
                self.conn.execute(
                    """
                    INSERT INTO classification_run(
                        consolidated_id, agent_id, batch_id, status, classified_at
                    )
                    VALUES (?, ?, ?, 'failed', ?)
                    ON CONFLICT(consolidated_id) DO UPDATE SET
                        batch_id = excluded.batch_id,
                        status = excluded.status,
                        classified_at = excluded.classified_at
                    """,
                    (record.id, record.agent_id, batch_id, ts),
                )
                self.conn.execute(
                    "DELETE FROM classification_assignment WHERE consolidated_id = ?",
                    (record.id,),
                )

    def advance_watermark(self, source_ids: Iterable[int] | None = None) -> int:
        current = self.get_last_classified_id()
        if source_ids is not None:
            ordered_ids = sorted({int(source_id) for source_id in source_ids if int(source_id) > current})
            if not ordered_ids:
                return current
            placeholders = ",".join("?" for _ in ordered_ids)
            rows = self.conn.execute(
                f"""
                SELECT consolidated_id, status
                FROM classification_run
                WHERE consolidated_id IN ({placeholders})
                """,
                ordered_ids,
            ).fetchall()
            status_by_id = {int(row["consolidated_id"]): str(row["status"]) for row in rows}
            advanced_to = current
            for source_id in ordered_ids:
                if status_by_id.get(source_id) not in {"ok", "skipped", "redacted"}:
                    break
                advanced_to = source_id
            if advanced_to != current:
                self.set_run_state("last_classified_id", str(advanced_to))
            return advanced_to

        rows = self.conn.execute(
            """
            SELECT consolidated_id, status
            FROM classification_run
            WHERE consolidated_id > ?
            ORDER BY consolidated_id ASC
            """,
            (current,),
        ).fetchall()
        next_expected = current + 1
        advanced_to = current
        for row in rows:
            consolidated_id = int(row["consolidated_id"])
            status = str(row["status"])
            if consolidated_id != next_expected:
                break
            if status not in {"ok", "skipped", "redacted"}:
                break
            advanced_to = consolidated_id
            next_expected += 1
        if advanced_to != current:
            self.set_run_state("last_classified_id", str(advanced_to))
        return advanced_to


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
