from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any

from .bridge_adapter import BridgeMemoryAdapter
from .config import AnattaConfig
from .models import DriveContribution, EmotionalAnnotation, TurnContext


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class AnattaMemoryStore:
    def __init__(self, bridge_adapter: BridgeMemoryAdapter, config: AnattaConfig):
        self.bridge_adapter = bridge_adapter
        if self.bridge_adapter.db_path is None:
            raise ValueError("BridgeMemoryAdapter must expose db_path for Anatta memory tables")
        self.db_path = self.bridge_adapter.db_path
        self.config = config
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS emotional_annotations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    bridge_row_type TEXT NOT NULL,
                    bridge_row_id INTEGER NOT NULL,
                    event_ts TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    source TEXT NOT NULL,
                    actor_role TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    intensity INTEGER NOT NULL CHECK (intensity BETWEEN 0 AND 10),
                    dominant_drives_json TEXT NOT NULL,
                    drive_contributions_json TEXT NOT NULL,
                    relationship_key TEXT,
                    tags_json TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    importance REAL NOT NULL DEFAULT 1.0,
                    archived INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS relationship_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    relationship_key TEXT NOT NULL,
                    event_ts TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    intensity INTEGER NOT NULL CHECK (intensity BETWEEN 0 AND 10),
                    drive_contributions_json TEXT NOT NULL,
                    metadata_json TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS derived_relationship_cache (
                    relationship_key TEXT PRIMARY KEY,
                    derived_summary_json TEXT NOT NULL,
                    computed_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS drive_registry (
                    drive_name TEXT PRIMARY KEY,
                    display_name TEXT NOT NULL,
                    min_value REAL NOT NULL,
                    max_value REAL NOT NULL,
                    default_decay REAL NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    config_json TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS anatta_meta (
                    key TEXT PRIMARY KEY,
                    value_json TEXT NOT NULL
                )
                """
            )
            registry = self.config.drive_registry()
            for name, cfg in registry.items():
                conn.execute(
                    """
                    INSERT OR REPLACE INTO drive_registry(
                        drive_name, display_name, min_value, max_value, default_decay, enabled, config_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        name,
                        str(cfg.get("display_name", name)),
                        float(cfg.get("min", 0.0)),
                        float(cfg.get("max", 100.0)),
                        float(cfg.get("default_decay", 0.1)),
                        1 if cfg.get("enabled", True) else 0,
                        json.dumps(cfg, ensure_ascii=False),
                    ),
                )
            conn.commit()

    def get_meta(self, key: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT value_json FROM anatta_meta WHERE key = ?", (key,)).fetchone()
        if not row:
            return None
        try:
            payload = json.loads(row["value_json"])
            return payload if isinstance(payload, dict) else None
        except Exception:
            return None

    def set_meta(self, key: str, value: dict[str, Any]) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO anatta_meta(key, value_json) VALUES (?, ?)",
                (key, json.dumps(value, ensure_ascii=False)),
            )
            conn.commit()

    def record_annotation(self, annotation: EmotionalAnnotation) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO emotional_annotations(
                    bridge_row_type, bridge_row_id, event_ts, created_at, source, actor_role,
                    event_type, summary, intensity, dominant_drives_json, drive_contributions_json,
                    relationship_key, tags_json, metadata_json, importance
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    annotation.bridge_row_type,
                    annotation.bridge_row_id,
                    annotation.event_ts.isoformat(),
                    annotation.created_at.isoformat(),
                    annotation.source,
                    annotation.actor_role,
                    annotation.event_type,
                    annotation.summary,
                    int(annotation.intensity),
                    json.dumps(annotation.dominant_drives, ensure_ascii=False),
                    json.dumps([self._serialize_contribution(c) for c in annotation.contributions], ensure_ascii=False),
                    annotation.relationship_key,
                    json.dumps(annotation.tags, ensure_ascii=False),
                    json.dumps(annotation.metadata, ensure_ascii=False),
                    float(annotation.importance),
                ),
            )
            if annotation.relationship_key:
                conn.execute(
                    """
                    INSERT INTO relationship_events(
                        relationship_key, event_ts, event_type, summary, intensity, drive_contributions_json, metadata_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        annotation.relationship_key,
                        annotation.event_ts.isoformat(),
                        annotation.event_type,
                        annotation.summary,
                        int(annotation.intensity),
                        json.dumps([self._serialize_contribution(c) for c in annotation.contributions], ensure_ascii=False),
                        json.dumps(annotation.metadata, ensure_ascii=False),
                    ),
                )
            conn.commit()
            return int(cur.lastrowid)

    def retrieve_relevant_annotations(self, turn_context: TurnContext, limit: int = 12) -> list[EmotionalAnnotation]:
        weights = self.config.retrieval_weights()
        policy = self.config.retrieval_policy()
        gate_floor = max(0.0, min(1.0, float(policy.get("intensity_semantic_gate_floor", 0.25))))
        candidates = self._fetch_candidate_rows(turn_context, limit=max(limit * 3, 24))
        real_annotation_count = self.count_real_annotations()
        scored: list[tuple[float, sqlite3.Row]] = []
        query_tokens = self._tokenize(turn_context.user_text)
        for row in candidates:
            summary_tokens = self._tokenize(self._row_search_text(row))
            semantic = self._overlap_score(query_tokens, summary_tokens)
            intensity = min(max(int(row["intensity"]), 0), 10) / 10.0
            gated_intensity = intensity * (gate_floor + ((1.0 - gate_floor) * semantic))
            rel_match = 1.0 if turn_context.relationship_key and row["relationship_key"] == turn_context.relationship_key else 0.0
            importance = max(float(row["importance"]), 0.0)
            recency = self._recency_decay(row["event_ts"])
            score = (
                weights["semantic_relevance"] * semantic
                + weights["normalized_intensity"] * gated_intensity
                + weights["relationship_match"] * rel_match
                + weights["importance"] * min(importance, 2.0) / 2.0
                + weights["recency_decay"] * recency
            )
            score *= self._bootstrap_decay_multiplier(row, real_annotation_count=real_annotation_count)
            scored.append((score, row))
        scored.sort(key=lambda item: item[0], reverse=True)
        annotations: list[EmotionalAnnotation] = []
        for score, row in scored[:limit]:
            annotation = self._row_to_annotation(row)
            annotation.metadata = {
                **annotation.metadata,
                "_retrieval_score": round(float(score), 6),
            }
            annotations.append(annotation)
        return annotations

    def count_real_annotations(self) -> int:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS count
                FROM emotional_annotations
                WHERE source != 'bootstrap'
                """
            ).fetchone()
        return int(row["count"]) if row else 0

    def get_relationship_events(self, relationship_key: str | None, limit: int = 24) -> list[dict[str, Any]]:
        if not relationship_key:
            return []
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT relationship_key, event_ts, event_type, summary, intensity, drive_contributions_json, metadata_json
                FROM relationship_events
                WHERE relationship_key = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (relationship_key, int(limit)),
            ).fetchall()
        return [dict(row) for row in rows]

    def compute_relationship_summary(self, relationship_key: str | None) -> dict[str, Any]:
        if not relationship_key:
            return {}
        events = self.get_relationship_events(relationship_key, limit=24)
        if not events:
            return {}
        trust_shift = 0.0
        care_signal = 0.0
        repair_signal = 0.0
        rupture_count = 0
        repair_count = 0
        for event in events:
            etype = (event.get("event_type") or "").strip().lower()
            intensity = min(max(int(event.get("intensity") or 0), 0), 10) / 10.0
            if etype == "care_bonding":
                trust_shift += intensity
                care_signal += intensity
            elif etype == "validation":
                trust_shift += intensity * 0.8
                care_signal += intensity * 0.45
            elif etype == "repair":
                trust_shift += intensity * 0.35
                repair_signal += intensity
            elif etype in {"rupture_risk", "betrayal", "boundary_crossing"}:
                trust_shift -= intensity
                rupture_count += 1
            if etype == "repair":
                repair_count += 1
        return {
            "relationship_key": relationship_key,
            "net_trust_shift": round(trust_shift, 4),
            "care_signal": round(care_signal, 4),
            "repair_signal": round(repair_signal, 4),
            "rupture_count": rupture_count,
            "repair_count": repair_count,
            "event_count": len(events),
        }

    def should_record(self, intensity: int, event_type: str) -> bool:
        policy = self.config.recording_policy()
        if intensity >= int(policy.get("minimum_intensity", 5)):
            return True
        always = {str(x).strip().lower() for x in policy.get("always_record_event_types", [])}
        return str(event_type).strip().lower() in always

    def has_bootstrap_profile(self, profile_name: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT 1
                FROM emotional_annotations
                WHERE source = 'bootstrap'
                  AND metadata_json LIKE ?
                LIMIT 1
                """,
                (f'%"{profile_name}"%',),
            ).fetchone()
        return row is not None

    def _fetch_candidate_rows(self, turn_context: TurnContext, limit: int) -> list[sqlite3.Row]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM emotional_annotations
                WHERE archived = 0
                ORDER BY id DESC
                LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
        return rows

    @staticmethod
    def _tokenize(text: str) -> set[str]:
        return {
            tok.lower()
            for tok in re.findall(r"[A-Za-z0-9]+|[\u4e00-\u9fff]", text or "")
            if tok.strip()
        }

    @staticmethod
    def _overlap_score(a: set[str], b: set[str]) -> float:
        if not a or not b:
            return 0.0
        overlap = len(a & b)
        union = len(a | b)
        return overlap / union if union else 0.0

    @staticmethod
    def _recency_decay(event_ts: str) -> float:
        try:
            event_dt = datetime.fromisoformat(event_ts)
        except Exception:
            return 0.0
        seconds = max((datetime.now(event_dt.tzinfo) - event_dt).total_seconds(), 0.0)
        day = 86400.0
        return max(0.0, 1.0 - min(seconds / (30.0 * day), 1.0))

    @staticmethod
    def _serialize_contribution(contribution: DriveContribution) -> dict[str, Any]:
        return {
            "source": contribution.source,
            "drive_delta": dict(contribution.drive_delta),
            "weight": float(contribution.weight),
            "rationale": contribution.rationale,
            "metadata": dict(contribution.metadata),
        }

    @staticmethod
    def _deserialize_contribution(payload: dict[str, Any]) -> DriveContribution:
        return DriveContribution(
            source=str(payload.get("source", "")),
            drive_delta={str(k): float(v) for k, v in dict(payload.get("drive_delta") or {}).items()},
            weight=float(payload.get("weight", 1.0)),
            rationale=str(payload.get("rationale", "")),
            metadata=dict(payload.get("metadata") or {}),
        )

    def _row_to_annotation(self, row: sqlite3.Row) -> EmotionalAnnotation:
        contributions = [
            self._deserialize_contribution(item)
            for item in json.loads(row["drive_contributions_json"] or "[]")
            if isinstance(item, dict)
        ]
        return EmotionalAnnotation(
            annotation_id=int(row["id"]),
            bridge_row_type=str(row["bridge_row_type"]),
            bridge_row_id=int(row["bridge_row_id"]),
            event_ts=datetime.fromisoformat(row["event_ts"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            source=str(row["source"]),
            actor_role=str(row["actor_role"]),
            event_type=str(row["event_type"]),
            summary=str(row["summary"]),
            intensity=int(row["intensity"]),
            dominant_drives=list(json.loads(row["dominant_drives_json"] or "[]")),
            contributions=contributions,
            relationship_key=row["relationship_key"],
            tags=list(json.loads(row["tags_json"] or "[]")),
            metadata=dict(json.loads(row["metadata_json"] or "{}")),
            importance=float(row["importance"]),
        )

    @staticmethod
    def _row_search_text(row: sqlite3.Row) -> str:
        parts = [
            str(row["summary"] or ""),
            str(row["event_type"] or ""),
            str(row["dominant_drives_json"] or ""),
            str(row["tags_json"] or ""),
        ]
        try:
            metadata = json.loads(row["metadata_json"] or "{}")
            if isinstance(metadata, dict):
                for key in ("summary", "user_text", "assistant_response", "source"):
                    value = metadata.get(key)
                    if value:
                        parts.append(str(value))
        except Exception:
            pass
        return "\n".join(parts)

    def _bootstrap_decay_multiplier(self, row: sqlite3.Row, *, real_annotation_count: int) -> float:
        if row["source"] != "bootstrap":
            return 1.0
        try:
            metadata = json.loads(row["metadata_json"] or "{}")
        except Exception:
            metadata = {}
        decay_turns = int(metadata.get("bootstrap_decay_turns", self.config.bootstrap_decay_turns()))
        half_life_days = int(metadata.get("bootstrap_half_life_days", self.config.bootstrap_half_life_days()))
        turn_factor = max(0.05, 1.0 - min(real_annotation_count / float(max(decay_turns, 1)), 0.95))
        age_factor = 1.0
        try:
            created_at = datetime.fromisoformat(row["created_at"])
            age_days = max((datetime.now(created_at.tzinfo) - created_at).total_seconds() / 86400.0, 0.0)
            age_factor = max(0.10, 0.5 ** (age_days / float(max(half_life_days, 1))))
        except Exception:
            age_factor = 1.0
        return turn_factor * age_factor
