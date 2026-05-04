"""SQLite state management for the HASHI wiki redesign pipeline."""

from __future__ import annotations

import json
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
    CREATE TABLE IF NOT EXISTS topic_registry (
        topic_id                   TEXT PRIMARY KEY,
        display                    TEXT NOT NULL,
        description                TEXT NOT NULL,
        topic_type                 TEXT NOT NULL DEFAULT 'concept',
        owner_domain               TEXT NOT NULL DEFAULT '',
        canonical_page_path        TEXT NOT NULL,
        aliases_json               TEXT NOT NULL DEFAULT '[]',
        status                     TEXT NOT NULL DEFAULT 'active',
        privacy_level              TEXT NOT NULL DEFAULT 'internal',
        quality_score              REAL,
        uncertainty_score          REAL,
        last_synthesized_at        TEXT,
        human_locked               INTEGER NOT NULL DEFAULT 0,
        ai_mutable                 INTEGER NOT NULL DEFAULT 1,
        merge_lineage_json         TEXT NOT NULL DEFAULT '[]',
        split_lineage_json         TEXT NOT NULL DEFAULT '[]',
        created_at                 TEXT NOT NULL,
        updated_at                 TEXT NOT NULL,
        created_by                 TEXT NOT NULL,
        promoted_from_candidate_id TEXT,
        review_note                TEXT NOT NULL DEFAULT ''
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS topic_candidate (
        candidate_id        TEXT PRIMARY KEY,
        proposed_topic_id   TEXT NOT NULL,
        display             TEXT NOT NULL,
        description         TEXT NOT NULL,
        topic_type          TEXT NOT NULL DEFAULT 'concept',
        owner_domain        TEXT NOT NULL DEFAULT '',
        aliases_json        TEXT NOT NULL DEFAULT '[]',
        evidence_ids_json   TEXT NOT NULL,
        source_terms_json   TEXT NOT NULL DEFAULT '[]',
        curator_reason      TEXT NOT NULL,
        recommended_action  TEXT NOT NULL,
        merge_target        TEXT,
        confidence          REAL NOT NULL,
        quality_score       REAL,
        uncertainty_score   REAL,
        privacy_level       TEXT NOT NULL DEFAULT 'internal',
        status              TEXT NOT NULL DEFAULT 'pending',
        created_at          TEXT NOT NULL,
        reviewed_at         TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS topic_claim (
        claim_id          TEXT PRIMARY KEY,
        topic_id          TEXT NOT NULL,
        claim             TEXT NOT NULL,
        section           TEXT NOT NULL,
        claim_type        TEXT NOT NULL,
        evidence_ids_json TEXT NOT NULL,
        confidence        REAL NOT NULL,
        status            TEXT NOT NULL DEFAULT 'active',
        created_at        TEXT NOT NULL,
        updated_at        TEXT NOT NULL,
        FOREIGN KEY(topic_id) REFERENCES topic_registry(topic_id)
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
    """
    CREATE INDEX IF NOT EXISTS idx_topic_registry_status
    ON topic_registry(status, topic_type, topic_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_topic_candidate_status
    ON topic_candidate(status, confidence, proposed_topic_id)
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
        if table not in {
            "classification_assignment",
            "classification_run",
            "run_state",
            "topic_registry",
            "topic_candidate",
            "topic_claim",
        }:
            raise ValueError(f"Unsupported table: {table}")
        return int(self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])

    def seed_topic_registry(self, topics: dict[str, dict[str, str]], *, created_by: str = "seed") -> None:
        """Seed current code-defined topics into the mutable runtime registry."""
        ts = _utc_now()
        with self.conn:
            self.conn.executemany(
                """
                INSERT INTO topic_registry(
                    topic_id,
                    display,
                    description,
                    topic_type,
                    canonical_page_path,
                    aliases_json,
                    created_at,
                    updated_at,
                    created_by
                )
                VALUES (?, ?, ?, ?, ?, '[]', ?, ?, ?)
                ON CONFLICT(topic_id) DO NOTHING
                """,
                [
                    (
                        topic_id,
                        meta["display"],
                        meta["desc"],
                        _infer_topic_type(topic_id),
                        f"10_GENERATED_TOPICS/{topic_id}.md",
                        ts,
                        ts,
                        created_by,
                    )
                    for topic_id, meta in topics.items()
                ],
            )

    def load_active_topics(self) -> dict[str, dict[str, str]]:
        """Return active runtime topics in the shape expected by classifier/page generation."""
        rows = self.conn.execute(
            """
            SELECT topic_id, display, description, topic_type
            FROM topic_registry
            WHERE status = 'active'
            ORDER BY topic_id
            """
        ).fetchall()
        return {
            row["topic_id"]: {
                "display": row["display"],
                "desc": row["description"],
                "topic_type": row["topic_type"],
            }
            for row in rows
        }

    def upsert_topic_candidate(
        self,
        *,
        candidate_id: str,
        proposed_topic_id: str,
        display: str,
        description: str,
        topic_type: str,
        evidence_ids: list[int],
        curator_reason: str,
        recommended_action: str,
        confidence: float,
        aliases: list[str] | None = None,
        source_terms: list[str] | None = None,
        merge_target: str | None = None,
        quality_score: float | None = None,
        uncertainty_score: float | None = None,
        privacy_level: str = "internal",
        status: str = "pending",
        created_at: str | None = None,
    ) -> None:
        ts = created_at or _utc_now()
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO topic_candidate(
                    candidate_id,
                    proposed_topic_id,
                    display,
                    description,
                    topic_type,
                    aliases_json,
                    evidence_ids_json,
                    source_terms_json,
                    curator_reason,
                    recommended_action,
                    merge_target,
                    confidence,
                    quality_score,
                    uncertainty_score,
                    privacy_level,
                    status,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(candidate_id) DO UPDATE SET
                    proposed_topic_id = excluded.proposed_topic_id,
                    display = excluded.display,
                    description = excluded.description,
                    topic_type = excluded.topic_type,
                    aliases_json = excluded.aliases_json,
                    evidence_ids_json = excluded.evidence_ids_json,
                    source_terms_json = excluded.source_terms_json,
                    curator_reason = excluded.curator_reason,
                    recommended_action = excluded.recommended_action,
                    merge_target = excluded.merge_target,
                    confidence = excluded.confidence,
                    quality_score = excluded.quality_score,
                    uncertainty_score = excluded.uncertainty_score,
                    privacy_level = excluded.privacy_level,
                    status = excluded.status
                """,
                (
                    candidate_id,
                    proposed_topic_id,
                    display,
                    description,
                    topic_type,
                    json.dumps(aliases or [], ensure_ascii=False),
                    json.dumps(evidence_ids, ensure_ascii=False),
                    json.dumps(source_terms or [], ensure_ascii=False),
                    curator_reason,
                    recommended_action,
                    merge_target,
                    confidence,
                    quality_score,
                    uncertainty_score,
                    privacy_level,
                    status,
                    ts,
                ),
            )

    def promote_topic_candidate(self, candidate_id: str, *, created_by: str = "ai_curator") -> bool:
        """Promote a reviewed AI topic candidate into the active topic registry."""
        row = self.conn.execute(
            "SELECT * FROM topic_candidate WHERE candidate_id = ?",
            (candidate_id,),
        ).fetchone()
        if row is None:
            return False
        if row["recommended_action"] != "promote":
            return False
        if row["privacy_level"] == "private_blocked" or float(row["confidence"]) < 0.7:
            return False

        topic_id = row["proposed_topic_id"]
        existing = self.conn.execute(
            "SELECT human_locked FROM topic_registry WHERE topic_id = ?",
            (topic_id,),
        ).fetchone()
        if existing is not None and int(existing["human_locked"]):
            return False

        ts = _utc_now()
        evidence_ids = json.loads(row["evidence_ids_json"])
        aliases_json = row["aliases_json"] or "[]"
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO topic_registry(
                    topic_id,
                    display,
                    description,
                    topic_type,
                    canonical_page_path,
                    aliases_json,
                    status,
                    privacy_level,
                    quality_score,
                    uncertainty_score,
                    created_at,
                    updated_at,
                    created_by,
                    promoted_from_candidate_id,
                    review_note
                )
                VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(topic_id) DO UPDATE SET
                    display = excluded.display,
                    description = excluded.description,
                    topic_type = excluded.topic_type,
                    canonical_page_path = excluded.canonical_page_path,
                    aliases_json = excluded.aliases_json,
                    status = 'active',
                    privacy_level = excluded.privacy_level,
                    quality_score = excluded.quality_score,
                    uncertainty_score = excluded.uncertainty_score,
                    updated_at = excluded.updated_at,
                    promoted_from_candidate_id = excluded.promoted_from_candidate_id,
                    review_note = excluded.review_note
                """,
                (
                    topic_id,
                    row["display"],
                    row["description"],
                    row["topic_type"],
                    f"10_GENERATED_TOPICS/{topic_id}.md",
                    aliases_json,
                    row["privacy_level"],
                    row["quality_score"],
                    row["uncertainty_score"],
                    ts,
                    ts,
                    created_by,
                    candidate_id,
                    row["curator_reason"],
                ),
            )
            self.conn.execute(
                """
                UPDATE topic_candidate
                SET status = 'promoted', reviewed_at = ?
                WHERE candidate_id = ?
                """,
                (ts, candidate_id),
            )
            self.conn.executemany(
                """
                INSERT INTO classification_assignment(
                    consolidated_id, topic_id, confidence, classified_at, classifier_model, status
                )
                VALUES (?, ?, ?, ?, 'topic-discovery/promoted', 'ok')
                ON CONFLICT(consolidated_id, topic_id) DO UPDATE SET
                    confidence = excluded.confidence,
                    classified_at = excluded.classified_at,
                    classifier_model = excluded.classifier_model,
                    status = 'ok'
                """,
                [
                    (
                        int(evidence_id),
                        topic_id,
                        float(row["confidence"]),
                        ts,
                    )
                    for evidence_id in evidence_ids
                ],
            )
        return True

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


def _infer_topic_type(topic_id: str) -> str:
    lower = topic_id.lower()
    if "workflow" in lower:
        return "workflow"
    if "research" in lower or "carbon" in lower:
        return "research"
    if "remote" in lower or "platform" in lower:
        return "system"
    if "wiki" in lower or "memory" in lower or "architecture" in lower or "security" in lower:
        return "system"
    return "concept"
