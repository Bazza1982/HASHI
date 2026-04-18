from __future__ import annotations

import json
import math
import re
import secrets
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from orchestrator.ticket_manager import detect_instance


ULID_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _now_event_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip()).lower()


def _to_json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=True, sort_keys=True)


def _from_json(raw: str | None, default: Any) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except Exception:
        return default


def _parse_iso_datetime(raw: str | None) -> datetime | None:
    token = str(raw or "").strip()
    if not token:
        return None
    try:
        return datetime.fromisoformat(token)
    except Exception:
        return None


def _safe_ratio(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return float(numerator) / float(denominator)


def _wilson_lower_bound(successes: int, total: int, z: float = 1.96) -> float:
    if total <= 0:
        return 0.0
    phat = float(successes) / float(total)
    z2 = z * z
    denom = 1.0 + z2 / total
    center = phat + z2 / (2.0 * total)
    margin = z * math.sqrt((phat * (1.0 - phat) + z2 / (4.0 * total)) / total)
    return max(0.0, min(1.0, (center - margin) / denom))


def generate_habit_id(instance: str, agent_id: str) -> str:
    """Generate a sortable instance-scoped ULID-like habit id."""
    ts_ms = int(time.time() * 1000)
    random_bits = secrets.randbits(80)
    value = (ts_ms << 80) | random_bits
    chars: list[str] = []
    for _ in range(26):
        chars.append(ULID_ALPHABET[value & 0x1F])
        value >>= 5
    ulid = "".join(reversed(chars))
    return f"{instance}_{agent_id}_{ulid}"


def generate_shared_pattern_id(instance: str) -> str:
    ts_ms = int(time.time() * 1000)
    random_bits = secrets.randbits(80)
    value = (ts_ms << 80) | random_bits
    chars: list[str] = []
    for _ in range(26):
        chars.append(ULID_ALPHABET[value & 0x1F])
        value >>= 5
    ulid = "".join(reversed(chars))
    return f"{instance}_shared_{ulid}"


def infer_task_type(prompt: str, source: str = "", summary: str = "") -> str | None:
    corpus = _normalize_text(" ".join(part for part in (prompt, source, summary) if part))
    if not corpus:
        return None

    hchat_terms = (
        "hchat", "lily", "shared context", "shared memory", "handoff",
        "cross-agent", "cross instance", "coordination", "mailbox",
        "上下文", "共享记忆", "共享上下文", "发消息给", "找小蕾", "问小蕾", "跨 agent",
    )
    cron_terms = (
        "cron", "crontab", "scheduled", "scheduler", "heartbeat", "job",
        "systemd", "timer", "autostart", "service", "定时任务", "计划任务",
        "自动执行", "守护进程", "调度",
    )

    if any(term in corpus for term in hchat_terms):
        return "coordination_hchat"
    if any(term in corpus for term in cron_terms):
        return "scheduling_cron"
    return None


@dataclass
class RetrievedHabit:
    habit_id: str
    habit_type: str
    instruction: str
    score: float
    title: str | None = None
    task_type: str | None = None
    origin: str = "local"
    scope: str | None = None


@dataclass
class HabitFeedbackResult:
    request_id: str
    sentiment: str
    updated_events: int
    updated_habits: list[str]


@dataclass
class HabitNightlyReview:
    agent_id: str
    lookback_days: int
    total_habits: int
    active_habits: int
    candidate_habits: int
    paused_habits: int
    disabled_habits: int
    reviewed_habits: int
    changed_habits: list[str]
    promoted: list[str]
    paused: list[str]
    disabled: list[str]
    decayed: list[str]
    recommendations: list[str]
    summary: str


@dataclass
class HabitRecommendation:
    agent_id: str
    habit_id: str
    title: str
    task_type: str | None
    status: str
    recommendation_type: str
    confidence: float
    helpful_recent: int
    harmful_recent: int
    ignored_recent: int
    triggered_recent: int
    summary: str


@dataclass
class HabitCopyRecommendation:
    recommendation_id: int
    source_agent_id: str
    source_habit_id: str
    source_title: str
    source_agent_class: str
    target_agent_id: str
    target_agent_class: str
    task_type: str | None
    status: str
    confidence: float
    helpful_recent: int
    harmful_recent: int
    triggered_recent: int
    summary: str
    generated_at: str
    reviewed_by: str | None = None
    reviewed_at: str | None = None
    review_note: str | None = None
    copied_habit_id: str | None = None


@dataclass
class SharedPattern:
    shared_pattern_id: str
    kind: str
    status: str
    title: str
    habit_type: str
    instruction: str
    rationale: str | None
    task_type: str | None
    target_agent_class: str
    owner: str
    confidence: float
    helpful_recent: int
    harmful_recent: int
    triggered_recent: int
    source_agent_id: str | None
    source_habit_id: str | None
    created_at: str
    updated_at: str
    promoted_by: str | None = None
    promoted_at: str | None = None
    retired_by: str | None = None
    retired_at: str | None = None
    governance_note: str | None = None


@dataclass
class HabitRecommendationReport:
    generated_at: str
    generated_by: str
    lookback_days: int
    agents_considered: int
    habits_considered: int
    recommendations: list[HabitRecommendation]
    copy_recommendations: list[HabitCopyRecommendation]
    shared_patterns: list[SharedPattern]
    agent_summaries: list[dict[str, Any]]
    task_family_summaries: list[dict[str, Any]]
    class_summaries: list[dict[str, Any]]
    backend_summaries: list[dict[str, Any]]
    timestamp_source_summaries: list[dict[str, Any]]
    markdown: str
    json_path: Path
    markdown_path: Path
    dashboard_json_path: Path
    dashboard_markdown_path: Path


class HabitStore:
    HARD_RETRIEVAL_LIMIT = 3
    MAX_DO = 2
    MAX_AVOID = 1
    CANDIDATE_BASE_CONFIDENCE = 0.35
    CANDIDATE_PROMOTION_HELPFUL_MIN = 2
    CANDIDATE_DISABLE_HARMFUL_MIN = 2
    MIN_SAMPLE_FOR_COPY_RECOMMENDATION = 10
    MIN_CONFIDENCE_FOR_COPY_RECOMMENDATION = 0.72
    MAX_COPY_TARGETS_PER_HABIT = 3
    ACTIVE_STALE_DAYS = 21
    CANDIDATE_STALE_DAYS = 14
    DECAY_STEP = 0.08
    REPORT_DIRNAME = "habit_reports"
    COPY_APPROVAL_STATUS_PENDING = "pending"
    COPY_APPROVAL_STATUS_APPROVED = "approved"
    COPY_APPROVAL_STATUS_APPLIED = "applied"
    COPY_APPROVAL_STATUS_REJECTED = "rejected"
    COPY_APPROVAL_STATUS_OBSOLETE = "obsolete"
    SHARED_PATTERN_STATUS_ACTIVE = "active"
    SHARED_PATTERN_STATUS_RETIRED = "retired"
    SHARED_PATTERN_KIND_PATTERN = "pattern"
    SHARED_PATTERN_KIND_PROTOCOL = "protocol"
    MIN_HELPFUL_FOR_SHARED_PROMOTION = 10
    MIN_TRIGGERED_FOR_SHARED_PROMOTION = 10
    MIN_CONFIDENCE_FOR_SHARED_PROMOTION = 0.72

    def __init__(self, workspace_dir: Path, project_root: Path, agent_id: str, agent_class: str | None):
        self.workspace_dir = workspace_dir
        self.project_root = project_root
        self.agent_id = agent_id
        self.agent_class = (agent_class or "general").strip().lower()
        self.instance = detect_instance(project_root)
        self.db_path = workspace_dir / "habits.sqlite"
        self.eval_db_path = project_root / "workspaces" / "lily" / "habit_evaluation.sqlite"
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _connect_eval(self) -> sqlite3.Connection:
        self.eval_db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.eval_db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        self._init_eval_schema(conn)
        return conn

    @classmethod
    def _connect_eval_db(cls, eval_db_path: Path) -> sqlite3.Connection:
        conn = sqlite3.connect(eval_db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        cls._init_eval_schema(conn)
        return conn

    def _init_db(self):
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS habits (
                    habit_id            TEXT PRIMARY KEY,
                    version             INTEGER NOT NULL DEFAULT 1,
                    agent_id            TEXT NOT NULL,
                    agent_class         TEXT NOT NULL DEFAULT 'general',
                    status              TEXT NOT NULL DEFAULT 'active',
                    enabled             INTEGER NOT NULL DEFAULT 1,
                    habit_type          TEXT NOT NULL,
                    title               TEXT,
                    instruction         TEXT NOT NULL,
                    rationale           TEXT,
                    scope               TEXT NOT NULL DEFAULT 'agent_local',
                    task_type           TEXT,
                    trigger_json        TEXT NOT NULL DEFAULT '{}',
                    source              TEXT NOT NULL DEFAULT 'runtime',
                    confidence          REAL NOT NULL DEFAULT 0.5,
                    times_triggered     INTEGER NOT NULL DEFAULT 0,
                    times_applied       INTEGER NOT NULL DEFAULT 0,
                    times_helpful       INTEGER NOT NULL DEFAULT 0,
                    times_harmful       INTEGER NOT NULL DEFAULT 0,
                    created_at          TEXT NOT NULL,
                    updated_at          TEXT NOT NULL,
                    last_triggered_at   TEXT,
                    last_helpful_at     TEXT,
                    copied_from_habit_id TEXT,
                    copied_from_agent_id TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS habit_state_changes (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    habit_id    TEXT NOT NULL,
                    change_type TEXT NOT NULL,
                    old_value   TEXT,
                    new_value   TEXT,
                    reason      TEXT,
                    changed_at  TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_habits_status ON habits(status, enabled)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_habits_agent_task ON habits(agent_id, task_type)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_habit_changes_habit ON habit_state_changes(habit_id, changed_at)"
            )
            self._ensure_column(conn, "habits", "copied_from_habit_id", "TEXT")
            self._ensure_column(conn, "habits", "copied_from_agent_id", "TEXT")
        with self._connect_eval():
            pass

    @classmethod
    def _init_eval_schema(cls, conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS habit_events (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                version         INTEGER NOT NULL DEFAULT 1,
                instance        TEXT    NOT NULL,
                agent_id        TEXT    NOT NULL,
                habit_id        TEXT    NOT NULL,
                request_id      TEXT,
                task_type       TEXT,
                triggered       INTEGER NOT NULL DEFAULT 0,
                applied         INTEGER NOT NULL DEFAULT 0,
                helpful         INTEGER,
                harmful         INTEGER,
                ignored         INTEGER NOT NULL DEFAULT 0,
                context_summary TEXT,
                feedback_text   TEXT,
                feedback_ts     TEXT,
                ts              TEXT    NOT NULL,
                ts_source       TEXT    NOT NULL DEFAULT 'native',
                UNIQUE(instance, agent_id, habit_id, ts)
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_habit_events_agent_ts
            ON habit_events(instance, agent_id, ts)
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_habit_events_habit_ts
            ON habit_events(habit_id, ts)
            """
        )
        cls._ensure_column(conn, "habit_events", "request_id", "TEXT")
        cls._ensure_column(conn, "habit_events", "feedback_text", "TEXT")
        cls._ensure_column(conn, "habit_events", "feedback_ts", "TEXT")
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_habit_events_request
            ON habit_events(instance, agent_id, request_id, ts)
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS habit_copy_recommendations (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                instance            TEXT NOT NULL,
                source_agent_id     TEXT NOT NULL,
                source_agent_class  TEXT NOT NULL DEFAULT 'general',
                source_habit_id     TEXT NOT NULL,
                target_agent_id     TEXT NOT NULL,
                target_agent_class  TEXT NOT NULL DEFAULT 'general',
                task_type           TEXT,
                confidence          REAL NOT NULL DEFAULT 0.0,
                helpful_recent      INTEGER NOT NULL DEFAULT 0,
                harmful_recent      INTEGER NOT NULL DEFAULT 0,
                triggered_recent    INTEGER NOT NULL DEFAULT 0,
                generated_at        TEXT NOT NULL,
                summary             TEXT NOT NULL,
                status              TEXT NOT NULL DEFAULT 'pending',
                reviewed_by         TEXT,
                reviewed_at         TEXT,
                review_note         TEXT,
                applied_at          TEXT,
                copied_habit_id     TEXT,
                UNIQUE(instance, source_agent_id, source_habit_id, target_agent_id)
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_habit_copy_recommendations_status
            ON habit_copy_recommendations(instance, status, generated_at)
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_habit_copy_recommendations_target
            ON habit_copy_recommendations(instance, target_agent_id, status)
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS shared_patterns (
                id                  TEXT PRIMARY KEY,
                instance            TEXT NOT NULL,
                kind                TEXT NOT NULL DEFAULT 'pattern',
                status              TEXT NOT NULL DEFAULT 'active',
                source_agent_id     TEXT,
                source_habit_id     TEXT,
                target_agent_class  TEXT NOT NULL DEFAULT 'general',
                owner               TEXT NOT NULL DEFAULT 'lily',
                title               TEXT NOT NULL,
                habit_type          TEXT NOT NULL,
                instruction         TEXT NOT NULL,
                rationale           TEXT,
                task_type           TEXT,
                trigger_json        TEXT NOT NULL DEFAULT '{}',
                scope               TEXT NOT NULL DEFAULT 'shared_pattern',
                confidence          REAL NOT NULL DEFAULT 0.0,
                times_triggered     INTEGER NOT NULL DEFAULT 0,
                times_applied       INTEGER NOT NULL DEFAULT 0,
                times_helpful       INTEGER NOT NULL DEFAULT 0,
                times_harmful       INTEGER NOT NULL DEFAULT 0,
                last_triggered_at   TEXT,
                last_helpful_at     TEXT,
                governance_note     TEXT,
                promoted_by         TEXT,
                promoted_at         TEXT,
                retired_by          TEXT,
                retired_at          TEXT,
                created_at          TEXT NOT NULL,
                updated_at          TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_shared_patterns_status
            ON shared_patterns(instance, status, kind, target_agent_class, updated_at)
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS shared_pattern_changes (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                shared_pattern_id   TEXT NOT NULL,
                change_type         TEXT NOT NULL,
                old_value           TEXT,
                new_value           TEXT,
                reason              TEXT,
                changed_by          TEXT,
                changed_at          TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_shared_pattern_changes_pattern
            ON shared_pattern_changes(shared_pattern_id, changed_at)
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS user_signals (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                instance            TEXT NOT NULL,
                agent_id            TEXT NOT NULL,
                signal              TEXT NOT NULL,
                comment             TEXT,
                context             TEXT NOT NULL,
                ts                  TEXT NOT NULL,
                processed           INTEGER NOT NULL DEFAULT 0,
                processed_at        TEXT,
                habit_ids_generated TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_user_signals_agent_processed
            ON user_signals(instance, agent_id, processed, ts)
            """
        )

    @staticmethod
    def _ensure_column(conn: sqlite3.Connection, table: str, column: str, column_sql: str) -> None:
        existing = {
            str(row["name"]).strip().lower()
            for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if column.lower() not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_sql}")

    def _is_shared_pattern_id(self, item_id: str | None) -> bool:
        token = str(item_id or "").strip()
        return token.startswith(f"{self.instance}_shared_")

    @staticmethod
    def _record_shared_pattern_change(
        eval_conn: sqlite3.Connection,
        *,
        shared_pattern_id: str,
        change_type: str,
        old_value: str | None,
        new_value: str | None,
        reason: str | None,
        changed_by: str | None,
    ) -> None:
        eval_conn.execute(
            """
            INSERT INTO shared_pattern_changes (
                shared_pattern_id, change_type, old_value, new_value, reason, changed_by, changed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (shared_pattern_id, change_type, old_value, new_value, reason, changed_by, _now_event_iso()),
        )

    def upsert_habit(
        self,
        *,
        habit_id: str | None = None,
        habit_type: str,
        instruction: str,
        title: str | None = None,
        rationale: str | None = None,
        task_type: str | None = None,
        trigger: dict[str, Any] | None = None,
        confidence: float = 0.5,
        status: str = "active",
        enabled: bool = True,
        source: str = "runtime",
        scope: str = "agent_local",
        copied_from_habit_id: str | None = None,
        copied_from_agent_id: str | None = None,
    ) -> str:
        now = _now_iso()
        resolved_habit_id = habit_id or generate_habit_id(self.instance, self.agent_id)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT habit_id FROM habits WHERE habit_id = ?",
                (resolved_habit_id,),
            ).fetchone()
            if row:
                conn.execute(
                    """
                    UPDATE habits
                    SET habit_type = ?, title = ?, instruction = ?, rationale = ?,
                        task_type = ?, trigger_json = ?, confidence = ?, status = ?,
                        enabled = ?, source = ?, scope = ?, copied_from_habit_id = ?,
                        copied_from_agent_id = ?, updated_at = ?
                    WHERE habit_id = ?
                    """,
                    (
                        habit_type,
                        title,
                        instruction.strip(),
                        rationale,
                        task_type,
                        _to_json(trigger or {}),
                        confidence,
                        status,
                        1 if enabled else 0,
                        source,
                        scope,
                        copied_from_habit_id,
                        copied_from_agent_id,
                        now,
                        resolved_habit_id,
                    ),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO habits (
                        habit_id, agent_id, agent_class, status, enabled, habit_type,
                        title, instruction, rationale, scope, task_type, trigger_json,
                        source, confidence, created_at, updated_at,
                        copied_from_habit_id, copied_from_agent_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        resolved_habit_id,
                        self.agent_id,
                        self.agent_class,
                        status,
                        1 if enabled else 0,
                        habit_type,
                        title,
                        instruction.strip(),
                        rationale,
                        scope,
                        task_type,
                        _to_json(trigger or {}),
                        source,
                        confidence,
                        now,
                        now,
                        copied_from_habit_id,
                        copied_from_agent_id,
                    ),
                )
        return resolved_habit_id

    def record_state_change(
        self,
        *,
        habit_id: str,
        change_type: str,
        old_value: str | None,
        new_value: str | None,
        reason: str | None = None,
        conn: sqlite3.Connection | None = None,
    ):
        if conn is None:
            with self._connect() as managed_conn:
                self.record_state_change(
                    habit_id=habit_id,
                    change_type=change_type,
                    old_value=old_value,
                    new_value=new_value,
                    reason=reason,
                    conn=managed_conn,
                )
            return
        conn.execute(
            """
            INSERT INTO habit_state_changes (habit_id, change_type, old_value, new_value, reason, changed_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (habit_id, change_type, old_value, new_value, reason, _now_iso()),
        )

    @classmethod
    def _load_agent_registry(cls, project_root: Path) -> dict[str, dict[str, str]]:
        agents_path = project_root / "agents.json"
        try:
            raw = json.loads(agents_path.read_text(encoding="utf-8"))
        except Exception:
            raw = {"agents": []}

        registry: dict[str, dict[str, str]] = {}
        for entry in raw.get("agents", []):
            agent_id = str(entry.get("name") or entry.get("id") or "").strip()
            if not agent_id:
                continue
            extra = entry.get("extra") or {}
            workspace_dir = str(entry.get("workspace_dir") or "").strip()
            if not workspace_dir:
                workspace_dir = f"workspaces/{agent_id}"
            registry[agent_id] = {
                "agent_id": agent_id,
                "agent_class": str(entry.get("agent_class") or extra.get("agent_class") or "general").strip().lower(),
                "workspace_dir": str((project_root / workspace_dir).resolve()),
                "is_active": "1" if bool(entry.get("is_active", True)) else "0",
                "backend": str(entry.get("active_backend") or entry.get("backend") or "unknown").strip().lower() or "unknown",
                "model": str(entry.get("model") or entry.get("default_model") or "unknown").strip() or "unknown",
            }
        return registry

    @classmethod
    def _get_workspace_dir_for_agent(cls, project_root: Path, agent_id: str) -> Path:
        registry = cls._load_agent_registry(project_root)
        entry = registry.get(agent_id)
        if entry:
            return Path(entry["workspace_dir"])
        return project_root / "workspaces" / agent_id

    @classmethod
    def _get_agent_class_for_agent(cls, project_root: Path, agent_id: str) -> str:
        registry = cls._load_agent_registry(project_root)
        entry = registry.get(agent_id)
        if entry:
            return entry["agent_class"]
        return "general"

    def retrieve(self, prompt: str, source: str = "", summary: str = "") -> list[RetrievedHabit]:
        task_type = infer_task_type(prompt, source, summary)
        corpus = _normalize_text(" ".join(part for part in (prompt, source, summary) if part))
        if not corpus:
            return []

        with self._connect() as conn, self._connect_eval() as eval_conn:
            rows = conn.execute(
                """
                SELECT habit_id, habit_type, title, instruction, task_type, trigger_json, confidence
                FROM habits
                WHERE agent_id = ?
                  AND enabled = 1
                  AND status IN ('active', 'candidate')
                  AND (agent_class = ? OR agent_class = 'general')
                ORDER BY confidence DESC, updated_at DESC
                """,
                (self.agent_id, self.agent_class),
            ).fetchall()
            shared_rows = eval_conn.execute(
                """
                SELECT
                    id AS habit_id,
                    habit_type,
                    title,
                    instruction,
                    task_type,
                    trigger_json,
                    confidence,
                    scope
                FROM shared_patterns
                WHERE instance = ?
                  AND status = ?
                  AND target_agent_class IN (?, 'general')
                ORDER BY confidence DESC, updated_at DESC
                """,
                (self.instance, self.SHARED_PATTERN_STATUS_ACTIVE, self.agent_class),
            ).fetchall()

        scored: list[RetrievedHabit] = []
        for row in rows:
            trigger = _from_json(row["trigger_json"], {})
            score = self._score_habit(
                corpus=corpus,
                row_task_type=row["task_type"],
                current_task_type=task_type,
                trigger=trigger,
            )
            if score <= 0:
                continue
            scored.append(
                RetrievedHabit(
                    habit_id=row["habit_id"],
                    habit_type=row["habit_type"],
                    title=row["title"],
                    instruction=row["instruction"],
                    task_type=row["task_type"],
                    score=score + float(row["confidence"] or 0.0),
                    origin="local",
                    scope="agent_local",
                )
            )

        for row in shared_rows:
            trigger = _from_json(row["trigger_json"], {})
            score = self._score_habit(
                corpus=corpus,
                row_task_type=row["task_type"],
                current_task_type=task_type,
                trigger=trigger,
            )
            if score <= 0:
                continue
            scored.append(
                RetrievedHabit(
                    habit_id=row["habit_id"],
                    habit_type=row["habit_type"],
                    title=row["title"],
                    instruction=row["instruction"],
                    task_type=row["task_type"],
                    score=score + float(row["confidence"] or 0.0),
                    origin="shared",
                    scope=str(row["scope"] or "shared_pattern"),
                )
            )

        scored.sort(key=lambda item: item.score, reverse=True)
        return self._apply_hard_limits(scored)

    def _score_habit(
        self,
        *,
        corpus: str,
        row_task_type: str | None,
        current_task_type: str | None,
        trigger: dict[str, Any],
    ) -> float:
        score = 0.0
        if row_task_type and current_task_type and row_task_type == current_task_type:
            score += 6.0
        elif row_task_type and current_task_type and row_task_type != current_task_type:
            return -1.0

        for field_name, weight in (("keywords", 3.0), ("synonyms", 2.0), ("patterns", 1.5)):
            items = trigger.get(field_name) or []
            for raw in items:
                token = _normalize_text(str(raw))
                if token and token in corpus:
                    score += weight
        return score

    def _apply_hard_limits(self, habits: list[RetrievedHabit]) -> list[RetrievedHabit]:
        chosen: list[RetrievedHabit] = []
        do_count = 0
        avoid_count = 0
        for habit in habits:
            kind = (habit.habit_type or "").strip().lower()
            if kind == "avoid":
                if avoid_count >= self.MAX_AVOID:
                    continue
                avoid_count += 1
            else:
                if do_count >= self.MAX_DO:
                    continue
                do_count += 1
            chosen.append(habit)
            if len(chosen) >= self.HARD_RETRIEVAL_LIMIT:
                break
        return chosen

    def render_prompt_section(self, habits: list[RetrievedHabit]) -> tuple[str, str] | None:
        if not habits:
            return None
        lines = []
        for habit in habits:
            prefix = "DO" if (habit.habit_type or "").lower() != "avoid" else "AVOID"
            title = f" [{habit.title}]" if habit.title else ""
            lines.append(f"- {prefix}{title}: {habit.instruction.strip()}")
        return ("ACTIVE HABITS", "\n".join(lines))

    def serialize_habits(self, habits: list[RetrievedHabit]) -> list[dict[str, Any]]:
        payloads: list[dict[str, Any]] = []
        if not habits:
            return payloads
        with self._connect() as conn:
            for habit in habits:
                row = conn.execute(
                    """
                    SELECT habit_id, habit_type, title, instruction, task_type, trigger_json
                    FROM habits
                    WHERE habit_id = ?
                    """,
                    (habit.habit_id,),
                ).fetchone()
                if not row:
                    continue
                payloads.append(
                    {
                        "habit_id": row["habit_id"],
                        "habit_type": row["habit_type"],
                        "title": row["title"],
                        "instruction": row["instruction"],
                        "task_type": row["task_type"],
                        "trigger": _from_json(row["trigger_json"], {}),
                        "origin": "local",
                        "scope": "agent_local",
                    }
                )
        with self._connect_eval() as eval_conn:
            for habit in habits:
                if habit.origin != "shared":
                    continue
                row = eval_conn.execute(
                    """
                    SELECT id, habit_type, title, instruction, task_type, trigger_json, scope
                    FROM shared_patterns
                    WHERE id = ?
                    """,
                    (habit.habit_id,),
                ).fetchone()
                if not row:
                    continue
                payloads.append(
                    {
                        "habit_id": row["id"],
                        "habit_type": row["habit_type"],
                        "title": row["title"],
                        "instruction": row["instruction"],
                        "task_type": row["task_type"],
                        "trigger": _from_json(row["trigger_json"], {}),
                        "origin": "shared",
                        "scope": row["scope"] or "shared_pattern",
                    }
                )
        return payloads

    def mark_triggered(self, habits: list[RetrievedHabit]):
        if not habits:
            return
        now = _now_iso()
        local_habits = [habit for habit in habits if habit.origin != "shared"]
        shared_habits = [habit for habit in habits if habit.origin == "shared"]
        if local_habits:
            with self._connect() as conn:
                for habit in local_habits:
                    conn.execute(
                        """
                        UPDATE habits
                        SET times_triggered = times_triggered + 1,
                            last_triggered_at = ?,
                            updated_at = ?
                        WHERE habit_id = ?
                        """,
                        (now, now, habit.habit_id),
                    )
        if shared_habits:
            with self._connect_eval() as eval_conn:
                for habit in shared_habits:
                    eval_conn.execute(
                        """
                        UPDATE shared_patterns
                        SET times_triggered = times_triggered + 1,
                            last_triggered_at = ?,
                            updated_at = ?
                        WHERE id = ?
                        """,
                        (now, now, habit.habit_id),
                    )

    def record_execution_outcome(
        self,
        *,
        request_id: str | None,
        prompt: str,
        source: str,
        summary: str,
        active_habits: list[dict[str, Any]] | None,
        response_text: str | None = None,
        error_text: str | None = None,
        success: bool,
    ):
        task_type = infer_task_type(prompt, source, summary)
        context_summary = self._build_context_summary(
            prompt=prompt,
            summary=summary,
            response_text=response_text,
            error_text=error_text,
            success=success,
        )
        event_ts = _now_event_iso()
        generated_candidates = self._maybe_generate_candidate_habits(
            task_type=task_type,
            prompt=prompt,
            source=source,
            summary=summary,
            response_text=response_text,
            error_text=error_text,
            success=success,
            active_habits=active_habits or [],
        )

        with self._connect() as conn, self._connect_eval() as eval_conn:
            for payload in active_habits or []:
                habit_id = str(payload.get("habit_id") or "").strip()
                if not habit_id:
                    continue
                origin = str(payload.get("origin") or "local").strip().lower()
                explicit_use = self._response_signals_use(
                    response_text=response_text,
                    payload=payload,
                    task_type=task_type or payload.get("task_type"),
                )
                helpful = 1 if (success and explicit_use) else None
                harmful = 1 if not success else 0
                ignored = 1 if (success and not explicit_use) else 0
                eval_conn.execute(
                    """
                    INSERT INTO habit_events (
                        version, instance, agent_id, habit_id, request_id, task_type,
                        triggered, applied, helpful, harmful, ignored,
                        context_summary, feedback_text, feedback_ts, ts, ts_source
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        1,
                        self.instance,
                        self.agent_id,
                        habit_id,
                        request_id,
                        task_type or payload.get("task_type"),
                        1,
                        1,
                        helpful,
                        harmful,
                        ignored,
                        context_summary,
                        None,
                        None,
                        event_ts,
                        "native",
                    ),
                )
                if origin == "shared" or self._is_shared_pattern_id(habit_id):
                    eval_conn.execute(
                        """
                        UPDATE shared_patterns
                        SET times_applied = times_applied + 1,
                            times_helpful = times_helpful + ?,
                            times_harmful = times_harmful + ?,
                            last_helpful_at = CASE WHEN ? = 1 THEN ? ELSE last_helpful_at END,
                            updated_at = ?
                        WHERE id = ?
                        """,
                        (
                            1 if helpful == 1 else 0,
                            1 if harmful == 1 else 0,
                            1 if helpful == 1 else 0,
                            event_ts,
                            event_ts,
                            habit_id,
                        ),
                    )
                else:
                    conn.execute(
                        """
                        UPDATE habits
                        SET times_applied = times_applied + 1,
                            times_helpful = times_helpful + ?,
                            times_harmful = times_harmful + ?,
                            last_helpful_at = CASE WHEN ? = 1 THEN ? ELSE last_helpful_at END,
                            updated_at = ?
                        WHERE habit_id = ?
                        """,
                        (
                            1 if helpful == 1 else 0,
                            1 if harmful == 1 else 0,
                            1 if helpful == 1 else 0,
                            event_ts,
                            event_ts,
                            habit_id,
                        ),
                    )
                    self._refresh_habit_status(conn, eval_conn, habit_id)

            for payload in generated_candidates:
                habit_id = str(payload.get("habit_id") or "").strip()
                if not habit_id:
                    continue
                eval_conn.execute(
                    """
                    INSERT INTO habit_events (
                        version, instance, agent_id, habit_id, request_id, task_type,
                        triggered, applied, helpful, harmful, ignored,
                        context_summary, feedback_text, feedback_ts, ts, ts_source
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        1,
                        self.instance,
                        self.agent_id,
                        habit_id,
                        request_id,
                        task_type or payload.get("task_type"),
                        0,
                        0,
                        1 if success else 0,
                        0 if success else 1,
                        0,
                        f"{context_summary} | candidate_generated=1",
                        None,
                        None,
                        event_ts,
                        "native",
                    ),
                )
                self._refresh_habit_status(conn, eval_conn, habit_id)

    def apply_user_feedback(
        self,
        *,
        request_id: str | None,
        feedback_text: str,
        responded_at: str | None = None,
        max_age_seconds: int = 900,
    ) -> HabitFeedbackResult | None:
        if not request_id:
            return None
        sentiment = self._classify_feedback(feedback_text)
        if sentiment == "neutral":
            return None

        if responded_at:
            try:
                age_s = (datetime.now().astimezone() - datetime.fromisoformat(responded_at)).total_seconds()
            except Exception:
                age_s = 0
            if age_s > max_age_seconds:
                return None

        feedback_ts = _now_event_iso()
        updated_habits: list[str] = []
        updated_events = 0
        with self._connect() as conn, self._connect_eval() as eval_conn:
            rows = eval_conn.execute(
                """
                SELECT id, habit_id, helpful, harmful, ignored, context_summary
                FROM habit_events
                WHERE instance = ? AND agent_id = ? AND request_id = ?
                ORDER BY ts DESC
                """,
                (self.instance, self.agent_id, request_id),
            ).fetchall()
            if not rows:
                return None

            for row in rows:
                new_helpful = 1 if sentiment == "positive" else 0
                new_harmful = 1 if sentiment == "negative" else 0
                new_ignored = 0
                old_helpful = 1 if row["helpful"] == 1 else 0
                old_harmful = 1 if row["harmful"] == 1 else 0
                old_ignored = 1 if row["ignored"] == 1 else 0
                delta_helpful = new_helpful - old_helpful
                delta_harmful = new_harmful - old_harmful

                context_summary = str(row["context_summary"] or "").strip()
                feedback_note = f"feedback={_normalize_text(feedback_text)[:220]}"
                if feedback_note not in context_summary:
                    context_summary = f"{context_summary} | {feedback_note}".strip(" |")

                eval_conn.execute(
                    """
                    UPDATE habit_events
                    SET helpful = ?, harmful = ?, ignored = ?,
                        feedback_text = ?, feedback_ts = ?, context_summary = ?
                    WHERE id = ?
                    """,
                    (
                        new_helpful,
                        new_harmful,
                        new_ignored,
                        feedback_text.strip()[:500],
                        feedback_ts,
                        context_summary,
                        row["id"],
                    ),
                )
                habit_id = str(row["habit_id"])
                if self._is_shared_pattern_id(habit_id):
                    eval_conn.execute(
                        """
                        UPDATE shared_patterns
                        SET times_helpful = MAX(0, times_helpful + ?),
                            times_harmful = MAX(0, times_harmful + ?),
                            last_helpful_at = CASE WHEN ? = 1 THEN ? ELSE last_helpful_at END,
                            updated_at = ?
                        WHERE id = ?
                        """,
                        (
                            delta_helpful,
                            delta_harmful,
                            new_helpful,
                            feedback_ts,
                            feedback_ts,
                            habit_id,
                        ),
                    )
                else:
                    conn.execute(
                        """
                        UPDATE habits
                        SET times_helpful = MAX(0, times_helpful + ?),
                            times_harmful = MAX(0, times_harmful + ?),
                            last_helpful_at = CASE WHEN ? = 1 THEN ? ELSE last_helpful_at END,
                            updated_at = ?
                        WHERE habit_id = ?
                        """,
                        (
                            delta_helpful,
                            delta_harmful,
                            new_helpful,
                            feedback_ts,
                            feedback_ts,
                            habit_id,
                        ),
                    )
                    self._refresh_habit_status(conn, eval_conn, habit_id)
                updated_events += 1
                updated_habits.append(habit_id)

        if updated_events <= 0:
            return None
        return HabitFeedbackResult(
            request_id=request_id,
            sentiment=sentiment,
            updated_events=updated_events,
            updated_habits=updated_habits,
        )

    def nightly_review(self, *, lookback_days: int = 7) -> HabitNightlyReview:
        changed_habits: list[str] = []
        promoted: list[str] = []
        paused: list[str] = []
        disabled: list[str] = []
        decayed: list[str] = []
        recommendations: list[str] = []
        reviewed_habits = 0

        with self._connect() as conn, self._connect_eval() as eval_conn:
            rows = conn.execute(
                """
                SELECT
                    habit_id, status, habit_type, title, instruction, task_type,
                    confidence, times_triggered, times_helpful, times_harmful,
                    created_at, updated_at, last_triggered_at, last_helpful_at
                FROM habits
                WHERE agent_id = ?
                ORDER BY
                    CASE status
                        WHEN 'active' THEN 0
                        WHEN 'candidate' THEN 1
                        WHEN 'paused' THEN 2
                        WHEN 'disabled' THEN 3
                        ELSE 4
                    END,
                    updated_at DESC
                """,
                (self.agent_id,),
            ).fetchall()
            review_cutoff = datetime.now().astimezone() - timedelta(days=max(1, lookback_days))
            for row in rows:
                reviewed_habits += 1
                outcome = self._review_single_habit(
                    conn=conn,
                    eval_conn=eval_conn,
                    row=row,
                    review_cutoff=review_cutoff,
                )
                if not outcome:
                    continue
                habit_id = outcome["habit_id"]
                if outcome["changed"]:
                    changed_habits.append(habit_id)
                if outcome["promoted"]:
                    promoted.append(habit_id)
                if outcome["paused"]:
                    paused.append(habit_id)
                if outcome["disabled"]:
                    disabled.append(habit_id)
                if outcome["decayed"]:
                    decayed.append(habit_id)
                recommendation = outcome["recommendation"]
                if recommendation:
                    recommendations.append(recommendation)

            totals = conn.execute(
                """
                SELECT
                    COUNT(*) AS total_habits,
                    COALESCE(SUM(CASE WHEN status = 'active' THEN 1 ELSE 0 END), 0) AS active_habits,
                    COALESCE(SUM(CASE WHEN status = 'candidate' THEN 1 ELSE 0 END), 0) AS candidate_habits,
                    COALESCE(SUM(CASE WHEN status = 'paused' THEN 1 ELSE 0 END), 0) AS paused_habits,
                    COALESCE(SUM(CASE WHEN status = 'disabled' THEN 1 ELSE 0 END), 0) AS disabled_habits
                FROM habits
                WHERE agent_id = ?
                """,
                (self.agent_id,),
            ).fetchone()

        summary_parts = [
            f"reviewed={reviewed_habits}",
            f"changed={len(changed_habits)}",
            f"active={int(totals['active_habits'] or 0)}",
            f"candidate={int(totals['candidate_habits'] or 0)}",
        ]
        if promoted:
            summary_parts.append(f"promoted={len(promoted)}")
        if paused:
            summary_parts.append(f"paused={len(paused)}")
        if disabled:
            summary_parts.append(f"disabled={len(disabled)}")
        if decayed:
            summary_parts.append(f"decayed={len(decayed)}")

        return HabitNightlyReview(
            agent_id=self.agent_id,
            lookback_days=lookback_days,
            total_habits=int(totals["total_habits"] or 0),
            active_habits=int(totals["active_habits"] or 0),
            candidate_habits=int(totals["candidate_habits"] or 0),
            paused_habits=int(totals["paused_habits"] or 0),
            disabled_habits=int(totals["disabled_habits"] or 0),
            reviewed_habits=reviewed_habits,
            changed_habits=changed_habits,
            promoted=promoted,
            paused=paused,
            disabled=disabled,
            decayed=decayed,
            recommendations=recommendations[:5],
            summary=", ".join(summary_parts),
        )

    def format_nightly_review(self, review: HabitNightlyReview) -> str:
        lines = [
            "🪄 Habit review:",
            (
                f"reviewed {review.reviewed_habits} habits in the last {review.lookback_days}d; "
                f"active={review.active_habits}, candidate={review.candidate_habits}, "
                f"paused={review.paused_habits}, disabled={review.disabled_habits}"
            ),
        ]
        if review.promoted:
            lines.append(f"  • promoted {len(review.promoted)}")
        if review.paused:
            lines.append(f"  • paused {len(review.paused)}")
        if review.disabled:
            lines.append(f"  • disabled {len(review.disabled)}")
        if review.decayed:
            lines.append(f"  • decayed confidence on {len(review.decayed)}")
        for item in review.recommendations[:3]:
            lines.append(f"  • {item}")
        return "\n".join(lines)

    # ── User signal recording ─────────────────────────────────────────────────

    def record_user_signal(
        self,
        *,
        signal: str,
        comment: str | None,
        context: str,
    ) -> int:
        """Store a /good or /bad signal with full transcript context."""
        ts = _now_event_iso()
        with self._connect_eval() as conn:
            cursor = conn.execute(
                """
                INSERT INTO user_signals (instance, agent_id, signal, comment, context, ts)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (self.instance, self.agent_id, signal, comment, context, ts),
            )
            return cursor.lastrowid  # type: ignore[return-value]

    def process_user_signals(
        self,
        *,
        api_key: str,
        call_llm_fn: Any,
        max_signals: int = 3,
        max_habits_per_signal: int = 2,
        max_context_words: int = 6000,
    ) -> list[str]:
        """Process pending user_signals during dream, generate habit candidates.

        Returns a list of summary lines for the dream log.
        """
        with self._connect_eval() as eval_conn:
            rows = eval_conn.execute(
                """
                SELECT id, signal, comment, context
                FROM user_signals
                WHERE instance = ? AND agent_id = ? AND processed = 0
                ORDER BY CASE signal WHEN 'bad' THEN 0 ELSE 1 END, ts ASC
                LIMIT ?
                """,
                (self.instance, self.agent_id, max_signals),
            ).fetchall()

        if not rows:
            return []

        log_lines: list[str] = []

        for row in rows:
            signal_id = row["id"]
            signal = row["signal"]
            comment = row["comment"] or ""
            context_raw = row["context"]

            # Truncate context to max_context_words
            words = context_raw.split()
            if len(words) > max_context_words:
                context_trimmed = " ".join(words[-max_context_words:])
                context_text = f"[...context truncated to last {max_context_words} words...]\n{context_trimmed}"
            else:
                context_text = context_raw

            prompt = (
                f"You are reviewing a conversation between an AI agent ('{self.agent_id}') and their user.\n"
                f"The user has given a /{signal} signal{'with comment: ' + repr(comment) if comment else ''}.\n\n"
                f"Here is the full conversation context (including thinking tokens marked with [THINKING]):\n\n"
                f"{context_text}\n\n"
                f"Based on this, identify {'what the agent did well that should become a habit' if signal == 'good' else 'what the agent did wrong that should be avoided as a habit'}.\n"
                f"Extract {max_habits_per_signal} specific, actionable habit instructions (or fewer if not enough distinct lessons).\n\n"
                f"Respond ONLY with a JSON array of objects. Each object must have:\n"
                f'  "title": short habit title (max 60 chars)\n'
                f'  "instruction": concrete instruction for the agent (1-2 sentences)\n'
                f'  "rationale": why this matters based on the conversation\n'
                f'  "habit_type": "do" if signal is good, "avoid" if signal is bad\n\n'
                f"Example: [{{"
                f'"title": "Verify before asserting", '
                f'"instruction": "Always check the actual file or data before stating a fact as certain.", '
                f'"rationale": "Agent claimed a file existed without checking, wasting user time.", '
                f'"habit_type": "avoid"'
                f"}}]\n\n"
                f"If there is nothing meaningful to extract, return an empty array []."
            )

            try:
                response = call_llm_fn(api_key, [{"role": "user", "content": prompt}])
                # Extract JSON from response
                match = re.search(r"\[.*\]", response, re.DOTALL)
                if not match:
                    raise ValueError("No JSON array found in LLM response")
                candidates = json.loads(match.group(0))
            except Exception as exc:
                log_lines.append(f"[user-signal #{signal_id}] parse error: {exc}")
                self._mark_signal_processed(signal_id, [])
                continue

            generated_ids: list[str] = []
            with self._connect() as conn:
                existing_habits = conn.execute(
                    "SELECT habit_id, instruction FROM habits WHERE agent_id = ?",
                    (self.agent_id,),
                ).fetchall()
                existing_instructions = [
                    (_normalize_text(r["instruction"]), r["habit_id"]) for r in existing_habits
                ]

            for item in candidates[:max_habits_per_signal]:
                title = str(item.get("title", "")).strip()[:60]
                instruction = str(item.get("instruction", "")).strip()
                rationale = str(item.get("rationale", "")).strip()
                habit_type = str(item.get("habit_type", "avoid" if signal == "bad" else "do")).strip()

                if not instruction:
                    continue

                # Check similarity to avoid duplicate habits (simple word overlap)
                norm_new = _normalize_text(instruction)
                new_words = set(norm_new.split())
                duplicate_id: str | None = None
                for norm_existing, existing_id in existing_instructions:
                    existing_words = set(norm_existing.split())
                    if not existing_words:
                        continue
                    overlap = len(new_words & existing_words) / max(len(new_words), len(existing_words))
                    if overlap > 0.6:
                        duplicate_id = existing_id
                        break

                if duplicate_id:
                    # Reinforce existing habit by bumping confidence slightly
                    with self._connect() as conn:
                        conn.execute(
                            """
                            UPDATE habits SET
                                confidence = MIN(1.0, confidence + 0.05),
                                updated_at = ?
                            WHERE habit_id = ?
                            """,
                            (_now_iso(), duplicate_id),
                        )
                    generated_ids.append(duplicate_id)
                    log_lines.append(
                        f"[user-signal #{signal_id}/{signal}] reinforced existing habit {duplicate_id}: {title}"
                    )
                else:
                    habit_id = generate_habit_id(self.instance, self.agent_id)
                    with self._connect() as conn:
                        conn.execute(
                            """
                            INSERT OR IGNORE INTO habits (
                                habit_id, agent_id, agent_class, status, habit_type,
                                title, instruction, rationale, source, confidence,
                                created_at, updated_at
                            ) VALUES (?, ?, ?, 'active', ?, ?, ?, ?, 'user_signal', 0.8, ?, ?)
                            """,
                            (
                                habit_id,
                                self.agent_id,
                                self.agent_class,
                                habit_type,
                                title,
                                instruction,
                                rationale,
                                _now_iso(),
                                _now_iso(),
                            ),
                        )
                    generated_ids.append(habit_id)
                    log_lines.append(
                        f"[user-signal #{signal_id}/{signal}] new candidate habit: {title}"
                    )

            self._mark_signal_processed(signal_id, generated_ids)

        return log_lines

    def _mark_signal_processed(self, signal_id: int, habit_ids: list[str]) -> None:
        with self._connect_eval() as conn:
            conn.execute(
                """
                UPDATE user_signals SET processed = 1, processed_at = ?, habit_ids_generated = ?
                WHERE id = ?
                """,
                (_now_event_iso(), json.dumps(habit_ids), signal_id),
            )

    @classmethod
    def generate_recommendation_report(
        cls,
        *,
        project_root: Path,
        generated_by: str,
        lookback_days: int = 7,
        max_recommendations: int = 12,
    ) -> HabitRecommendationReport:
        lily_workspace = project_root / "workspaces" / "lily"
        report_dir = lily_workspace / cls.REPORT_DIRNAME
        report_dir.mkdir(parents=True, exist_ok=True)
        generated_at = _now_event_iso()
        stamped_name = re.sub(r"[^0-9A-Za-z]+", "-", generated_at).strip("-")
        instance = detect_instance(project_root)
        review_cutoff = datetime.now().astimezone() - timedelta(days=max(1, lookback_days))

        eval_db_path = lily_workspace / "habit_evaluation.sqlite"
        if not eval_db_path.exists():
            markdown = (
                "# Habit Recommendation Report\n\n"
                f"Generated at: {generated_at}\n\n"
                "No habit evaluation database found yet.\n"
            )
            json_path = report_dir / "latest.json"
            markdown_path = report_dir / "latest.md"
            dashboard_json_path = report_dir / "dashboard.json"
            dashboard_markdown_path = report_dir / "dashboard.md"
            json_path.write_text(_to_json({
                "generated_at": generated_at,
                "generated_by": generated_by,
                "lookback_days": lookback_days,
                "agents_considered": 0,
                "habits_considered": 0,
                "pending_copy_approvals": 0,
                "recommendations": [],
                "copy_recommendations": [],
                "agent_summaries": [],
                "task_family_summaries": [],
                "class_summaries": [],
                "backend_summaries": [],
                "timestamp_source_summaries": [],
            }), encoding="utf-8")
            markdown_path.write_text(markdown, encoding="utf-8")
            dashboard_payload = {
                "generated_at": generated_at,
                "generated_by": generated_by,
                "lookback_days": lookback_days,
                "overview": {
                    "agents_considered": 0,
                    "habits_considered": 0,
                    "actionable_recommendations": 0,
                    "pending_copy_approvals": 0,
                    "active_shared_patterns": 0,
                },
                "agent_summaries": [],
                "task_family_summaries": [],
                "class_summaries": [],
                "backend_summaries": [],
                "timestamp_source_summaries": [],
            }
            dashboard_json_path.write_text(json.dumps(dashboard_payload, indent=2, ensure_ascii=False), encoding="utf-8")
            dashboard_markdown_path.write_text(
                cls._format_dashboard_markdown(
                    generated_at=generated_at,
                    generated_by=generated_by,
                    lookback_days=lookback_days,
                    overview=dashboard_payload["overview"],
                    agent_summaries=[],
                    task_family_summaries=[],
                    class_summaries=[],
                    backend_summaries=[],
                    timestamp_source_summaries=[],
                ),
                encoding="utf-8",
            )
            return HabitRecommendationReport(
                generated_at=generated_at,
                generated_by=generated_by,
                lookback_days=lookback_days,
                agents_considered=0,
                habits_considered=0,
                recommendations=[],
                copy_recommendations=[],
                shared_patterns=[],
                agent_summaries=[],
                task_family_summaries=[],
                class_summaries=[],
                backend_summaries=[],
                timestamp_source_summaries=[],
                markdown=markdown,
                json_path=json_path,
                markdown_path=markdown_path,
                dashboard_json_path=dashboard_json_path,
                dashboard_markdown_path=dashboard_markdown_path,
            )

        workspaces_dir = project_root / "workspaces"
        recommendations: list[HabitRecommendation] = []
        copy_recommendations: list[HabitCopyRecommendation] = []
        agent_summaries: list[dict[str, Any]] = []
        task_family_totals: dict[str, dict[str, Any]] = {}
        class_totals: dict[str, dict[str, Any]] = {}
        backend_totals: dict[str, dict[str, Any]] = {}
        habits_considered = 0
        agent_registry = cls._load_agent_registry(project_root)

        with cls._connect_eval_db(eval_db_path) as eval_conn:
            ts_source_rows = eval_conn.execute(
                """
                SELECT ts_source, COUNT(*) AS event_count
                FROM habit_events
                WHERE instance = ? AND ts >= ?
                GROUP BY ts_source
                ORDER BY event_count DESC, ts_source ASC
                """,
                (instance, review_cutoff.isoformat(timespec="seconds")),
            ).fetchall()
            timestamp_source_summaries = [
                {
                    "ts_source": str(row["ts_source"] or "unknown"),
                    "event_count": int(row["event_count"] or 0),
                }
                for row in ts_source_rows
            ]
            for habit_db_path in sorted(workspaces_dir.glob("*/habits.sqlite")):
                agent_id = habit_db_path.parent.name
                agent_meta = agent_registry.get(agent_id) or {}
                agent_class = str(agent_meta.get("agent_class") or "general")
                backend = str(agent_meta.get("backend") or "unknown")
                model = str(agent_meta.get("model") or "unknown")
                conn = sqlite3.connect(habit_db_path)
                conn.row_factory = sqlite3.Row
                try:
                    rows = conn.execute(
                        """
                        SELECT
                            habit_id, status, habit_type, title, instruction, task_type,
                            confidence, last_triggered_at, last_helpful_at, updated_at, created_at
                        FROM habits
                        WHERE enabled = 1
                        ORDER BY updated_at DESC
                        """
                    ).fetchall()
                    if not rows:
                        continue

                    agent_counts = {"active": 0, "candidate": 0, "paused": 0, "disabled": 0}
                    agent_recommendations: list[HabitRecommendation] = []
                    agent_triggered_total = 0
                    agent_helpful_total = 0
                    agent_harmful_total = 0
                    agent_ignored_total = 0
                    for row in rows:
                        habits_considered += 1
                        status = str(row["status"] or "candidate")
                        agent_counts[status] = agent_counts.get(status, 0) + 1
                        task_type = str(row["task_type"] or "").strip() or "untyped"
                        task_bucket = task_family_totals.setdefault(
                            task_type,
                            {
                                **cls._make_dashboard_bucket(task_type, bucket_type="task_family"),
                                "task_type": task_type,
                            },
                        )
                        task_bucket["habits"] += 1

                        stats = eval_conn.execute(
                            """
                            SELECT
                                COALESCE(SUM(CASE WHEN triggered = 1 THEN 1 ELSE 0 END), 0) AS triggered_count,
                                COALESCE(SUM(CASE WHEN helpful = 1 THEN 1 ELSE 0 END), 0) AS helpful_count,
                                COALESCE(SUM(CASE WHEN harmful = 1 THEN 1 ELSE 0 END), 0) AS harmful_count,
                                COALESCE(SUM(CASE WHEN ignored = 1 THEN 1 ELSE 0 END), 0) AS ignored_count
                            FROM habit_events
                            WHERE instance = ? AND agent_id = ? AND habit_id = ? AND ts >= ?
                            """,
                            (instance, agent_id, str(row["habit_id"]), review_cutoff.isoformat(timespec="seconds")),
                        ).fetchone()
                        helpful_recent = int(stats["helpful_count"] or 0)
                        harmful_recent = int(stats["harmful_count"] or 0)
                        ignored_recent = int(stats["ignored_count"] or 0)
                        triggered_recent = int(stats["triggered_count"] or 0)
                        agent_triggered_total += triggered_recent
                        agent_helpful_total += helpful_recent
                        agent_harmful_total += harmful_recent
                        agent_ignored_total += ignored_recent
                        task_bucket["helpful"] += helpful_recent
                        task_bucket["harmful"] += harmful_recent
                        task_bucket["ignored"] += ignored_recent
                        task_bucket["triggered"] = int(task_bucket.get("triggered", 0)) + triggered_recent
                        copy_recommendations.extend(
                            cls._build_copy_recommendations_for_row(
                                project_root=project_root,
                                agent_registry=agent_registry,
                                source_agent_id=agent_id,
                                row=row,
                                helpful_recent=helpful_recent,
                                harmful_recent=harmful_recent,
                                triggered_recent=triggered_recent,
                            )
                        )

                        recommendation = cls._recommendation_for_row(
                            agent_id=agent_id,
                            row=row,
                            lookback_days=lookback_days,
                            helpful_recent=helpful_recent,
                            harmful_recent=harmful_recent,
                            ignored_recent=ignored_recent,
                            triggered_recent=triggered_recent,
                        )
                        if recommendation is None:
                            continue
                        agent_recommendations.append(recommendation)
                        recommendations.append(recommendation)
                        task_bucket["recommendations"] += 1

                    event_total = agent_helpful_total + agent_harmful_total
                    helpful_rate = _safe_ratio(agent_helpful_total, event_total)
                    harmful_rate = _safe_ratio(agent_harmful_total, event_total)
                    evidence_quality = _wilson_lower_bound(agent_helpful_total, event_total)
                    observe_more = max(0, agent_triggered_total - event_total)

                    agent_summaries.append(
                        {
                            "agent_id": agent_id,
                            "agent_class": agent_class,
                            "backend": backend,
                            "model": model,
                            "total_habits": len(rows),
                            "active_habits": agent_counts.get("active", 0),
                            "candidate_habits": agent_counts.get("candidate", 0),
                            "paused_habits": agent_counts.get("paused", 0),
                            "disabled_habits": agent_counts.get("disabled", 0),
                            "recommendation_count": len(agent_recommendations),
                            "triggered_recent": agent_triggered_total,
                            "helpful_recent": agent_helpful_total,
                            "harmful_recent": agent_harmful_total,
                            "ignored_recent": agent_ignored_total,
                            "observe_more_count": observe_more,
                            "helpful_rate": helpful_rate,
                            "harmful_rate": harmful_rate,
                            "evidence_quality": evidence_quality,
                            "top_recommendations": [item.summary for item in agent_recommendations[:3]],
                        }
                    )
                    class_bucket = class_totals.setdefault(
                        agent_class,
                        cls._make_dashboard_bucket(agent_class, bucket_type="agent_class"),
                    )
                    backend_bucket = backend_totals.setdefault(
                        backend,
                        cls._make_dashboard_bucket(backend, bucket_type="backend"),
                    )
                    cls._update_dashboard_bucket(
                        class_bucket,
                        habit_count=len(rows),
                        recommendation_count=len(agent_recommendations),
                        triggered=agent_triggered_total,
                        helpful=agent_helpful_total,
                        harmful=agent_harmful_total,
                        ignored=agent_ignored_total,
                    )
                    cls._update_dashboard_bucket(
                        backend_bucket,
                        habit_count=len(rows),
                        recommendation_count=len(agent_recommendations),
                        triggered=agent_triggered_total,
                        helpful=agent_helpful_total,
                        harmful=agent_harmful_total,
                        ignored=agent_ignored_total,
                    )
                    backend_bucket["models"][model] = int(backend_bucket["models"].get(model, 0)) + 1
                finally:
                    conn.close()

            priority = {
                "pause_active": 0,
                "disable_candidate": 1,
                "promote_candidate": 2,
                "reactivate_paused": 3,
                "patch_active": 4,
                "observe_more": 5,
            }
            recommendations.sort(
                key=lambda item: (
                    priority.get(item.recommendation_type, 99),
                    -item.harmful_recent,
                    -item.helpful_recent,
                    item.agent_id,
                    item.title.lower(),
                )
            )
            trimmed_recommendations = recommendations[:max(1, max_recommendations)]
            synced_copy_recommendations = cls._sync_copy_recommendations(
                eval_conn=eval_conn,
                instance=instance,
                generated_at=generated_at,
                copy_recommendations=copy_recommendations,
            )
            pending_copy_count = sum(1 for item in synced_copy_recommendations if item.status == cls.COPY_APPROVAL_STATUS_PENDING)
            shared_patterns = cls._load_shared_patterns(
                eval_conn,
                instance,
                status=cls.SHARED_PATTERN_STATUS_ACTIVE,
                limit=100,
            )
            agent_summaries.sort(key=lambda item: (-int(item["recommendation_count"]), item["agent_id"]))
            task_family_summaries = sorted(
                (cls._finalize_dashboard_bucket(item) for item in task_family_totals.values()),
                key=lambda item: (-int(item["recommendations"]), -int(item["harmful"]), item["task_type"]),
            )
            class_summaries = sorted(
                (cls._finalize_dashboard_bucket(item) for item in class_totals.values()),
                key=lambda item: (-int(item["recommendations"]), -float(item["evidence_quality"]), item["name"]),
            )
            backend_summaries = sorted(
                (cls._finalize_dashboard_bucket(item) for item in backend_totals.values()),
                key=lambda item: (-int(item["recommendations"]), -float(item["evidence_quality"]), item["name"]),
            )
            agent_summaries = [
                {
                    **item,
                    "helpful_rate": round(float(item["helpful_rate"]), 4),
                    "harmful_rate": round(float(item["harmful_rate"]), 4),
                    "evidence_quality": round(float(item["evidence_quality"]), 4),
                }
                for item in agent_summaries
            ]

            markdown = cls._format_recommendation_report_markdown(
                generated_at=generated_at,
                generated_by=generated_by,
                lookback_days=lookback_days,
                agent_summaries=agent_summaries,
                task_family_summaries=task_family_summaries,
                class_summaries=class_summaries,
                backend_summaries=backend_summaries,
                timestamp_source_summaries=timestamp_source_summaries,
                recommendations=trimmed_recommendations,
                copy_recommendations=synced_copy_recommendations,
                shared_patterns=shared_patterns,
            )
            dashboard_payload = {
                "generated_at": generated_at,
                "generated_by": generated_by,
                "lookback_days": lookback_days,
                "overview": {
                    "agents_considered": len(agent_summaries),
                    "habits_considered": habits_considered,
                    "actionable_recommendations": len(trimmed_recommendations),
                    "pending_copy_approvals": pending_copy_count,
                    "active_shared_patterns": len(shared_patterns),
                },
                "agent_summaries": agent_summaries,
                "task_family_summaries": task_family_summaries,
                "class_summaries": class_summaries,
                "backend_summaries": backend_summaries,
                "timestamp_source_summaries": timestamp_source_summaries,
            }
            payload = {
                "generated_at": generated_at,
                "generated_by": generated_by,
                "lookback_days": lookback_days,
                "agents_considered": len(agent_summaries),
                "habits_considered": habits_considered,
                "pending_copy_approvals": pending_copy_count,
                "active_shared_patterns": len(shared_patterns),
                "recommendations": [
                    {
                        "agent_id": item.agent_id,
                        "habit_id": item.habit_id,
                        "title": item.title,
                        "task_type": item.task_type,
                        "status": item.status,
                        "recommendation_type": item.recommendation_type,
                        "confidence": item.confidence,
                        "helpful_recent": item.helpful_recent,
                        "harmful_recent": item.harmful_recent,
                        "ignored_recent": item.ignored_recent,
                        "triggered_recent": item.triggered_recent,
                        "summary": item.summary,
                    }
                    for item in trimmed_recommendations
                ],
                "copy_recommendations": [
                    {
                        "recommendation_id": item.recommendation_id,
                        "source_agent_id": item.source_agent_id,
                        "source_habit_id": item.source_habit_id,
                        "source_title": item.source_title,
                        "source_agent_class": item.source_agent_class,
                        "target_agent_id": item.target_agent_id,
                        "target_agent_class": item.target_agent_class,
                        "task_type": item.task_type,
                        "status": item.status,
                        "confidence": item.confidence,
                        "helpful_recent": item.helpful_recent,
                        "harmful_recent": item.harmful_recent,
                        "triggered_recent": item.triggered_recent,
                        "summary": item.summary,
                        "generated_at": item.generated_at,
                        "reviewed_by": item.reviewed_by,
                        "reviewed_at": item.reviewed_at,
                        "review_note": item.review_note,
                        "copied_habit_id": item.copied_habit_id,
                    }
                    for item in synced_copy_recommendations
                ],
                "shared_patterns": [
                    {
                        "shared_pattern_id": item.shared_pattern_id,
                        "kind": item.kind,
                        "status": item.status,
                        "title": item.title,
                        "habit_type": item.habit_type,
                        "instruction": item.instruction,
                        "task_type": item.task_type,
                        "target_agent_class": item.target_agent_class,
                        "owner": item.owner,
                        "confidence": item.confidence,
                        "helpful_recent": item.helpful_recent,
                        "harmful_recent": item.harmful_recent,
                        "triggered_recent": item.triggered_recent,
                        "source_agent_id": item.source_agent_id,
                        "source_habit_id": item.source_habit_id,
                        "promoted_by": item.promoted_by,
                        "promoted_at": item.promoted_at,
                        "governance_note": item.governance_note,
                    }
                    for item in shared_patterns
                ],
                "agent_summaries": agent_summaries,
                "task_family_summaries": task_family_summaries,
                "class_summaries": class_summaries,
                "backend_summaries": backend_summaries,
                "timestamp_source_summaries": timestamp_source_summaries,
                "dashboard": dashboard_payload,
            }
        json_path = report_dir / "latest.json"
        markdown_path = report_dir / "latest.md"
        dashboard_json_path = report_dir / "dashboard.json"
        dashboard_markdown_path = report_dir / "dashboard.md"
        json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        markdown_path.write_text(markdown, encoding="utf-8")
        dashboard_json_path.write_text(json.dumps(dashboard_payload, indent=2, ensure_ascii=False), encoding="utf-8")
        dashboard_markdown_path.write_text(
            cls._format_dashboard_markdown(
                generated_at=generated_at,
                generated_by=generated_by,
                lookback_days=lookback_days,
                overview=dashboard_payload["overview"],
                agent_summaries=agent_summaries,
                task_family_summaries=task_family_summaries,
                class_summaries=class_summaries,
                backend_summaries=backend_summaries,
                timestamp_source_summaries=timestamp_source_summaries,
            ),
            encoding="utf-8",
        )
        stamped_json_path = report_dir / f"recommendations_{stamped_name}.json"
        stamped_md_path = report_dir / f"recommendations_{stamped_name}.md"
        stamped_dashboard_json_path = report_dir / f"dashboard_{stamped_name}.json"
        stamped_dashboard_md_path = report_dir / f"dashboard_{stamped_name}.md"
        stamped_json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        stamped_md_path.write_text(markdown, encoding="utf-8")
        stamped_dashboard_json_path.write_text(json.dumps(dashboard_payload, indent=2, ensure_ascii=False), encoding="utf-8")
        stamped_dashboard_md_path.write_text(dashboard_markdown_path.read_text(encoding="utf-8"), encoding="utf-8")
        cls.export_shared_pattern_registry(project_root=project_root)

        return HabitRecommendationReport(
            generated_at=generated_at,
            generated_by=generated_by,
            lookback_days=lookback_days,
            agents_considered=len(agent_summaries),
            habits_considered=habits_considered,
            recommendations=trimmed_recommendations,
            copy_recommendations=synced_copy_recommendations,
            shared_patterns=shared_patterns,
            agent_summaries=agent_summaries,
            task_family_summaries=task_family_summaries,
            class_summaries=class_summaries,
            backend_summaries=backend_summaries,
            timestamp_source_summaries=timestamp_source_summaries,
            markdown=markdown,
            json_path=json_path,
            markdown_path=markdown_path,
            dashboard_json_path=dashboard_json_path,
            dashboard_markdown_path=dashboard_markdown_path,
        )

    @classmethod
    def summarize_recommendation_report(cls, report: HabitRecommendationReport) -> str:
        if not report.recommendations:
            return (
                f"habit report refreshed: agents={report.agents_considered}, "
                f"habits={report.habits_considered}, shared={len(report.shared_patterns)}, "
                f"copy_pending={len([item for item in report.copy_recommendations if item.status == cls.COPY_APPROVAL_STATUS_PENDING])}, "
                "no actionable recommendations"
            )
        return (
            f"habit report refreshed: agents={report.agents_considered}, habits={report.habits_considered}, "
            f"recommendations={len(report.recommendations)}, shared={len(report.shared_patterns)}, "
            f"copy_pending={len([item for item in report.copy_recommendations if item.status == cls.COPY_APPROVAL_STATUS_PENDING])}, "
            f"top={report.recommendations[0].recommendation_type}"
        )

    @staticmethod
    def _make_dashboard_bucket(name: str, *, bucket_type: str) -> dict[str, Any]:
        return {
            "name": name,
            "bucket_type": bucket_type,
            "habits": 0,
            "triggered": 0,
            "helpful": 0,
            "harmful": 0,
            "ignored": 0,
            "recommendations": 0,
            "models": {},
        }

    @classmethod
    def _update_dashboard_bucket(
        cls,
        bucket: dict[str, Any],
        *,
        habit_count: int,
        recommendation_count: int,
        triggered: int,
        helpful: int,
        harmful: int,
        ignored: int,
    ) -> None:
        bucket["habits"] = int(bucket.get("habits", 0)) + int(habit_count)
        bucket["recommendations"] = int(bucket.get("recommendations", 0)) + int(recommendation_count)
        bucket["triggered"] = int(bucket.get("triggered", 0)) + int(triggered)
        bucket["helpful"] = int(bucket.get("helpful", 0)) + int(helpful)
        bucket["harmful"] = int(bucket.get("harmful", 0)) + int(harmful)
        bucket["ignored"] = int(bucket.get("ignored", 0)) + int(ignored)

    @classmethod
    def _finalize_dashboard_bucket(cls, bucket: dict[str, Any]) -> dict[str, Any]:
        helpful = int(bucket.get("helpful", 0))
        harmful = int(bucket.get("harmful", 0))
        triggered = int(bucket.get("triggered", 0))
        evidence_total = helpful + harmful
        finalized = dict(bucket)
        finalized["observe_more"] = max(0, triggered - evidence_total)
        finalized["helpful_rate"] = round(_safe_ratio(helpful, evidence_total), 4)
        finalized["harmful_rate"] = round(_safe_ratio(harmful, evidence_total), 4)
        finalized["evidence_quality"] = round(_wilson_lower_bound(helpful, evidence_total), 4)
        if finalized.get("bucket_type") == "backend":
            models = finalized.get("models") or {}
            finalized["models"] = [
                {"model": model_name, "agents": count}
                for model_name, count in sorted(
                    models.items(),
                    key=lambda item: (-int(item[1]), str(item[0])),
                )
            ]
        else:
            finalized.pop("models", None)
        return finalized

    @classmethod
    def _build_copy_recommendations_for_row(
        cls,
        *,
        project_root: Path,
        agent_registry: dict[str, dict[str, str]],
        source_agent_id: str,
        row: sqlite3.Row,
        helpful_recent: int,
        harmful_recent: int,
        triggered_recent: int,
    ) -> list[HabitCopyRecommendation]:
        status = str(row["status"] or "")
        task_type = str(row["task_type"] or "").strip() or None
        confidence = float(row["confidence"] or 0.0)
        source_agent_class = str((agent_registry.get(source_agent_id) or {}).get("agent_class") or "general").strip().lower()
        if status != "active":
            return []
        if task_type not in {"coordination_hchat", "scheduling_cron"}:
            return []
        if helpful_recent < cls.MIN_SAMPLE_FOR_COPY_RECOMMENDATION:
            return []
        if harmful_recent > 0:
            return []
        if triggered_recent < cls.MIN_SAMPLE_FOR_COPY_RECOMMENDATION:
            return []
        if confidence < cls.MIN_CONFIDENCE_FOR_COPY_RECOMMENDATION:
            return []

        title = str(row["title"] or row["instruction"] or row["habit_id"]).strip()
        recommendations: list[HabitCopyRecommendation] = []
        for agent_id, meta in sorted(agent_registry.items()):
            if agent_id == source_agent_id:
                continue
            if meta.get("is_active") != "1":
                continue
            if meta.get("agent_class") != source_agent_class:
                continue
            target_workspace = Path(meta["workspace_dir"])
            target_db = target_workspace / "habits.sqlite"
            if cls._target_has_equivalent_habit(
                target_db=target_db,
                task_type=task_type,
                instruction=str(row["instruction"] or ""),
                copied_from_habit_id=str(row["habit_id"] or ""),
            ):
                continue
            recommendations.append(
                HabitCopyRecommendation(
                    recommendation_id=0,
                    source_agent_id=source_agent_id,
                    source_habit_id=str(row["habit_id"] or ""),
                    source_title=title,
                    source_agent_class=source_agent_class,
                    target_agent_id=agent_id,
                    target_agent_class=str(meta.get("agent_class") or "general"),
                    task_type=task_type,
                    status=cls.COPY_APPROVAL_STATUS_PENDING,
                    confidence=confidence,
                    helpful_recent=helpful_recent,
                    harmful_recent=harmful_recent,
                    triggered_recent=triggered_recent,
                    summary=(
                        f"copy `{title[:60]}` from {source_agent_id} to {agent_id}; "
                        f"task={task_type}, helpful={helpful_recent}, confidence={confidence:.2f}"
                    ),
                    generated_at=_now_event_iso(),
                )
            )
            if len(recommendations) >= cls.MAX_COPY_TARGETS_PER_HABIT:
                break
        return recommendations

    @classmethod
    def _target_has_equivalent_habit(
        cls,
        *,
        target_db: Path,
        task_type: str | None,
        instruction: str,
        copied_from_habit_id: str,
    ) -> bool:
        if not target_db.exists():
            return False
        instruction_key = _normalize_text(instruction)
        with sqlite3.connect(target_db) as conn:
            conn.row_factory = sqlite3.Row
            columns = {
                str(item["name"]).strip().lower()
                for item in conn.execute("PRAGMA table_info(habits)").fetchall()
            }
            select_columns = ["instruction", "task_type"]
            if "copied_from_habit_id" in columns:
                select_columns.append("copied_from_habit_id")
            rows = conn.execute(
                f"""
                SELECT {', '.join(select_columns)}
                FROM habits
                WHERE enabled = 1
                """
            ).fetchall()
        for row in rows:
            existing_copied_from = str(row["copied_from_habit_id"] or "").strip() if "copied_from_habit_id" in row.keys() else ""
            if copied_from_habit_id and existing_copied_from == copied_from_habit_id:
                return True
            if task_type and str(row["task_type"] or "").strip() != task_type:
                continue
            if instruction_key and _normalize_text(str(row["instruction"] or "")) == instruction_key:
                return True
        return False

    @classmethod
    def _sync_copy_recommendations(
        cls,
        *,
        eval_conn: sqlite3.Connection,
        instance: str,
        generated_at: str,
        copy_recommendations: list[HabitCopyRecommendation],
    ) -> list[HabitCopyRecommendation]:
        touched_keys: set[tuple[str, str, str]] = set()
        for item in copy_recommendations:
            touched_keys.add((item.source_agent_id, item.source_habit_id, item.target_agent_id))
            eval_conn.execute(
                """
                INSERT INTO habit_copy_recommendations (
                    instance, source_agent_id, source_agent_class, source_habit_id,
                    target_agent_id, target_agent_class, task_type, confidence,
                    helpful_recent, harmful_recent, triggered_recent, generated_at,
                    summary, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(instance, source_agent_id, source_habit_id, target_agent_id)
                DO UPDATE SET
                    source_agent_class = excluded.source_agent_class,
                    target_agent_class = excluded.target_agent_class,
                    task_type = excluded.task_type,
                    confidence = excluded.confidence,
                    helpful_recent = excluded.helpful_recent,
                    harmful_recent = excluded.harmful_recent,
                    triggered_recent = excluded.triggered_recent,
                    generated_at = excluded.generated_at,
                    summary = excluded.summary,
                    status = CASE
                        WHEN habit_copy_recommendations.status IN ('applied', 'rejected') THEN habit_copy_recommendations.status
                        ELSE habit_copy_recommendations.status
                    END
                """,
                (
                    instance,
                    item.source_agent_id,
                    item.source_agent_class,
                    item.source_habit_id,
                    item.target_agent_id,
                    item.target_agent_class,
                    item.task_type,
                    item.confidence,
                    item.helpful_recent,
                    item.harmful_recent,
                    item.triggered_recent,
                    generated_at,
                    item.summary,
                    item.status,
                ),
            )

        if touched_keys:
            existing = eval_conn.execute(
                """
                SELECT id, source_agent_id, source_habit_id, target_agent_id, status
                FROM habit_copy_recommendations
                WHERE instance = ?
                """,
                (instance,),
            ).fetchall()
            for row in existing:
                key = (str(row["source_agent_id"]), str(row["source_habit_id"]), str(row["target_agent_id"]))
                if key in touched_keys:
                    continue
                if str(row["status"] or "") in {cls.COPY_APPROVAL_STATUS_PENDING, cls.COPY_APPROVAL_STATUS_APPROVED}:
                    eval_conn.execute(
                        """
                        UPDATE habit_copy_recommendations
                        SET status = ?, review_note = COALESCE(review_note, ?)
                        WHERE id = ?
                        """,
                        (cls.COPY_APPROVAL_STATUS_OBSOLETE, "source no longer qualifies for copy recommendation", int(row["id"])),
                    )

        return cls._load_copy_recommendations(eval_conn, instance, limit=200)

    @classmethod
    def _load_copy_recommendations(
        cls,
        eval_conn: sqlite3.Connection,
        instance: str,
        *,
        status: str | None = None,
        limit: int = 200,
    ) -> list[HabitCopyRecommendation]:
        params: list[Any] = [instance]
        where = ["instance = ?"]
        if status:
            where.append("status = ?")
            params.append(status)
        params.append(max(1, limit))
        rows = eval_conn.execute(
            f"""
            SELECT *
            FROM habit_copy_recommendations
            WHERE {' AND '.join(where)}
            ORDER BY
                CASE status
                    WHEN 'pending' THEN 0
                    WHEN 'approved' THEN 1
                    WHEN 'applied' THEN 2
                    WHEN 'rejected' THEN 3
                    ELSE 4
                END,
                generated_at DESC,
                id DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
        return [
            HabitCopyRecommendation(
                recommendation_id=int(row["id"]),
                source_agent_id=str(row["source_agent_id"]),
                source_habit_id=str(row["source_habit_id"]),
                source_title=cls._load_habit_title(
                    project_root=None,
                    source_agent_id=str(row["source_agent_id"]),
                    source_habit_id=str(row["source_habit_id"]),
                    fallback=str(row["summary"] or ""),
                ),
                source_agent_class=str(row["source_agent_class"] or "general"),
                target_agent_id=str(row["target_agent_id"]),
                target_agent_class=str(row["target_agent_class"] or "general"),
                task_type=str(row["task_type"] or "").strip() or None,
                status=str(row["status"] or cls.COPY_APPROVAL_STATUS_PENDING),
                confidence=float(row["confidence"] or 0.0),
                helpful_recent=int(row["helpful_recent"] or 0),
                harmful_recent=int(row["harmful_recent"] or 0),
                triggered_recent=int(row["triggered_recent"] or 0),
                summary=str(row["summary"] or ""),
                generated_at=str(row["generated_at"] or ""),
                reviewed_by=str(row["reviewed_by"] or "").strip() or None,
                reviewed_at=str(row["reviewed_at"] or "").strip() or None,
                review_note=str(row["review_note"] or "").strip() or None,
                copied_habit_id=str(row["copied_habit_id"] or "").strip() or None,
            )
            for row in rows
        ]

    @classmethod
    def _load_habit_title(
        cls,
        *,
        project_root: Path | None,
        source_agent_id: str,
        source_habit_id: str,
        fallback: str,
    ) -> str:
        if project_root is None:
            return fallback
        habit_db = cls._get_workspace_dir_for_agent(project_root, source_agent_id) / "habits.sqlite"
        if not habit_db.exists():
            return fallback
        with sqlite3.connect(habit_db) as conn:
            row = conn.execute("SELECT title, instruction FROM habits WHERE habit_id = ?", (source_habit_id,)).fetchone()
        if not row:
            return fallback
        return str((row[0] or row[1] or fallback)).strip()

    @classmethod
    def list_copy_recommendations(
        cls,
        *,
        project_root: Path,
        status: str | None = None,
        limit: int = 50,
    ) -> list[HabitCopyRecommendation]:
        eval_db_path = project_root / "workspaces" / "lily" / "habit_evaluation.sqlite"
        if not eval_db_path.exists():
            return []
        with cls._connect_eval_db(eval_db_path) as conn:
            return cls._load_copy_recommendations(conn, detect_instance(project_root), status=status, limit=limit)

    @classmethod
    def _load_shared_patterns(
        cls,
        eval_conn: sqlite3.Connection,
        instance: str,
        *,
        status: str | None = None,
        agent_class: str | None = None,
        limit: int = 100,
    ) -> list[SharedPattern]:
        params: list[Any] = [instance]
        where = ["instance = ?"]
        if status:
            where.append("status = ?")
            params.append(status)
        if agent_class:
            where.append("target_agent_class IN (?, 'general')")
            params.append(agent_class)
        params.append(max(1, limit))
        rows = eval_conn.execute(
            f"""
            SELECT *
            FROM shared_patterns
            WHERE {' AND '.join(where)}
            ORDER BY
                CASE kind WHEN 'protocol' THEN 0 ELSE 1 END,
                confidence DESC,
                updated_at DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
        return [
            SharedPattern(
                shared_pattern_id=str(row["id"]),
                kind=str(row["kind"] or cls.SHARED_PATTERN_KIND_PATTERN),
                status=str(row["status"] or cls.SHARED_PATTERN_STATUS_ACTIVE),
                title=str(row["title"] or row["instruction"] or row["id"]).strip(),
                habit_type=str(row["habit_type"] or "do").strip(),
                instruction=str(row["instruction"] or "").strip(),
                rationale=str(row["rationale"] or "").strip() or None,
                task_type=str(row["task_type"] or "").strip() or None,
                target_agent_class=str(row["target_agent_class"] or "general").strip().lower(),
                owner=str(row["owner"] or "lily").strip(),
                confidence=float(row["confidence"] or 0.0),
                helpful_recent=int(row["times_helpful"] or 0),
                harmful_recent=int(row["times_harmful"] or 0),
                triggered_recent=int(row["times_triggered"] or 0),
                source_agent_id=str(row["source_agent_id"] or "").strip() or None,
                source_habit_id=str(row["source_habit_id"] or "").strip() or None,
                created_at=str(row["created_at"] or ""),
                updated_at=str(row["updated_at"] or ""),
                promoted_by=str(row["promoted_by"] or "").strip() or None,
                promoted_at=str(row["promoted_at"] or "").strip() or None,
                retired_by=str(row["retired_by"] or "").strip() or None,
                retired_at=str(row["retired_at"] or "").strip() or None,
                governance_note=str(row["governance_note"] or "").strip() or None,
            )
            for row in rows
        ]

    @classmethod
    def list_shared_patterns(
        cls,
        *,
        project_root: Path,
        status: str | None = None,
        agent_class: str | None = None,
        limit: int = 50,
    ) -> list[SharedPattern]:
        eval_db_path = project_root / "workspaces" / "lily" / "habit_evaluation.sqlite"
        if not eval_db_path.exists():
            return []
        with cls._connect_eval_db(eval_db_path) as conn:
            return cls._load_shared_patterns(
                conn,
                detect_instance(project_root),
                status=status,
                agent_class=agent_class,
                limit=limit,
            )

    @classmethod
    def export_shared_pattern_registry(cls, *, project_root: Path) -> Path:
        eval_db_path = project_root / "workspaces" / "lily" / "habit_evaluation.sqlite"
        report_dir = project_root / "workspaces" / "lily" / cls.REPORT_DIRNAME
        report_dir.mkdir(parents=True, exist_ok=True)
        target = report_dir / "shared_registry.md"
        patterns = cls.list_shared_patterns(project_root=project_root, limit=200)
        lines = [
            "# Shared Pattern Registry",
            "",
            f"- Exported at: {_now_iso()}",
            f"- Active entries: {len([item for item in patterns if item.status == cls.SHARED_PATTERN_STATUS_ACTIVE])}",
            f"- Registry DB: {eval_db_path}",
            "",
            "## Entries",
        ]
        if patterns:
            for item in patterns:
                lines.append(
                    "- "
                    f"{item.shared_pattern_id} [{item.kind}/{item.status}] class={item.target_agent_class} "
                    f"owner={item.owner} source={item.source_agent_id or 'n/a'} "
                    f"title={item.title} helpful={item.helpful_recent} harmful={item.harmful_recent} "
                    f"triggered={item.triggered_recent}"
                )
                lines.append(f"  instruction: {item.instruction}")
                if item.governance_note:
                    lines.append(f"  governance: {item.governance_note}")
        else:
            lines.append("- None yet.")
        target.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return target

    @classmethod
    def promote_habit_to_shared_pattern(
        cls,
        *,
        project_root: Path,
        reviewer: str,
        source_agent_id: str,
        habit_id: str,
        kind: str = "pattern",
        target_agent_class: str | None = None,
        owner: str | None = None,
        note: str | None = None,
    ) -> SharedPattern:
        normalized_kind = (kind or cls.SHARED_PATTERN_KIND_PATTERN).strip().lower()
        if normalized_kind not in {cls.SHARED_PATTERN_KIND_PATTERN, cls.SHARED_PATTERN_KIND_PROTOCOL}:
            raise ValueError(f"Unsupported shared kind: {kind}")
        registry = cls._load_agent_registry(project_root)
        source_workspace = cls._get_workspace_dir_for_agent(project_root, source_agent_id)
        source_db = source_workspace / "habits.sqlite"
        if not source_db.exists():
            raise ValueError(f"Source habit store not found for agent '{source_agent_id}'")
        with sqlite3.connect(source_db) as source_conn:
            source_conn.row_factory = sqlite3.Row
            row = source_conn.execute(
                """
                SELECT habit_id, status, habit_type, title, instruction, rationale, task_type, trigger_json, confidence
                FROM habits
                WHERE habit_id = ?
                """,
                (habit_id,),
            ).fetchone()
        if not row:
            raise ValueError(f"Habit not found: {habit_id}")
        if str(row["status"] or "") != "active":
            raise ValueError("Only active habits can be promoted to shared registry")

        resolved_agent_class = (
            str(target_agent_class or (registry.get(source_agent_id) or {}).get("agent_class") or "general")
            .strip()
            .lower()
        )
        resolved_owner = str(owner or reviewer).strip() or reviewer
        eval_db_path = project_root / "workspaces" / "lily" / "habit_evaluation.sqlite"
        instance = detect_instance(project_root)
        with cls._connect_eval_db(eval_db_path) as eval_conn:
            stats = eval_conn.execute(
                """
                SELECT
                    COALESCE(SUM(CASE WHEN triggered = 1 THEN 1 ELSE 0 END), 0) AS triggered_count,
                    COALESCE(SUM(CASE WHEN helpful = 1 THEN 1 ELSE 0 END), 0) AS helpful_count,
                    COALESCE(SUM(CASE WHEN harmful = 1 THEN 1 ELSE 0 END), 0) AS harmful_count
                FROM habit_events
                WHERE instance = ? AND agent_id = ? AND habit_id = ?
                """,
                (instance, source_agent_id, habit_id),
            ).fetchone()
            helpful_count = int(stats["helpful_count"] or 0)
            harmful_count = int(stats["harmful_count"] or 0)
            triggered_count = int(stats["triggered_count"] or 0)
            confidence = float(row["confidence"] or 0.0)
            if helpful_count < cls.MIN_HELPFUL_FOR_SHARED_PROMOTION:
                raise ValueError(
                    f"Promotion requires helpful>={cls.MIN_HELPFUL_FOR_SHARED_PROMOTION}; got {helpful_count}"
                )
            if triggered_count < cls.MIN_TRIGGERED_FOR_SHARED_PROMOTION:
                raise ValueError(
                    f"Promotion requires triggered>={cls.MIN_TRIGGERED_FOR_SHARED_PROMOTION}; got {triggered_count}"
                )
            if harmful_count > 0:
                raise ValueError(f"Promotion requires harmful=0; got {harmful_count}")
            if confidence < cls.MIN_CONFIDENCE_FOR_SHARED_PROMOTION:
                raise ValueError(
                    f"Promotion requires confidence>={cls.MIN_CONFIDENCE_FOR_SHARED_PROMOTION:.2f}; got {confidence:.2f}"
                )

            existing = eval_conn.execute(
                """
                SELECT id, status
                FROM shared_patterns
                WHERE instance = ?
                  AND source_agent_id = ?
                  AND source_habit_id = ?
                  AND kind = ?
                  AND target_agent_class = ?
                """,
                (instance, source_agent_id, habit_id, normalized_kind, resolved_agent_class),
            ).fetchone()
            pattern_id = str(existing["id"]) if existing else generate_shared_pattern_id(instance)
            now = _now_event_iso()
            scope = "system_protocol" if normalized_kind == cls.SHARED_PATTERN_KIND_PROTOCOL else "shared_pattern"
            governance_note = (
                (note or "").strip()
                or f"Promoted by {reviewer} from {source_agent_id}/{habit_id}; owner={resolved_owner}"
            )
            eval_conn.execute(
                """
                INSERT INTO shared_patterns (
                    id, instance, kind, status, source_agent_id, source_habit_id,
                    target_agent_class, owner, title, habit_type, instruction, rationale,
                    task_type, trigger_json, scope, confidence, times_triggered,
                    times_applied, times_helpful, times_harmful, last_helpful_at,
                    governance_note, promoted_by, promoted_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    kind = excluded.kind,
                    status = excluded.status,
                    target_agent_class = excluded.target_agent_class,
                    owner = excluded.owner,
                    title = excluded.title,
                    habit_type = excluded.habit_type,
                    instruction = excluded.instruction,
                    rationale = excluded.rationale,
                    task_type = excluded.task_type,
                    trigger_json = excluded.trigger_json,
                    scope = excluded.scope,
                    confidence = excluded.confidence,
                    times_triggered = excluded.times_triggered,
                    times_applied = excluded.times_applied,
                    times_helpful = excluded.times_helpful,
                    times_harmful = excluded.times_harmful,
                    last_helpful_at = excluded.last_helpful_at,
                    governance_note = excluded.governance_note,
                    promoted_by = excluded.promoted_by,
                    promoted_at = excluded.promoted_at,
                    updated_at = excluded.updated_at
                """,
                (
                    pattern_id,
                    instance,
                    normalized_kind,
                    cls.SHARED_PATTERN_STATUS_ACTIVE,
                    source_agent_id,
                    habit_id,
                    resolved_agent_class,
                    resolved_owner,
                    str(row["title"] or row["instruction"] or habit_id).strip(),
                    str(row["habit_type"] or "do").strip(),
                    str(row["instruction"] or "").strip(),
                    str(row["rationale"] or "").strip() or None,
                    str(row["task_type"] or "").strip() or None,
                    str(row["trigger_json"] or "{}"),
                    scope,
                    confidence,
                    triggered_count,
                    triggered_count,
                    helpful_count,
                    harmful_count,
                    _now_iso(),
                    governance_note,
                    reviewer,
                    now,
                    now,
                    now,
                ),
            )
            self_changed_type = "promote" if not existing else "refresh_promotion"
            cls._record_shared_pattern_change(
                eval_conn,
                shared_pattern_id=pattern_id,
                change_type=self_changed_type,
                old_value=str(existing["status"]) if existing else None,
                new_value=cls.SHARED_PATTERN_STATUS_ACTIVE,
                reason=governance_note,
                changed_by=reviewer,
            )
            pattern = cls._load_shared_patterns(eval_conn, instance, limit=200)
            selected = next((item for item in pattern if item.shared_pattern_id == pattern_id), None)
            if not selected:
                raise ValueError(f"Failed to load promoted shared pattern: {pattern_id}")
        cls.export_shared_pattern_registry(project_root=project_root)
        return selected

    @classmethod
    def retire_shared_pattern(
        cls,
        *,
        project_root: Path,
        reviewer: str,
        shared_pattern_id: str,
        note: str | None = None,
    ) -> SharedPattern:
        eval_db_path = project_root / "workspaces" / "lily" / "habit_evaluation.sqlite"
        instance = detect_instance(project_root)
        retired_at = _now_event_iso()
        with cls._connect_eval_db(eval_db_path) as eval_conn:
            row = eval_conn.execute(
                """
                SELECT status
                FROM shared_patterns
                WHERE instance = ? AND id = ?
                """,
                (instance, shared_pattern_id),
            ).fetchone()
            if not row:
                raise ValueError(f"Shared pattern not found: {shared_pattern_id}")
            eval_conn.execute(
                """
                UPDATE shared_patterns
                SET status = ?, retired_by = ?, retired_at = ?, governance_note = ?, updated_at = ?
                WHERE instance = ? AND id = ?
                """,
                (
                    cls.SHARED_PATTERN_STATUS_RETIRED,
                    reviewer,
                    retired_at,
                    (note or "").strip() or f"Retired by {reviewer}",
                    retired_at,
                    instance,
                    shared_pattern_id,
                ),
            )
            cls._record_shared_pattern_change(
                eval_conn,
                shared_pattern_id=shared_pattern_id,
                change_type="retire",
                old_value=str(row["status"] or ""),
                new_value=cls.SHARED_PATTERN_STATUS_RETIRED,
                reason=(note or "").strip() or None,
                changed_by=reviewer,
            )
            selected = cls._load_shared_patterns(eval_conn, instance, limit=200)
            retired = next((item for item in selected if item.shared_pattern_id == shared_pattern_id), None)
            if retired is None:
                retired_rows = cls._load_shared_patterns(
                    eval_conn,
                    instance,
                    status=cls.SHARED_PATTERN_STATUS_RETIRED,
                    limit=200,
                )
                retired = next((item for item in retired_rows if item.shared_pattern_id == shared_pattern_id), None)
            if retired is None:
                raise ValueError(f"Failed to load retired shared pattern: {shared_pattern_id}")
        cls.export_shared_pattern_registry(project_root=project_root)
        return retired

    @classmethod
    def approve_copy_recommendations(
        cls,
        *,
        project_root: Path,
        reviewer: str,
        recommendation_ids: list[int] | None = None,
        note: str | None = None,
    ) -> list[HabitCopyRecommendation]:
        eval_db_path = project_root / "workspaces" / "lily" / "habit_evaluation.sqlite"
        if not eval_db_path.exists():
            return []
        instance = detect_instance(project_root)
        reviewed_at = _now_event_iso()
        with cls._connect_eval_db(eval_db_path) as conn:
            params: list[Any] = [cls.COPY_APPROVAL_STATUS_APPROVED, reviewer, reviewed_at, (note or "").strip() or None, instance]
            where = ["instance = ?", "status = ?"]
            params.append(cls.COPY_APPROVAL_STATUS_PENDING)
            if recommendation_ids:
                placeholders = ", ".join("?" for _ in recommendation_ids)
                where.append(f"id IN ({placeholders})")
                params.extend(recommendation_ids)
            conn.execute(
                f"""
                UPDATE habit_copy_recommendations
                SET status = ?, reviewed_by = ?, reviewed_at = ?, review_note = ?
                WHERE {' AND '.join(where)}
                """,
                params,
            )
            return cls._load_copy_recommendations(conn, instance, status=cls.COPY_APPROVAL_STATUS_APPROVED, limit=max(1, len(recommendation_ids or [])) if recommendation_ids else 50)

    @classmethod
    def reject_copy_recommendations(
        cls,
        *,
        project_root: Path,
        reviewer: str,
        recommendation_ids: list[int] | None = None,
        note: str | None = None,
    ) -> list[HabitCopyRecommendation]:
        eval_db_path = project_root / "workspaces" / "lily" / "habit_evaluation.sqlite"
        if not eval_db_path.exists():
            return []
        instance = detect_instance(project_root)
        reviewed_at = _now_event_iso()
        with cls._connect_eval_db(eval_db_path) as conn:
            params: list[Any] = [cls.COPY_APPROVAL_STATUS_REJECTED, reviewer, reviewed_at, (note or "").strip() or None, instance]
            where = ["instance = ?", "status IN (?, ?)"]
            params.extend([cls.COPY_APPROVAL_STATUS_PENDING, cls.COPY_APPROVAL_STATUS_APPROVED])
            if recommendation_ids:
                placeholders = ", ".join("?" for _ in recommendation_ids)
                where.append(f"id IN ({placeholders})")
                params.extend(recommendation_ids)
            conn.execute(
                f"""
                UPDATE habit_copy_recommendations
                SET status = ?, reviewed_by = ?, reviewed_at = ?, review_note = ?
                WHERE {' AND '.join(where)}
                """,
                params,
            )
            return cls._load_copy_recommendations(conn, instance, status=cls.COPY_APPROVAL_STATUS_REJECTED, limit=max(1, len(recommendation_ids or [])) if recommendation_ids else 50)

    @classmethod
    def apply_approved_copy_recommendations(
        cls,
        *,
        project_root: Path,
        reviewer: str,
        limit: int | None = None,
    ) -> list[HabitCopyRecommendation]:
        eval_db_path = project_root / "workspaces" / "lily" / "habit_evaluation.sqlite"
        if not eval_db_path.exists():
            return []
        instance = detect_instance(project_root)
        applied_rows: list[HabitCopyRecommendation] = []
        registry = cls._load_agent_registry(project_root)
        with cls._connect_eval_db(eval_db_path) as eval_conn:
            approved_rows = eval_conn.execute(
                """
                SELECT *
                FROM habit_copy_recommendations
                WHERE instance = ? AND status = ?
                ORDER BY generated_at ASC, id ASC
                LIMIT ?
                """,
                (instance, cls.COPY_APPROVAL_STATUS_APPROVED, int(limit or 1000)),
            ).fetchall()
            for row in approved_rows:
                target_agent_id = str(row["target_agent_id"])
                source_agent_id = str(row["source_agent_id"])
                source_habit_id = str(row["source_habit_id"])
                target_workspace = cls._get_workspace_dir_for_agent(project_root, target_agent_id)
                target_workspace.mkdir(parents=True, exist_ok=True)
                target_store = HabitStore(
                    workspace_dir=target_workspace,
                    project_root=project_root,
                    agent_id=target_agent_id,
                    agent_class=(registry.get(target_agent_id) or {}).get("agent_class"),
                )
                source_workspace = cls._get_workspace_dir_for_agent(project_root, source_agent_id)
                source_db = source_workspace / "habits.sqlite"
                if not source_db.exists():
                    continue
                with sqlite3.connect(source_db) as source_conn:
                    source_conn.row_factory = sqlite3.Row
                    habit_row = source_conn.execute(
                        """
                        SELECT habit_id, habit_type, title, instruction, rationale, task_type,
                               trigger_json, confidence
                        FROM habits
                        WHERE habit_id = ?
                        """,
                        (source_habit_id,),
                    ).fetchone()
                if not habit_row:
                    continue
                copied_habit_id = target_store._find_equivalent_habit_id(
                    task_type=str(habit_row["task_type"] or "").strip() or None,
                    instruction=str(habit_row["instruction"] or ""),
                )
                trigger = _from_json(habit_row["trigger_json"], {})
                rationale = str(habit_row["rationale"] or "").strip()
                provenance = f"Copied from {source_agent_id}/{source_habit_id} after Lily approval."
                merged_rationale = f"{rationale} | {provenance}".strip(" |")
                if not copied_habit_id:
                    copied_habit_id = target_store.upsert_habit(
                        habit_type=str(habit_row["habit_type"] or "do"),
                        title=str(habit_row["title"] or ""),
                        instruction=str(habit_row["instruction"] or ""),
                        rationale=merged_rationale,
                        task_type=str(habit_row["task_type"] or "").strip() or None,
                        trigger=trigger,
                        confidence=min(float(habit_row["confidence"] or cls.CANDIDATE_BASE_CONFIDENCE), 0.8),
                        status="candidate",
                        enabled=True,
                        source="recommendation_copy",
                        scope="system_recommended",
                        copied_from_habit_id=source_habit_id,
                        copied_from_agent_id=source_agent_id,
                    )
                    with target_store._connect() as target_conn:
                        target_store.record_state_change(
                            habit_id=copied_habit_id,
                            change_type="copied_from_recommendation",
                            old_value=None,
                            new_value=source_habit_id,
                            reason=f"approved by {reviewer}",
                            conn=target_conn,
                        )
                eval_conn.execute(
                    """
                    UPDATE habit_copy_recommendations
                    SET status = ?, reviewed_by = ?, reviewed_at = ?, applied_at = ?, copied_habit_id = ?
                    WHERE id = ?
                    """,
                    (
                        cls.COPY_APPROVAL_STATUS_APPLIED,
                        reviewer,
                        _now_event_iso(),
                        _now_event_iso(),
                        copied_habit_id,
                        int(row["id"]),
                    ),
                )
                applied_rows.append(
                    HabitCopyRecommendation(
                        recommendation_id=int(row["id"]),
                        source_agent_id=source_agent_id,
                        source_habit_id=source_habit_id,
                        source_title=str(row["summary"] or ""),
                        source_agent_class=str(row["source_agent_class"] or "general"),
                        target_agent_id=target_agent_id,
                        target_agent_class=str(row["target_agent_class"] or "general"),
                        task_type=str(row["task_type"] or "").strip() or None,
                        status=cls.COPY_APPROVAL_STATUS_APPLIED,
                        confidence=float(row["confidence"] or 0.0),
                        helpful_recent=int(row["helpful_recent"] or 0),
                        harmful_recent=int(row["harmful_recent"] or 0),
                        triggered_recent=int(row["triggered_recent"] or 0),
                        summary=str(row["summary"] or ""),
                        generated_at=str(row["generated_at"] or ""),
                        reviewed_by=reviewer,
                        reviewed_at=_now_event_iso(),
                        review_note=str(row["review_note"] or ""),
                        copied_habit_id=copied_habit_id,
                    )
                )
        return applied_rows

    @classmethod
    def _recommendation_for_row(
        cls,
        *,
        agent_id: str,
        row: sqlite3.Row,
        lookback_days: int,
        helpful_recent: int,
        harmful_recent: int,
        ignored_recent: int,
        triggered_recent: int,
    ) -> HabitRecommendation | None:
        status = str(row["status"] or "candidate")
        title = str(row["title"] or row["instruction"] or row["habit_id"]).strip()
        task_type = str(row["task_type"] or "").strip() or None
        confidence = float(row["confidence"] or cls.CANDIDATE_BASE_CONFIDENCE)
        recommendation_type: str | None = None
        summary = ""

        if status == "active" and harmful_recent >= max(2, helpful_recent + 1):
            recommendation_type = "pause_active"
            summary = f"{agent_id}: pause `{title[:60]}`; recent harmful={harmful_recent}, helpful={helpful_recent}"
        elif status == "candidate" and helpful_recent >= cls.CANDIDATE_PROMOTION_HELPFUL_MIN and harmful_recent == 0:
            recommendation_type = "promote_candidate"
            summary = f"{agent_id}: promote `{title[:60]}`; helpful={helpful_recent} in last {lookback_days}d"
        elif status == "candidate" and harmful_recent >= cls.CANDIDATE_DISABLE_HARMFUL_MIN and harmful_recent >= helpful_recent:
            recommendation_type = "disable_candidate"
            summary = f"{agent_id}: disable candidate `{title[:60]}`; harmful={harmful_recent}, helpful={helpful_recent}"
        elif status == "paused" and helpful_recent >= 2 and harmful_recent == 0:
            recommendation_type = "reactivate_paused"
            summary = f"{agent_id}: reactivate `{title[:60]}`; helpful signals recovered={helpful_recent}"
        elif status == "active" and ignored_recent >= max(3, helpful_recent + 2):
            recommendation_type = "patch_active"
            summary = f"{agent_id}: patch `{title[:60]}`; triggered={triggered_recent}, ignored={ignored_recent}"
        elif triggered_recent == 0 and helpful_recent == 0 and harmful_recent == 0:
            recommendation_type = "observe_more"
            summary = f"{agent_id}: observe `{title[:60]}` longer; no recent evidence"

        if recommendation_type is None:
            return None
        return HabitRecommendation(
            agent_id=agent_id,
            habit_id=str(row["habit_id"]),
            title=title,
            task_type=task_type,
            status=status,
            recommendation_type=recommendation_type,
            confidence=confidence,
            helpful_recent=helpful_recent,
            harmful_recent=harmful_recent,
            ignored_recent=ignored_recent,
            triggered_recent=triggered_recent,
            summary=summary,
        )

    @classmethod
    def _format_recommendation_report_markdown(
        cls,
        *,
        generated_at: str,
        generated_by: str,
        lookback_days: int,
        agent_summaries: list[dict[str, Any]],
        task_family_summaries: list[dict[str, Any]],
        class_summaries: list[dict[str, Any]],
        backend_summaries: list[dict[str, Any]],
        timestamp_source_summaries: list[dict[str, Any]],
        recommendations: list[HabitRecommendation],
        copy_recommendations: list[HabitCopyRecommendation],
        shared_patterns: list[SharedPattern],
    ) -> str:
        pending_copy = [item for item in copy_recommendations if item.status == cls.COPY_APPROVAL_STATUS_PENDING]
        lines = [
            "# Habit Recommendation Report",
            "",
            f"- Generated at: {generated_at}",
            f"- Generated by: {generated_by}",
            f"- Lookback: {lookback_days} days",
            f"- Agents considered: {len(agent_summaries)}",
            f"- Actionable recommendations: {len(recommendations)}",
            f"- Active shared patterns: {len(shared_patterns)}",
            f"- Pending copy approvals: {len(pending_copy)}",
            "",
            "## Top Recommendations",
        ]
        if recommendations:
            for item in recommendations:
                lines.append(
                    "- "
                    f"[{item.recommendation_type}] {item.summary} "
                    f"(task={item.task_type or 'n/a'}, status={item.status}, confidence={item.confidence:.2f})"
                )
        else:
            lines.append("- None yet.")

        lines.extend(["", "## Copy Recommendations"])
        if copy_recommendations:
            for item in copy_recommendations[:12]:
                lines.append(
                    "- "
                    f"[{item.status}] #{item.recommendation_id} {item.source_agent_id} -> {item.target_agent_id}: "
                    f"{item.summary}"
                )
        else:
            lines.append("- None yet.")

        lines.extend(["", "## Shared Patterns And Protocols"])
        if shared_patterns:
            for item in shared_patterns[:12]:
                lines.append(
                    "- "
                    f"[{item.kind}/{item.status}] {item.shared_pattern_id} "
                    f"class={item.target_agent_class} owner={item.owner} "
                    f"title={item.title} helpful={item.helpful_recent} harmful={item.harmful_recent} "
                    f"triggered={item.triggered_recent}"
                )
        else:
            lines.append("- None yet.")

        lines.extend(["", "## Agent Summaries"])
        if agent_summaries:
            for item in agent_summaries:
                lines.append(
                    "- "
                    f"{item['agent_id']}: habits={item['total_habits']}, "
                    f"active={item['active_habits']}, candidate={item['candidate_habits']}, "
                    f"paused={item['paused_habits']}, disabled={item['disabled_habits']}, "
                    f"recommendations={item['recommendation_count']}, "
                    f"class={item['agent_class']}, backend={item['backend']}, "
                    f"quality={item['evidence_quality']:.2f}"
                )
        else:
            lines.append("- No agent habit stores found.")

        lines.extend(["", "## Task Families"])
        if task_family_summaries:
            for item in task_family_summaries:
                lines.append(
                    "- "
                    f"{item['task_type']}: habits={item['habits']}, triggered={item['triggered']}, helpful={item['helpful']}, "
                    f"harmful={item['harmful']}, ignored={item['ignored']}, "
                    f"recommendations={item['recommendations']}, quality={item['evidence_quality']:.2f}"
                )
        else:
            lines.append("- No task-family data yet.")
        lines.extend(["", "## Agent Classes"])
        if class_summaries:
            for item in class_summaries:
                lines.append(
                    "- "
                    f"{item['name']}: habits={item['habits']}, triggered={item['triggered']}, "
                    f"helpful={item['helpful']}, harmful={item['harmful']}, "
                    f"recommendations={item['recommendations']}, quality={item['evidence_quality']:.2f}"
                )
        else:
            lines.append("- No class-level data yet.")
        lines.extend(["", "## Backends"])
        if backend_summaries:
            for item in backend_summaries:
                model_text = ", ".join(f"{entry['model']}({entry['agents']})" for entry in item.get("models", [])[:3]) or "n/a"
                lines.append(
                    "- "
                    f"{item['name']}: habits={item['habits']}, triggered={item['triggered']}, "
                    f"helpful={item['helpful']}, harmful={item['harmful']}, "
                    f"recommendations={item['recommendations']}, quality={item['evidence_quality']:.2f}, "
                    f"models={model_text}"
                )
        else:
            lines.append("- No backend-level data yet.")
        lines.extend(["", "## Timestamp Sources"])
        if timestamp_source_summaries:
            for item in timestamp_source_summaries:
                lines.append(f"- {item['ts_source']}: events={item['event_count']}")
        else:
            lines.append("- No timestamp-source data yet.")
        lines.append("")
        return "\n".join(lines)

    @classmethod
    def _format_dashboard_markdown(
        cls,
        *,
        generated_at: str,
        generated_by: str,
        lookback_days: int,
        overview: dict[str, Any],
        agent_summaries: list[dict[str, Any]],
        task_family_summaries: list[dict[str, Any]],
        class_summaries: list[dict[str, Any]],
        backend_summaries: list[dict[str, Any]],
        timestamp_source_summaries: list[dict[str, Any]],
    ) -> str:
        lines = [
            "# Habit Evaluation Dashboard",
            "",
            f"- Generated at: {generated_at}",
            f"- Generated by: {generated_by}",
            f"- Lookback: {lookback_days} days",
            f"- Agents considered: {overview.get('agents_considered', 0)}",
            f"- Habits considered: {overview.get('habits_considered', 0)}",
            f"- Actionable recommendations: {overview.get('actionable_recommendations', 0)}",
            f"- Pending copy approvals: {overview.get('pending_copy_approvals', 0)}",
            f"- Active shared patterns: {overview.get('active_shared_patterns', 0)}",
            "",
            "## Agent Quality",
        ]
        if agent_summaries:
            for item in agent_summaries[:12]:
                lines.append(
                    "- "
                    f"{item['agent_id']} [{item['agent_class']}/{item['backend']}]: "
                    f"quality={item['evidence_quality']:.2f}, helpful_rate={item['helpful_rate']:.2f}, "
                    f"harmful_rate={item['harmful_rate']:.2f}, triggered={item['triggered_recent']}, "
                    f"observe_more={item['observe_more_count']}"
                )
        else:
            lines.append("- No agent data yet.")
        lines.extend(["", "## Task Families"])
        if task_family_summaries:
            for item in task_family_summaries[:12]:
                lines.append(
                    "- "
                    f"{item['task_type']}: quality={item['evidence_quality']:.2f}, "
                    f"helpful_rate={item['helpful_rate']:.2f}, harmful={item['harmful']}, "
                    f"triggered={item['triggered']}, observe_more={item['observe_more']}"
                )
        else:
            lines.append("- No task-family data yet.")
        lines.extend(["", "## Agent Classes"])
        if class_summaries:
            for item in class_summaries:
                lines.append(
                    "- "
                    f"{item['name']}: quality={item['evidence_quality']:.2f}, helpful={item['helpful']}, "
                    f"harmful={item['harmful']}, triggered={item['triggered']}"
                )
        else:
            lines.append("- No class-level data yet.")
        lines.extend(["", "## Backends"])
        if backend_summaries:
            for item in backend_summaries:
                model_text = ", ".join(f"{entry['model']}({entry['agents']})" for entry in item.get("models", [])[:4]) or "n/a"
                lines.append(
                    "- "
                    f"{item['name']}: quality={item['evidence_quality']:.2f}, helpful={item['helpful']}, "
                    f"harmful={item['harmful']}, triggered={item['triggered']}, models={model_text}"
                )
        else:
            lines.append("- No backend-level data yet.")
        lines.extend(["", "## Timestamp Sources"])
        if timestamp_source_summaries:
            total_events = sum(int(item.get("event_count", 0)) for item in timestamp_source_summaries)
            for item in timestamp_source_summaries:
                share = _safe_ratio(int(item.get("event_count", 0)), total_events)
                lines.append(f"- {item['ts_source']}: events={item['event_count']}, share={share:.2f}")
        else:
            lines.append("- No timestamp-source data yet.")
        lines.append("")
        return "\n".join(lines)

    def _build_context_summary(
        self,
        *,
        prompt: str,
        summary: str,
        response_text: str | None,
        error_text: str | None,
        success: bool,
    ) -> str:
        parts = [
            f"summary={summary.strip()[:180]}",
            f"success={'1' if success else '0'}",
        ]
        if prompt:
            parts.append(f"prompt={_normalize_text(prompt)[:220]}")
        if response_text:
            parts.append(f"response={_normalize_text(response_text)[:220]}")
        if error_text:
            parts.append(f"error={_normalize_text(error_text)[:220]}")
        return " | ".join(parts)

    def _review_single_habit(
        self,
        *,
        conn: sqlite3.Connection,
        eval_conn: sqlite3.Connection,
        row: sqlite3.Row,
        review_cutoff: datetime,
    ) -> dict[str, Any]:
        habit_id = str(row["habit_id"])
        old_status = str(row["status"] or "candidate")
        title = str(row["title"] or row["instruction"] or habit_id).strip()
        old_confidence = float(row["confidence"] or self.CANDIDATE_BASE_CONFIDENCE)
        stats = self._load_recent_habit_stats(eval_conn, habit_id=habit_id, review_cutoff=review_cutoff)
        new_status = old_status
        new_confidence = old_confidence
        changed = False
        promoted = False
        paused = False
        disabled = False
        decayed = False
        recommendation = ""

        stale_days = self._habit_stale_days(row)
        helpful_recent = stats["helpful"]
        harmful_recent = stats["harmful"]
        ignored_recent = stats["ignored"]
        triggered_recent = stats["triggered"]

        if old_status == "candidate":
            if helpful_recent >= self.CANDIDATE_PROMOTION_HELPFUL_MIN and harmful_recent == 0:
                new_status = "active"
                promoted = True
                changed = True
                recommendation = f"activate `{title[:60]}`: {helpful_recent} recent helpful signals"
            elif harmful_recent >= self.CANDIDATE_DISABLE_HARMFUL_MIN and harmful_recent >= helpful_recent:
                new_status = "disabled"
                disabled = True
                changed = True
                recommendation = f"disable candidate `{title[:60]}`: {harmful_recent} recent harmful signals"
            elif stale_days is not None and stale_days >= self.CANDIDATE_STALE_DAYS and helpful_recent == 0:
                new_confidence = max(0.05, old_confidence - self.DECAY_STEP)
                decayed = new_confidence < old_confidence
                changed = changed or decayed
                recommendation = f"candidate `{title[:60]}` is stale; consider patching trigger keywords"
                if new_confidence <= 0.15:
                    new_status = "disabled"
                    disabled = True
                    changed = True
                    recommendation = f"disable stale candidate `{title[:60]}` after {stale_days}d without proof"
        elif old_status == "active":
            if harmful_recent >= max(2, helpful_recent + 1):
                new_status = "paused"
                paused = True
                changed = True
                recommendation = f"pause `{title[:60]}`: recent harmful>{helpful_recent}/{harmful_recent}"
            elif stale_days is not None and stale_days >= self.ACTIVE_STALE_DAYS and helpful_recent == 0:
                new_status = "paused"
                new_confidence = max(0.05, old_confidence - self.DECAY_STEP)
                paused = True
                decayed = new_confidence < old_confidence
                changed = True
                recommendation = f"pause stale active habit `{title[:60]}` after {stale_days}d inactivity"
            elif ignored_recent >= max(3, helpful_recent + 2):
                recommendation = f"patch `{title[:60]}`: triggered {triggered_recent} times but mostly ignored"
        elif old_status == "paused":
            if helpful_recent >= 2 and harmful_recent == 0:
                new_status = "active"
                promoted = True
                changed = True
                recommendation = f"reactivate `{title[:60]}`: helpful signals recovered"
            elif stale_days is not None and stale_days >= self.ACTIVE_STALE_DAYS + 14 and helpful_recent == 0:
                new_status = "disabled"
                disabled = True
                changed = True
                recommendation = f"disable paused stale habit `{title[:60]}` after prolonged inactivity"

        if not changed and not recommendation and helpful_recent == 0 and harmful_recent == 0 and triggered_recent == 0:
            recommendation = f"no recent evidence for `{title[:60]}` in last {max(1, (datetime.now().astimezone() - review_cutoff).days)}d"

        if changed:
            conn.execute(
                """
                UPDATE habits
                SET status = ?, confidence = ?, updated_at = ?
                WHERE habit_id = ?
                """,
                (new_status, new_confidence, _now_iso(), habit_id),
            )
            if new_status != old_status:
                self.record_state_change(
                    habit_id=habit_id,
                    change_type="nightly_review_status",
                    old_value=old_status,
                    new_value=new_status,
                    reason=recommendation or f"nightly review helpful={helpful_recent} harmful={harmful_recent}",
                    conn=conn,
                )
            if new_confidence != old_confidence:
                self.record_state_change(
                    habit_id=habit_id,
                    change_type="nightly_review_confidence",
                    old_value=f"{old_confidence:.2f}",
                    new_value=f"{new_confidence:.2f}",
                    reason=recommendation or f"nightly review stale_days={stale_days}",
                    conn=conn,
                )

        return {
            "habit_id": habit_id,
            "changed": changed,
            "promoted": promoted,
            "paused": paused,
            "disabled": disabled,
            "decayed": decayed,
            "recommendation": recommendation,
        }

    def _load_recent_habit_stats(
        self,
        eval_conn: sqlite3.Connection,
        *,
        habit_id: str,
        review_cutoff: datetime,
    ) -> dict[str, int]:
        row = eval_conn.execute(
            """
            SELECT
                COALESCE(SUM(CASE WHEN triggered = 1 THEN 1 ELSE 0 END), 0) AS triggered_count,
                COALESCE(SUM(CASE WHEN helpful = 1 THEN 1 ELSE 0 END), 0) AS helpful_count,
                COALESCE(SUM(CASE WHEN harmful = 1 THEN 1 ELSE 0 END), 0) AS harmful_count,
                COALESCE(SUM(CASE WHEN ignored = 1 THEN 1 ELSE 0 END), 0) AS ignored_count
            FROM habit_events
            WHERE instance = ? AND agent_id = ? AND habit_id = ? AND ts >= ?
            """,
            (self.instance, self.agent_id, habit_id, review_cutoff.isoformat(timespec="seconds")),
        ).fetchone()
        return {
            "triggered": int(row["triggered_count"] or 0),
            "helpful": int(row["helpful_count"] or 0),
            "harmful": int(row["harmful_count"] or 0),
            "ignored": int(row["ignored_count"] or 0),
        }

    def _habit_stale_days(self, row: sqlite3.Row) -> int | None:
        for key in ("last_triggered_at", "last_helpful_at", "updated_at", "created_at"):
            raw = str(row[key] or "").strip()
            if not raw:
                continue
            try:
                dt = datetime.fromisoformat(raw)
            except Exception:
                continue
            return max(0, (datetime.now().astimezone() - dt).days)
        return None

    def _response_signals_use(
        self,
        *,
        response_text: str | None,
        payload: dict[str, Any],
        task_type: str | None,
    ) -> bool:
        corpus = _normalize_text(response_text or "")
        if not corpus:
            return False

        title = _normalize_text(str(payload.get("title") or ""))
        instruction = _normalize_text(str(payload.get("instruction") or ""))
        for token in filter(None, (title, instruction)):
            if len(token) >= 8 and token[:48] in corpus:
                return True

        trigger = payload.get("trigger") or {}
        for field_name in ("keywords", "synonyms", "patterns"):
            for raw in trigger.get(field_name) or []:
                token = _normalize_text(str(raw))
                if token and token in corpus:
                    return True

        if task_type == "coordination_hchat":
            evidence = (
                "hchat", "lily", "mailbox", "handoff", "shared context",
                "shared memory", "小蕾", "共享上下文", "共享记忆", "跨 agent",
            )
            return any(token in corpus for token in evidence)
        if task_type == "scheduling_cron":
            evidence = (
                "cron", "scheduler", "systemd", "timer", "service",
                "heartbeat", "定时任务", "计划任务", "调度", "自动执行",
            )
            return any(token in corpus for token in evidence)
        return False

    def _classify_feedback(self, text: str) -> str:
        corpus = _normalize_text(text)
        if not corpus:
            return "neutral"

        negative_phrases = (
            "不对", "不行", "没用", "无效", "失败", "有问题", "报错", "错误", "坏了",
            "没解决", "没有解决", "不工作", "not work", "doesn't work", "didn't work",
            "wrong", "incorrect", "error", "failed", "broken", "bug",
        )
        positive_phrases = (
            "解决了", "搞定了", "有用了", "有帮助", "帮到我了", "正是我要的",
            "可以了", "行了", "成功了", "works", "worked", "fixed", "helpful",
            "that helps", "solved", "thank you this worked", "谢谢，解决了",
        )
        soft_positive = ("谢谢", "感谢", "多谢")
        neutral_control = ("开始吧", "继续", "下一步", "批准", "好的", "收到", "明白", "ok", "okay")

        if any(token in corpus for token in negative_phrases):
            return "negative"
        if any(token in corpus for token in positive_phrases):
            return "positive"
        if any(token == corpus for token in neutral_control):
            return "neutral"
        if any(token in corpus for token in soft_positive):
            return "positive"
        return "neutral"

    def _maybe_generate_candidate_habits(
        self,
        *,
        task_type: str | None,
        prompt: str,
        source: str,
        summary: str,
        response_text: str | None,
        error_text: str | None,
        success: bool,
        active_habits: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        if task_type not in {"coordination_hchat", "scheduling_cron"}:
            return []
        if any(_normalize_text(str(item.get("task_type") or "")) == task_type for item in active_habits):
            return []

        template = self._candidate_template_for_task(task_type=task_type, success=success)
        if not template:
            return []

        corpus = " ".join(part for part in (prompt, source, summary, response_text or "", error_text or "") if part)
        trigger = {
            "keywords": self._extract_candidate_keywords(task_type, corpus),
            "synonyms": self._candidate_synonyms_for_task(task_type),
        }
        habit_id = self._upsert_candidate_habit(
            habit_type=template["habit_type"],
            title=template["title"],
            instruction=template["instruction"],
            rationale=template["rationale"],
            task_type=task_type,
            trigger=trigger,
            success=success,
        )
        if not habit_id:
            return []
        return [
            {
                "habit_id": habit_id,
                "habit_type": template["habit_type"],
                "title": template["title"],
                "instruction": template["instruction"],
                "task_type": task_type,
                "trigger": trigger,
            }
        ]

    def _candidate_template_for_task(self, *, task_type: str, success: bool) -> dict[str, str] | None:
        templates: dict[tuple[str, bool], dict[str, str]] = {
            ("coordination_hchat", True): {
                "habit_type": "do",
                "title": "Use Lily For Shared Context",
                "instruction": "遇到共享上下文、跨 agent 协作或系统记忆相关请求时，优先通过 Lily、Hchat 或 mailbox 获取桥接证据，不要假设当前会话记忆可靠。",
                "rationale": "runtime success pattern for cross-agent/shared-context coordination",
            },
            ("coordination_hchat", False): {
                "habit_type": "avoid",
                "title": "Avoid Session-Only Coordination Assumptions",
                "instruction": "处理共享上下文、跨 agent 协作或系统记忆问题时，不要只依赖当前会话记忆；先查 Lily、Hchat、mailbox 或其它 bridge 证据。",
                "rationale": "runtime failure pattern for cross-agent/shared-context coordination",
            },
            ("scheduling_cron", True): {
                "habit_type": "do",
                "title": "Use HASHI Cron System",
                "instruction": "处理定时任务、heartbeat 或服务调度请求时，直接使用 HASHI 的 cron/tasks 系统落地配置，并明确说明是否需要重启或重载才会生效。",
                "rationale": "runtime success pattern for cron/scheduler execution",
            },
            ("scheduling_cron", False): {
                "habit_type": "avoid",
                "title": "Avoid Advice-Only Scheduler Replies",
                "instruction": "处理定时任务、heartbeat 或服务调度请求时，不要只给口头建议；要落实到 HASHI cron/tasks 配置，并核对生效条件。",
                "rationale": "runtime failure pattern for cron/scheduler execution",
            },
        }
        return templates.get((task_type, success))

    def _candidate_synonyms_for_task(self, task_type: str) -> list[str]:
        if task_type == "coordination_hchat":
            return ["shared context", "shared memory", "handoff", "mailbox", "小蕾", "共享上下文", "共享记忆"]
        if task_type == "scheduling_cron":
            return ["cron", "scheduler", "heartbeat", "tasks.json", "定时任务", "计划任务", "调度"]
        return []

    def _extract_candidate_keywords(self, task_type: str, corpus: str) -> list[str]:
        normalized = _normalize_text(corpus)
        keywords: list[str] = []
        if task_type == "coordination_hchat":
            candidates = ["lily", "hchat", "mailbox", "handoff", "小蕾", "共享上下文", "共享记忆", "跨 agent"]
        elif task_type == "scheduling_cron":
            candidates = ["cron", "scheduler", "heartbeat", "tasks.json", "systemd", "timer", "定时任务", "计划任务", "调度"]
        else:
            candidates = []
        for token in candidates:
            normalized_token = _normalize_text(token)
            if normalized_token and normalized_token in normalized:
                keywords.append(token)
        if not keywords:
            keywords.extend(candidates[:3])
        return keywords[:6]

    def _upsert_candidate_habit(
        self,
        *,
        habit_type: str,
        title: str,
        instruction: str,
        rationale: str,
        task_type: str,
        trigger: dict[str, Any],
        success: bool,
    ) -> str | None:
        now = _now_iso()
        instruction_key = _normalize_text(instruction)
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT habit_id, trigger_json, confidence, status, instruction
                FROM habits
                WHERE agent_id = ? AND task_type = ?
                ORDER BY
                    CASE status WHEN 'active' THEN 0 WHEN 'candidate' THEN 1 ELSE 2 END,
                    updated_at DESC
                """,
                (self.agent_id, task_type),
            ).fetchall()
            row = next(
                (
                    candidate_row
                    for candidate_row in rows
                    if _normalize_text(str(candidate_row["instruction"] or "")) == instruction_key
                ),
                None,
            )

            merged_trigger = trigger
            if row:
                existing_trigger = _from_json(row["trigger_json"], {})
                merged_trigger = self._merge_trigger_payload(existing_trigger, trigger)
                confidence = float(row["confidence"] or self.CANDIDATE_BASE_CONFIDENCE)
                confidence += 0.08 if success else 0.04
                conn.execute(
                    """
                    UPDATE habits
                    SET title = ?, rationale = ?, trigger_json = ?, confidence = ?, updated_at = ?
                    WHERE habit_id = ?
                    """,
                    (
                        title,
                        rationale,
                        _to_json(merged_trigger),
                        min(0.95, confidence),
                        now,
                        row["habit_id"],
                    ),
                )
                return str(row["habit_id"])

            habit_id = generate_habit_id(self.instance, self.agent_id)
            conn.execute(
                """
                INSERT INTO habits (
                    habit_id, version, agent_id, agent_class, status, enabled, habit_type,
                    title, instruction, rationale, scope, task_type, trigger_json,
                    source, confidence, created_at, updated_at
                ) VALUES (?, 1, ?, ?, 'candidate', 1, ?, ?, ?, ?, 'agent_local', ?, ?, 'runtime_candidate', ?, ?, ?)
                """,
                (
                    habit_id,
                    self.agent_id,
                    self.agent_class,
                    habit_type,
                    title,
                    instruction.strip(),
                    rationale,
                    task_type,
                    _to_json(merged_trigger),
                    self.CANDIDATE_BASE_CONFIDENCE,
                    now,
                    now,
                ),
            )
            self.record_state_change(
                habit_id=habit_id,
                change_type="create_candidate",
                old_value=None,
                new_value="candidate",
                reason=rationale,
                conn=conn,
            )
            return habit_id

    def _merge_trigger_payload(self, left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
        merged: dict[str, Any] = {}
        for key in {"keywords", "synonyms", "patterns"}:
            values: list[str] = []
            for raw in list(left.get(key) or []) + list(right.get(key) or []):
                token = str(raw).strip()
                if token and token not in values:
                    values.append(token)
            if values:
                merged[key] = values[:8]
        return merged

    def _find_equivalent_habit_id(self, *, task_type: str | None, instruction: str) -> str | None:
        instruction_key = _normalize_text(instruction)
        if not instruction_key:
            return None
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT habit_id, instruction, task_type
                FROM habits
                WHERE agent_id = ? AND enabled = 1
                ORDER BY updated_at DESC
                """,
                (self.agent_id,),
            ).fetchall()
        for row in rows:
            if task_type and str(row["task_type"] or "").strip() != task_type:
                continue
            if _normalize_text(str(row["instruction"] or "")) == instruction_key:
                return str(row["habit_id"])
        return None

    def _refresh_habit_status(
        self,
        conn: sqlite3.Connection,
        eval_conn: sqlite3.Connection,
        habit_id: str,
    ) -> None:
        row = conn.execute(
            """
            SELECT status, confidence, times_helpful, times_harmful
            FROM habits
            WHERE habit_id = ?
            """,
            (habit_id,),
        ).fetchone()
        if not row:
            return

        counts = eval_conn.execute(
            """
            SELECT
                COALESCE(SUM(CASE WHEN helpful = 1 THEN 1 ELSE 0 END), 0) AS helpful_count,
                COALESCE(SUM(CASE WHEN harmful = 1 THEN 1 ELSE 0 END), 0) AS harmful_count,
                MAX(CASE WHEN helpful = 1 THEN ts ELSE NULL END) AS last_helpful_ts
            FROM habit_events
            WHERE instance = ? AND agent_id = ? AND habit_id = ?
            """,
            (self.instance, self.agent_id, habit_id),
        ).fetchone()
        helpful_count = int(counts["helpful_count"] or 0)
        harmful_count = int(counts["harmful_count"] or 0)
        last_helpful_ts = counts["last_helpful_ts"]
        status = str(row["status"] or "candidate")
        old_status = status
        confidence = self.CANDIDATE_BASE_CONFIDENCE + min(helpful_count, 4) * 0.15 - min(harmful_count, 4) * 0.2
        confidence = max(0.05, min(0.95, confidence))

        if status == "candidate":
            if helpful_count >= self.CANDIDATE_PROMOTION_HELPFUL_MIN and helpful_count > harmful_count:
                status = "active"
            elif harmful_count >= self.CANDIDATE_DISABLE_HARMFUL_MIN and harmful_count >= helpful_count:
                status = "disabled"
        elif status == "active" and harmful_count >= helpful_count + 3:
            status = "paused"

        conn.execute(
            """
            UPDATE habits
            SET confidence = ?, status = ?, times_helpful = ?, times_harmful = ?,
                last_helpful_at = CASE WHEN ? IS NOT NULL THEN ? ELSE last_helpful_at END,
                updated_at = ?
            WHERE habit_id = ?
            """,
            (
                confidence,
                status,
                helpful_count,
                harmful_count,
                last_helpful_ts,
                last_helpful_ts,
                _now_iso(),
                habit_id,
            ),
        )
        if status != old_status:
            self.record_state_change(
                habit_id=habit_id,
                change_type="status",
                old_value=old_status,
                new_value=status,
                reason=f"helpful={helpful_count}, harmful={harmful_count}",
                conn=conn,
            )
