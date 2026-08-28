from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from orchestrator.audio_assets import (
    AudioAssetError,
    AudioAssetNotFound,
    AudioAssetStore,
    DEFAULT_RETENTION_SECONDS,
    MIN_RETENTION_SECONDS,
    normalize_audio_format,
)
from orchestrator.multimodal_contract import contains_persistent_inline_media

# ``importlib.reload`` reuses the module dictionary.  Preserve the class and
# exception identities already held by live runtimes; the handoff at the end
# of this module installs the newly defined implementation onto those objects.
_PRE_RELOAD_SESSION_STORE_CLASS = globals().get("SessionStore")
_PRE_RELOAD_SESSION_ERROR_CLASSES = {
    name: globals().get(name)
    for name in (
        "SessionStoreError",
        "SessionNotFound",
        "SessionConflict",
        "IdempotencyConflict",
        "StaleFencingToken",
    )
}

TERMINAL_RUN_STATES = frozenset(
    {"completed", "failed", "stopped", "superseded", "interrupted"}
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _json_object(value: str | None) -> dict[str, Any]:
    try:
        decoded = json.loads(value or "{}")
    except (TypeError, ValueError):
        return {}
    return dict(decoded) if isinstance(decoded, Mapping) else {}


class SessionStoreError(RuntimeError):
    code = "session_store_error"


class SessionNotFound(SessionStoreError):
    code = "session_not_found"


class SessionConflict(SessionStoreError):
    code = "session_conflict"


class IdempotencyConflict(SessionConflict):
    code = "idempotency_conflict"


class StaleFencingToken(SessionConflict):
    code = "stale_fencing_token"


@dataclass(frozen=True)
class AcceptedRun:
    session_id: str
    run_id: str
    message_id: str
    request_id: str
    context_generation: int
    replayed: bool = False


class SessionStore:
    """Transactional HASHI-owned conversation Session repository.

    The personal/local implementation deliberately lives under instance state,
    outside replaceable Agent workspaces. Raw Session records are canonical;
    per-Session working files are derived state used by Memory+ and Compact.
    """

    SCHEMA_VERSION = 4

    def __init__(self, db_path: str | Path, *, instance_id: str = "HASHI"):
        self.db_path = Path(db_path)
        self.instance_id = str(instance_id or "HASHI").upper()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.workspaces_root = self.db_path.parent / "session_workspaces"
        self.workspaces_root.mkdir(parents=True, exist_ok=True)
        self.audio_assets = AudioAssetStore(
            self.db_path.parent / "native_audio_assets"
        )
        self._lock = threading.RLock()
        self._initialize()

    @classmethod
    def from_global_config(cls, global_config: Any) -> SessionStore:
        bridge_home = Path(
            getattr(global_config, "bridge_home", None)
            or getattr(global_config, "project_root", None)
            or "."
        )
        return cls(
            bridge_home / "state" / "sessions.sqlite3",
            instance_id=str(getattr(global_config, "instance_id", "HASHI") or "HASHI"),
        )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.db_path, timeout=30.0, check_same_thread=False
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def _initialize(self) -> None:
        with self._lock, self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    instance_id TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    title_source TEXT NOT NULL DEFAULT 'system',
                    status TEXT NOT NULL DEFAULT 'active',
                    is_default INTEGER NOT NULL DEFAULT 0,
                    context_generation INTEGER NOT NULL DEFAULT 1,
                    memory_policy TEXT NOT NULL DEFAULT 'promote',
                    workzone TEXT,
                    revision INTEGER NOT NULL DEFAULT 1,
                    next_message_ordinal INTEGER NOT NULL DEFAULT 1,
                    next_event_sequence INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    deleted_at TEXT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS one_default_session_per_agent
                    ON sessions(instance_id, owner_id, agent_id)
                    WHERE is_default = 1;
                CREATE INDEX IF NOT EXISTS sessions_owner_agent_updated
                    ON sessions(instance_id, owner_id, agent_id, updated_at DESC);

                CREATE TABLE IF NOT EXISTS session_participants (
                    session_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    role TEXT NOT NULL DEFAULT 'presentation',
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(session_id, agent_id),
                    FOREIGN KEY(session_id) REFERENCES sessions(session_id)
                );

                CREATE TABLE IF NOT EXISTS session_context_generations (
                    session_id TEXT NOT NULL,
                    generation INTEGER NOT NULL,
                    reason TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(session_id, generation),
                    FOREIGN KEY(session_id) REFERENCES sessions(session_id)
                );

                CREATE TABLE IF NOT EXISTS messages (
                    message_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    run_id TEXT,
                    ordinal INTEGER NOT NULL,
                    context_generation INTEGER NOT NULL,
                    role TEXT NOT NULL,
                    author_id TEXT NOT NULL,
                    source TEXT NOT NULL,
                    content_json TEXT NOT NULL,
                    text TEXT NOT NULL,
                    visibility TEXT NOT NULL DEFAULT 'visible',
                    history_eligible INTEGER NOT NULL DEFAULT 1,
                    content_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(session_id, ordinal),
                    FOREIGN KEY(session_id) REFERENCES sessions(session_id)
                );
                CREATE INDEX IF NOT EXISTS messages_session_generation
                    ON messages(session_id, context_generation, ordinal);

                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    user_message_id TEXT NOT NULL,
                    final_message_id TEXT,
                    agent_id TEXT NOT NULL,
                    request_id TEXT NOT NULL UNIQUE,
                    idempotency_key TEXT NOT NULL,
                    request_digest TEXT NOT NULL,
                    source TEXT NOT NULL,
                    requested_mode TEXT,
                    effective_mode TEXT,
                    response_preferences_json TEXT NOT NULL DEFAULT '{}',
                    context_generation INTEGER NOT NULL,
                    state TEXT NOT NULL,
                    attempt INTEGER NOT NULL DEFAULT 0,
                    fencing_token INTEGER NOT NULL DEFAULT 0,
                    worker_id TEXT,
                    error_code TEXT,
                    error_text TEXT,
                    parent_run_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    started_at TEXT,
                    completed_at TEXT,
                    UNIQUE(session_id, idempotency_key),
                    FOREIGN KEY(session_id) REFERENCES sessions(session_id),
                    FOREIGN KEY(user_message_id) REFERENCES messages(message_id),
                    FOREIGN KEY(final_message_id) REFERENCES messages(message_id)
                );
                CREATE INDEX IF NOT EXISTS runs_session_created
                    ON runs(session_id, created_at, run_id);

                CREATE TABLE IF NOT EXISTS run_attempts (
                    run_id TEXT NOT NULL,
                    attempt INTEGER NOT NULL,
                    fencing_token INTEGER NOT NULL,
                    worker_id TEXT NOT NULL,
                    authorization_json TEXT NOT NULL DEFAULT '{}',
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    state TEXT NOT NULL,
                    PRIMARY KEY(run_id, attempt),
                    FOREIGN KEY(run_id) REFERENCES runs(run_id)
                );

                CREATE TABLE IF NOT EXISTS run_events (
                    event_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    run_id TEXT,
                    sequence INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    status TEXT,
                    phase TEXT,
                    delivery_class TEXT NOT NULL DEFAULT 'durable',
                    summary TEXT NOT NULL DEFAULT '',
                    detail_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    UNIQUE(session_id, sequence),
                    FOREIGN KEY(session_id) REFERENCES sessions(session_id),
                    FOREIGN KEY(run_id) REFERENCES runs(run_id)
                );

                CREATE TABLE IF NOT EXISTS run_projection_records (
                    run_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    projection_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(run_id) REFERENCES runs(run_id),
                    FOREIGN KEY(session_id) REFERENCES sessions(session_id)
                );

                CREATE TABLE IF NOT EXISTS idempotency_records (
                    session_id TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    request_digest TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(session_id, idempotency_key),
                    FOREIGN KEY(session_id) REFERENCES sessions(session_id),
                    FOREIGN KEY(run_id) REFERENCES runs(run_id)
                );

                CREATE TABLE IF NOT EXISTS channel_bindings (
                    instance_id TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    surface TEXT NOT NULL,
                    channel_key TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(instance_id, owner_id, agent_id, surface, channel_key),
                    FOREIGN KEY(session_id) REFERENCES sessions(session_id)
                );

                CREATE TABLE IF NOT EXISTS backend_bindings (
                    agent_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    context_generation INTEGER NOT NULL,
                    backend_id TEXT NOT NULL,
                    backend_thread_id TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(agent_id, session_id, context_generation, backend_id),
                    FOREIGN KEY(session_id) REFERENCES sessions(session_id)
                );

                CREATE TABLE IF NOT EXISTS session_capsules (
                    capsule_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    context_generation INTEGER NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(session_id) REFERENCES sessions(session_id)
                );

                CREATE TABLE IF NOT EXISTS agent_memory_records (
                    promotion_record_id TEXT PRIMARY KEY,
                    agent_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    run_id TEXT NOT NULL UNIQUE,
                    user_message_id TEXT NOT NULL,
                    assistant_message_id TEXT NOT NULL,
                    memory_origin_ref TEXT NOT NULL UNIQUE,
                    promoted_at TEXT NOT NULL,
                    FOREIGN KEY(session_id) REFERENCES sessions(session_id),
                    FOREIGN KEY(run_id) REFERENCES runs(run_id)
                );

                CREATE TABLE IF NOT EXISTS memory_promotion_watermarks (
                    agent_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    promoted_through_ordinal INTEGER NOT NULL DEFAULT 0,
                    promoted_at TEXT NOT NULL,
                    PRIMARY KEY(agent_id, session_id),
                    FOREIGN KEY(session_id) REFERENCES sessions(session_id)
                );

                CREATE TABLE IF NOT EXISTS memory_promotion_jobs (
                    job_id TEXT PRIMARY KEY,
                    agent_id TEXT NOT NULL,
                    session_id TEXT,
                    trigger_kind TEXT NOT NULL,
                    state TEXT NOT NULL,
                    promoted_count INTEGER NOT NULL DEFAULT 0,
                    error_text TEXT,
                    created_at TEXT NOT NULL,
                    completed_at TEXT
                );

                CREATE TABLE IF NOT EXISTS memory_promotion_schedules (
                    agent_id TEXT PRIMARY KEY,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    local_time TEXT NOT NULL DEFAULT '00:00',
                    timezone TEXT NOT NULL DEFAULT 'local',
                    last_local_date TEXT,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS event_consumers (
                    consumer_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    acknowledged_sequence INTEGER NOT NULL DEFAULT 0,
                    issued_through_sequence INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(session_id) REFERENCES sessions(session_id)
                );

                CREATE TABLE IF NOT EXISTS delivery_outbox (
                    outbox_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    run_id TEXT,
                    event_id TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'pending',
                    created_at TEXT NOT NULL,
                    delivered_at TEXT,
                    FOREIGN KEY(session_id) REFERENCES sessions(session_id),
                    FOREIGN KEY(event_id) REFERENCES run_events(event_id)
                );

                CREATE TABLE IF NOT EXISTS session_attachments (
                    attachment_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    media_type TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    sha256 TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'staged',
                    semantic_role TEXT NOT NULL DEFAULT '',
                    duration_ms INTEGER,
                    retention_seconds INTEGER,
                    retention_indefinite INTEGER NOT NULL DEFAULT 0,
                    upload_required INTEGER NOT NULL DEFAULT 0,
                    asset_id TEXT,
                    uploaded_at TEXT,
                    created_at TEXT NOT NULL,
                    committed_at TEXT,
                    FOREIGN KEY(session_id) REFERENCES sessions(session_id)
                );

                CREATE TABLE IF NOT EXISTS run_audio_assets (
                    run_id TEXT NOT NULL,
                    attachment_id TEXT NOT NULL,
                    asset_id TEXT NOT NULL,
                    direction TEXT NOT NULL DEFAULT 'input',
                    lease_released INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    released_at TEXT,
                    PRIMARY KEY(run_id, asset_id),
                    FOREIGN KEY(run_id) REFERENCES runs(run_id),
                    FOREIGN KEY(attachment_id) REFERENCES session_attachments(attachment_id)
                );

                CREATE TABLE IF NOT EXISTS voice_transcripts (
                    transcript_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    run_id TEXT,
                    message_id TEXT,
                    attachment_id TEXT NOT NULL,
                    text TEXT NOT NULL,
                    provenance TEXT NOT NULL,
                    safe_voice_state TEXT NOT NULL DEFAULT 'released',
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(session_id) REFERENCES sessions(session_id),
                    FOREIGN KEY(run_id) REFERENCES runs(run_id),
                    FOREIGN KEY(message_id) REFERENCES messages(message_id),
                    FOREIGN KEY(attachment_id) REFERENCES session_attachments(attachment_id)
                );

                CREATE TABLE IF NOT EXISTS runtime_event_correlations (
                    source_event_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    event_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(session_id) REFERENCES sessions(session_id),
                    FOREIGN KEY(run_id) REFERENCES runs(run_id),
                    FOREIGN KEY(event_id) REFERENCES run_events(event_id)
                );

                CREATE TABLE IF NOT EXISTS run_approvals (
                    approval_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    attempt INTEGER NOT NULL,
                    fencing_token INTEGER NOT NULL,
                    scope_json TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'pending',
                    decision TEXT,
                    created_at TEXT NOT NULL,
                    decided_at TEXT,
                    FOREIGN KEY(session_id) REFERENCES sessions(session_id),
                    FOREIGN KEY(run_id) REFERENCES runs(run_id)
                );
                """
            )
            consumer_columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(event_consumers)"
                ).fetchall()
            }
            if "issued_through_sequence" not in consumer_columns:
                connection.execute(
                    "ALTER TABLE event_consumers ADD COLUMN "
                    "issued_through_sequence INTEGER NOT NULL DEFAULT 0"
                )
            attachment_columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(session_attachments)"
                ).fetchall()
            }
            attachment_migrations = {
                "semantic_role": "TEXT NOT NULL DEFAULT ''",
                "duration_ms": "INTEGER",
                "retention_seconds": "INTEGER",
                "retention_indefinite": "INTEGER NOT NULL DEFAULT 0",
                "upload_required": "INTEGER NOT NULL DEFAULT 0",
                "asset_id": "TEXT",
                "uploaded_at": "TEXT",
            }
            for column, declaration in attachment_migrations.items():
                if column not in attachment_columns:
                    connection.execute(
                        f"ALTER TABLE session_attachments ADD COLUMN {column} {declaration}"
                    )
            run_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(runs)").fetchall()
            }
            if "response_preferences_json" not in run_columns:
                connection.execute(
                    "ALTER TABLE runs ADD COLUMN "
                    "response_preferences_json TEXT NOT NULL DEFAULT '{}'"
                )
            connection.execute(
                "INSERT OR REPLACE INTO schema_metadata(key, value) VALUES('schema_version', ?)",
                (str(self.SCHEMA_VERSION),),
            )

    @staticmethod
    def owner_id_for(global_config: Any, explicit: str | None = None) -> str:
        if explicit and str(explicit).strip():
            return str(explicit).strip()
        return f"user:{int(getattr(global_config, 'authorized_id', 0) or 0)}"

    @staticmethod
    def _session_dict(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["is_default"] = bool(result.get("is_default"))
        return result

    @staticmethod
    def _message_dict(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["content"] = json.loads(result.pop("content_json") or "[]")
        result["history_eligible"] = bool(result.get("history_eligible"))
        return result

    @staticmethod
    def _run_dict(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        if "response_preferences_json" in result:
            result["response_preferences"] = _json_object(
                result.pop("response_preferences_json")
            )
        return result

    def _next_ordinal(self, connection: sqlite3.Connection, session_id: str) -> int:
        row = connection.execute(
            "SELECT next_message_ordinal FROM sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if row is None:
            raise SessionNotFound(session_id)
        ordinal = int(row["next_message_ordinal"])
        connection.execute(
            "UPDATE sessions SET next_message_ordinal = ? WHERE session_id = ?",
            (ordinal + 1, session_id),
        )
        return ordinal

    def _append_event(
        self,
        connection: sqlite3.Connection,
        *,
        session_id: str,
        run_id: str | None,
        kind: str,
        status: str = "",
        phase: str = "",
        summary: str = "",
        detail: Mapping[str, Any] | None = None,
        outbox: bool = False,
    ) -> dict[str, Any]:
        row = connection.execute(
            "SELECT next_event_sequence FROM sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if row is None:
            raise SessionNotFound(session_id)
        sequence = int(row["next_event_sequence"])
        connection.execute(
            "UPDATE sessions SET next_event_sequence = ? WHERE session_id = ?",
            (sequence + 1, session_id),
        )
        event_id = _new_id("evt")
        created_at = _utc_now()
        connection.execute(
            """
            INSERT INTO run_events(
                event_id, session_id, run_id, sequence, kind, status, phase,
                summary, detail_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                session_id,
                run_id,
                sequence,
                kind,
                status or None,
                phase or None,
                summary,
                _json(dict(detail or {})),
                created_at,
            ),
        )
        if outbox:
            connection.execute(
                """
                INSERT INTO delivery_outbox(outbox_id, session_id, run_id, event_id, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (_new_id("out"), session_id, run_id, event_id, created_at),
            )
        return {
            "event_id": event_id,
            "session_id": session_id,
            "run_id": run_id,
            "sequence": sequence,
            "kind": kind,
            "status": status or None,
            "phase": phase or None,
            "summary": summary,
            "detail": dict(detail or {}),
            "created_at": created_at,
        }

    def create_session(
        self,
        *,
        owner_id: str,
        agent_id: str,
        title: str | None = None,
        is_default: bool = False,
    ) -> dict[str, Any]:
        owner_id = str(owner_id).strip()
        agent_id = str(agent_id).strip().lower()
        if not owner_id or not agent_id:
            raise ValueError("owner_id and agent_id are required")
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if is_default:
                row = connection.execute(
                    """
                    SELECT * FROM sessions
                    WHERE instance_id = ? AND owner_id = ? AND agent_id = ? AND is_default = 1
                    """,
                    (self.instance_id, owner_id, agent_id),
                ).fetchone()
                if row is not None:
                    return self._session_dict(row)
            session_id = _new_id("ses")
            now = _utc_now()
            resolved_title = str(
                title or (f"{agent_id} default" if is_default else "New session")
            ).strip()
            connection.execute(
                """
                INSERT INTO sessions(
                    session_id, instance_id, owner_id, agent_id, title,
                    is_default, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    self.instance_id,
                    owner_id,
                    agent_id,
                    resolved_title,
                    int(is_default),
                    now,
                    now,
                ),
            )
            connection.execute(
                "INSERT INTO session_participants(session_id, agent_id, created_at) VALUES (?, ?, ?)",
                (session_id, agent_id, now),
            )
            connection.execute(
                """
                INSERT INTO session_context_generations(session_id, generation, reason, created_at)
                VALUES (?, 1, 'session_created', ?)
                """,
                (session_id, now),
            )
            self._append_event(
                connection,
                session_id=session_id,
                run_id=None,
                kind="session.created",
                status="active",
                summary="Session created",
                detail={"is_default": bool(is_default), "agent_id": agent_id},
            )
            row = connection.execute(
                "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
            return self._session_dict(row)

    def ensure_default_session(self, *, owner_id: str, agent_id: str) -> dict[str, Any]:
        return self.create_session(
            owner_id=owner_id,
            agent_id=agent_id,
            title=f"{str(agent_id).strip().lower()} default",
            is_default=True,
        )

    def get_session(
        self,
        session_id: str,
        *,
        owner_id: str | None = None,
        agent_id: str | None = None,
        include_deleted: bool = True,
    ) -> dict[str, Any]:
        clauses = ["session_id = ?", "instance_id = ?"]
        params: list[Any] = [str(session_id), self.instance_id]
        if owner_id is not None:
            clauses.append("owner_id = ?")
            params.append(str(owner_id))
        if agent_id is not None:
            clauses.append("agent_id = ?")
            params.append(str(agent_id).lower())
        if not include_deleted:
            clauses.append("status != 'deleted'")
        with self._lock, self._connect() as connection:
            row = connection.execute(
                f"SELECT * FROM sessions WHERE {' AND '.join(clauses)}", params
            ).fetchone()
        if row is None:
            raise SessionNotFound(str(session_id))
        return self._session_dict(row)

    def list_sessions(
        self,
        *,
        owner_id: str,
        agent_id: str | None = None,
        include_archived: bool = False,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        clauses = ["instance_id = ?", "owner_id = ?", "status != 'deleted'"]
        params: list[Any] = [self.instance_id, str(owner_id)]
        if agent_id:
            clauses.append("agent_id = ?")
            params.append(str(agent_id).lower())
        if not include_archived:
            clauses.append("status = 'active'")
        params.append(max(1, min(int(limit), 500)))
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM sessions WHERE {" AND ".join(clauses)}
                ORDER BY is_default DESC, updated_at DESC, session_id ASC LIMIT ?
                """,
                params,
            ).fetchall()
        return [self._session_dict(row) for row in rows]

    def bind_channel(
        self,
        *,
        owner_id: str,
        agent_id: str,
        surface: str,
        channel_key: str,
        session_id: str,
    ) -> dict[str, Any]:
        session = self.get_session(
            session_id,
            owner_id=owner_id,
            agent_id=agent_id,
            include_deleted=False,
        )
        if session["status"] != "active":
            raise SessionConflict("only active Sessions can be selected")
        now = _utc_now()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO channel_bindings(
                    instance_id, owner_id, agent_id, surface, channel_key,
                    session_id, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(instance_id, owner_id, agent_id, surface, channel_key)
                DO UPDATE SET session_id = excluded.session_id, updated_at = excluded.updated_at
                """,
                (
                    self.instance_id,
                    str(owner_id),
                    str(agent_id).lower(),
                    str(surface).lower(),
                    str(channel_key),
                    str(session_id),
                    now,
                ),
            )
        return session

    def resolve_session(
        self,
        *,
        owner_id: str,
        agent_id: str,
        surface: str,
        channel_key: str,
        explicit_session_id: str | None = None,
        default_only: bool = False,
    ) -> dict[str, Any]:
        agent_id = str(agent_id).lower()
        default = self.ensure_default_session(owner_id=owner_id, agent_id=agent_id)
        if default_only:
            return default
        if explicit_session_id:
            return self.get_session(
                explicit_session_id,
                owner_id=owner_id,
                agent_id=agent_id,
                include_deleted=False,
            )
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT s.* FROM channel_bindings AS b
                JOIN sessions AS s ON s.session_id = b.session_id
                WHERE b.instance_id = ? AND b.owner_id = ? AND b.agent_id = ?
                  AND b.surface = ? AND b.channel_key = ? AND s.status = 'active'
                """,
                (
                    self.instance_id,
                    str(owner_id),
                    agent_id,
                    str(surface).lower(),
                    str(channel_key),
                ),
            ).fetchone()
        if row is not None:
            return self._session_dict(row)
        self.bind_channel(
            owner_id=owner_id,
            agent_id=agent_id,
            surface=surface,
            channel_key=channel_key,
            session_id=default["session_id"],
        )
        return default

    def accept_run(
        self,
        *,
        session_id: str,
        owner_id: str,
        agent_id: str,
        request_id: str,
        text: str,
        source: str,
        idempotency_key: str,
        execution_mode: str | None = None,
        content: Iterable[Mapping[str, Any]] | None = None,
        parent_run_id: str | None = None,
        response_preferences: Mapping[str, Any] | None = None,
    ) -> AcceptedRun:
        clean = str(text or "").strip()
        blocks = list(content or ({"type": "text", "text": clean},))
        if contains_persistent_inline_media(blocks):
            raise SessionConflict("Session content cannot contain inline media bytes")
        if not all(isinstance(block, Mapping) for block in blocks):
            raise ValueError("message content parts must be objects")
        if not clean:
            clean = "\n".join(
                str(block.get("text") or "").strip()
                for block in blocks
                if str(block.get("type") or "").strip().casefold() == "text"
                and str(block.get("text") or "").strip()
            ).strip()
        now = _utc_now()
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            session = connection.execute(
                """
                SELECT * FROM sessions
                WHERE session_id = ? AND instance_id = ? AND owner_id = ?
                  AND agent_id = ? AND status = 'active'
                """,
                (session_id, self.instance_id, str(owner_id), str(agent_id).lower()),
            ).fetchone()
            if session is None:
                raise SessionNotFound(session_id)
            audio_rows: list[sqlite3.Row] = []
            attachment_fingerprints: list[dict[str, Any]] = []
            normalized_blocks: list[dict[str, Any]] = []
            for raw_block in blocks:
                block = dict(raw_block)
                block_type = str(block.get("type") or "").strip().casefold()
                if block_type == "text":
                    if not isinstance(block.get("text"), str):
                        raise ValueError("text content parts require text")
                elif block_type == "audio":
                    attachment_id = str(block.get("attachment_id") or "").strip()
                    semantic_role = str(
                        block.get("semantic_role") or "audio_attachment"
                    ).strip().casefold()
                    if not attachment_id:
                        raise SessionConflict(
                            "audio content requires a committed attachment"
                        )
                    if semantic_role not in {"voice_message", "audio_attachment"}:
                        raise SessionConflict("invalid audio semantic role")
                    attachment = connection.execute(
                        """SELECT * FROM session_attachments
                           WHERE attachment_id=? AND session_id=? AND owner_id=?""",
                        (attachment_id, str(session_id), str(owner_id)),
                    ).fetchone()
                    if attachment is None:
                        raise SessionConflict(
                            "audio attachment is not authorized for this Session"
                        )
                    if str(attachment["state"]) != "committed":
                        raise SessionConflict("audio attachment is not committed")
                    if not str(attachment["media_type"]).casefold().startswith(
                        "audio/"
                    ):
                        raise SessionConflict("attachment is not audio")
                    asset_id = str(attachment["asset_id"] or "")
                    if not asset_id:
                        raise SessionConflict("audio attachment bytes are unavailable")
                    declared_mime = str(block.get("mime_type") or "").casefold()
                    if declared_mime and declared_mime != str(
                        attachment["media_type"]
                    ).casefold():
                        raise SessionConflict("audio MIME does not match committed metadata")
                    self.audio_assets.describe(
                        asset_id, owner_id=owner_id, session_id=session_id
                    )
                    block["semantic_role"] = semantic_role
                    block.setdefault("mime_type", str(attachment["media_type"]))
                    audio_rows.append(attachment)
                    attachment_fingerprints.append(
                        {
                            "attachment_id": attachment_id,
                            "sha256": str(attachment["sha256"]),
                            "size_bytes": int(attachment["size_bytes"]),
                            "semantic_role": semantic_role,
                        }
                    )
                elif not block_type:
                    raise ValueError("message content parts require a type")
                normalized_blocks.append(block)
            blocks = normalized_blocks
            if not clean and not audio_rows:
                raise ValueError("message requires text or a committed audio attachment")
            digest_payload = {
                "content": blocks,
                "attachments": attachment_fingerprints,
                "execution_mode": str(execution_mode or ""),
                "parent_run_id": str(parent_run_id or ""),
                "response_preferences": dict(response_preferences or {}),
            }
            digest = hashlib.sha256(
                _json(digest_payload).encode("utf-8")
            ).hexdigest()
            existing = connection.execute(
                """
                SELECT i.request_digest, r.* FROM idempotency_records AS i
                JOIN runs AS r ON r.run_id = i.run_id
                WHERE i.session_id = ? AND i.idempotency_key = ?
                """,
                (session_id, str(idempotency_key)),
            ).fetchone()
            if existing is not None:
                if str(existing["request_digest"]) != digest:
                    raise IdempotencyConflict(
                        "idempotency key is already bound to a different request"
                    )
                return AcceptedRun(
                    session_id=session_id,
                    run_id=str(existing["run_id"]),
                    message_id=str(existing["user_message_id"]),
                    request_id=str(existing["request_id"]),
                    context_generation=int(existing["context_generation"]),
                    replayed=True,
                )
            run_id = _new_id("run")
            message_id = _new_id("msg")
            generation = int(session["context_generation"])
            ordinal = self._next_ordinal(connection, session_id)
            content_json = _json(blocks)
            content_hash = hashlib.sha256(content_json.encode("utf-8")).hexdigest()
            connection.execute(
                """
                INSERT INTO messages(
                    message_id, session_id, run_id, ordinal, context_generation,
                    role, author_id, source, content_json, text, content_hash, created_at
                ) VALUES (?, ?, ?, ?, ?, 'user', ?, ?, ?, ?, ?, ?)
                """,
                (
                    message_id,
                    session_id,
                    run_id,
                    ordinal,
                    generation,
                    str(owner_id),
                    str(source),
                    content_json,
                    clean,
                    content_hash,
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO runs(
                    run_id, session_id, user_message_id, agent_id, request_id,
                    idempotency_key, request_digest, source, requested_mode,
                    effective_mode, response_preferences_json,
                    context_generation, state, parent_run_id,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?)
                """,
                (
                    run_id,
                    session_id,
                    message_id,
                    str(agent_id).lower(),
                    str(request_id),
                    str(idempotency_key),
                    digest,
                    str(source),
                    execution_mode,
                    execution_mode,
                    _json(dict(response_preferences or {})),
                    generation,
                    parent_run_id,
                    now,
                    now,
                ),
            )
            for attachment in audio_rows:
                asset_id = str(attachment["asset_id"])
                self.audio_assets.acquire(
                    asset_id, owner_id=owner_id, session_id=session_id
                )
                connection.execute(
                    """INSERT INTO run_audio_assets(
                           run_id, attachment_id, asset_id, direction, created_at
                       ) VALUES (?, ?, ?, 'input', ?)""",
                    (
                        run_id,
                        str(attachment["attachment_id"]),
                        asset_id,
                        now,
                    ),
                )
            connection.execute(
                """
                INSERT INTO idempotency_records(
                    session_id, owner_id, idempotency_key, request_digest,
                    run_id, message_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    str(owner_id),
                    str(idempotency_key),
                    digest,
                    run_id,
                    message_id,
                    now,
                ),
            )
            self._append_event(
                connection,
                session_id=session_id,
                run_id=run_id,
                kind="run.accepted",
                status="queued",
                phase="admission",
                summary="User message accepted",
                detail={"message_id": message_id, "request_id": request_id},
                outbox=True,
            )
            connection.execute(
                """
                UPDATE sessions SET updated_at = ?, revision = revision + 1
                WHERE session_id = ?
                """,
                (now, session_id),
            )
        return AcceptedRun(
            session_id=session_id,
            run_id=run_id,
            message_id=message_id,
            request_id=request_id,
            context_generation=generation,
        )

    def mark_request_running(self, request_id: str, *, worker_id: str) -> int | None:
        now = _utc_now()
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM runs WHERE request_id = ?", (str(request_id),)
            ).fetchone()
            if row is None or str(row["state"]) != "queued":
                return None
            attempt = int(row["attempt"]) + 1
            token = int(row["fencing_token"]) + 1
            updated = connection.execute(
                """
                UPDATE runs SET state = 'running', attempt = ?, fencing_token = ?,
                    worker_id = ?, started_at = ?, updated_at = ?
                WHERE run_id = ? AND state = 'queued' AND fencing_token = ?
                """,
                (
                    attempt,
                    token,
                    str(worker_id),
                    now,
                    now,
                    row["run_id"],
                    int(row["fencing_token"]),
                ),
            )
            if updated.rowcount != 1:
                return None
            connection.execute(
                """
                INSERT INTO run_attempts(
                    run_id, attempt, fencing_token, worker_id, started_at, state
                ) VALUES (?, ?, ?, ?, ?, 'running')
                """,
                (row["run_id"], attempt, token, str(worker_id), now),
            )
            self._append_event(
                connection,
                session_id=str(row["session_id"]),
                run_id=str(row["run_id"]),
                kind="run.started",
                status="running",
                phase="execution",
                summary="Run started",
                detail={"attempt": attempt, "fencing_token": token},
            )
            return token

    def _release_run_audio_leases(
        self, connection: sqlite3.Connection, *, run_id: str, released_at: str
    ) -> None:
        rows = connection.execute(
            """SELECT ra.asset_id, a.owner_id, a.session_id
               FROM run_audio_assets AS ra
               JOIN session_attachments AS a
                 ON a.attachment_id = ra.attachment_id
               WHERE ra.run_id=? AND ra.lease_released=0""",
            (str(run_id),),
        ).fetchall()
        for row in rows:
            try:
                self.audio_assets.release(
                    str(row["asset_id"]),
                    owner_id=str(row["owner_id"]),
                    session_id=str(row["session_id"]),
                )
            except AudioAssetNotFound:
                pass
        connection.execute(
            """UPDATE run_audio_assets SET lease_released=1, released_at=?
               WHERE run_id=? AND lease_released=0""",
            (released_at, str(run_id)),
        )

    def finish_request(
        self,
        request_id: str,
        *,
        success: bool,
        assistant_text: str | None = None,
        assistant_content: Iterable[Mapping[str, Any]] | None = None,
        assistant_source: str = "",
        error_text: str | None = None,
        failure_state: str = "failed",
        fencing_token: int | None = None,
    ) -> dict[str, Any] | None:
        now = _utc_now()
        clean = str(assistant_text or "").strip()
        supplied_content = list(assistant_content or ())
        if contains_persistent_inline_media(supplied_content):
            raise SessionConflict("assistant content cannot contain inline media bytes")
        normalized_content: list[dict[str, Any]] = []
        for raw_part in supplied_content:
            if not isinstance(raw_part, Mapping):
                raise ValueError("assistant content parts must be objects")
            part = dict(raw_part)
            part_type = str(part.get("type") or "").strip().casefold()
            if part_type == "text":
                if not isinstance(part.get("text"), str):
                    raise ValueError("assistant text parts require text")
                if not clean and str(part.get("text") or "").strip():
                    clean = str(part["text"]).strip()
            elif part_type == "audio":
                asset_id = str(part.get("asset_id") or "").strip()
                digest = str(part.get("sha256") or "").strip().casefold()
                if not asset_id:
                    raise ValueError("assistant audio parts require asset_id")
                if digest and (
                    len(digest) != 64
                    or any(character not in "0123456789abcdef" for character in digest)
                ):
                    raise ValueError("assistant audio sha256 is invalid")
            else:
                raise ValueError(f"unsupported assistant content type {part_type!r}")
            normalized_content.append(part)
        if not normalized_content and clean:
            normalized_content = [{"type": "text", "text": clean}]
        deliverable_audio = any(
            part.get("type") == "audio" and str(part.get("asset_id") or "").strip()
            for part in normalized_content
        )
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            run = connection.execute(
                "SELECT * FROM runs WHERE request_id = ?", (str(request_id),)
            ).fetchone()
            if run is None:
                return None
            current_state = str(run["state"])
            if current_state in TERMINAL_RUN_STATES:
                return self._run_dict(run)
            if fencing_token is not None and int(run["fencing_token"]) != int(
                fencing_token
            ):
                raise StaleFencingToken("executor fencing token is stale")
            session_id = str(run["session_id"])
            final_message_id = None
            if success:
                if not clean and not deliverable_audio:
                    success = False
                    error_text = (
                        error_text or "backend returned no visible final response"
                    )
                else:
                    content = normalized_content
                    content_json = _json(content)
                    content_hash = hashlib.sha256(
                        content_json.encode("utf-8")
                    ).hexdigest()
                    existing_output = connection.execute(
                        """SELECT message_id FROM messages
                           WHERE run_id=? AND role='assistant' AND content_hash=?
                           ORDER BY ordinal ASC LIMIT 1""",
                        (str(run["run_id"]), content_hash),
                    ).fetchone()
                    if existing_output is not None:
                        # Direct native audio was already projected at
                        # first-ready time.  Reuse that canonical Message when
                        # the Run later settles instead of duplicating it.
                        final_message_id = str(existing_output["message_id"])
                    else:
                        final_message_id = _new_id("msg")
                        ordinal = self._next_ordinal(connection, session_id)
                        connection.execute(
                            """
                            INSERT INTO messages(
                                message_id, session_id, run_id, ordinal,
                                context_generation, role, author_id, source,
                                content_json, text, content_hash, created_at
                            ) VALUES (?, ?, ?, ?, ?, 'assistant', ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                final_message_id,
                                session_id,
                                run["run_id"],
                                ordinal,
                                int(run["context_generation"]),
                                str(run["agent_id"]),
                                str(assistant_source or run["agent_id"]),
                                content_json,
                                clean,
                                content_hash,
                                now,
                            ),
                        )
                    if deliverable_audio and existing_output is None:
                        self._append_event(
                            connection,
                            session_id=session_id,
                            run_id=str(run["run_id"]),
                            kind="assistant.output.available",
                            status="available",
                            phase="final",
                            summary="Assistant audio output available",
                            detail={
                                "message_id": final_message_id,
                                "request_id": str(request_id),
                                "disposition": "final",
                                "content": content,
                            },
                            outbox=True,
                        )
            state = "completed" if success else str(failure_state or "failed")
            if state not in TERMINAL_RUN_STATES:
                state = "failed"
            error_code = None if success else "run_failed"
            connection.execute(
                """
                UPDATE runs SET state = ?, final_message_id = ?, error_code = ?,
                    error_text = ?, completed_at = ?, updated_at = ?
                WHERE run_id = ?
                """,
                (
                    state,
                    final_message_id,
                    error_code,
                    None if success else str(error_text or "run failed"),
                    now,
                    now,
                    run["run_id"],
                ),
            )
            connection.execute(
                """
                UPDATE run_attempts SET state = ?, finished_at = ?
                WHERE run_id = ? AND attempt = ?
                """,
                (state, now, run["run_id"], int(run["attempt"])),
            )
            event = self._append_event(
                connection,
                session_id=session_id,
                run_id=str(run["run_id"]),
                kind="run.completed" if success else f"run.{state}",
                status=state,
                phase="terminal",
                summary="Assistant response completed"
                if success
                else str(error_text or "Run failed"),
                detail={"message_id": final_message_id}
                if success
                else {"error": str(error_text or "run failed")},
                outbox=True,
            )
            projection = {
                "run_id": str(run["run_id"]),
                "session_id": session_id,
                "state": state,
                "user_message_id": str(run["user_message_id"]),
                "final_message_id": final_message_id,
                "error": None if success else str(error_text or "run failed"),
                "latest_event_sequence": event["sequence"],
            }
            connection.execute(
                """
                INSERT INTO run_projection_records(run_id, session_id, projection_json, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    projection_json = excluded.projection_json,
                    updated_at = excluded.updated_at
                """,
                (run["run_id"], session_id, _json(projection), now),
            )
            connection.execute(
                """
                UPDATE sessions SET updated_at = ?, revision = revision + 1
                WHERE session_id = ?
                """,
                (now, session_id),
            )
            self._release_run_audio_leases(
                connection, run_id=str(run["run_id"]), released_at=now
            )
            result = connection.execute(
                "SELECT * FROM runs WHERE run_id = ?", (run["run_id"],)
            ).fetchone()
            return self._run_dict(result)

    def cancel_run(
        self, run_id: str, *, owner_id: str, reason: str = "cancelled_by_user"
    ) -> dict[str, Any]:
        now = _utc_now()
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            run = connection.execute(
                """SELECT r.* FROM runs r JOIN sessions s ON s.session_id=r.session_id
                   WHERE r.run_id=? AND s.instance_id=? AND s.owner_id=?""",
                (str(run_id), self.instance_id, str(owner_id)),
            ).fetchone()
            if run is None:
                raise SessionNotFound(str(run_id))
            if str(run["state"]) in TERMINAL_RUN_STATES:
                return self._run_dict(run)
            connection.execute(
                """UPDATE runs SET state='stopped', fencing_token=fencing_token+1,
                   worker_id=NULL, error_code='run_cancelled', error_text=?,
                   completed_at=?, updated_at=? WHERE run_id=?""",
                (str(reason), now, now, str(run_id)),
            )
            connection.execute(
                "UPDATE run_attempts SET state='stopped', finished_at=? WHERE run_id=? AND state='running'",
                (now, str(run_id)),
            )
            self._append_event(
                connection,
                session_id=str(run["session_id"]),
                run_id=str(run_id),
                kind="run.stopped",
                status="stopped",
                phase="control",
                summary=str(reason),
                detail={"reason": str(reason)},
                outbox=True,
            )
            self._release_run_audio_leases(
                connection, run_id=str(run_id), released_at=now
            )
            row = connection.execute(
                "SELECT * FROM runs WHERE run_id=?", (str(run_id),)
            ).fetchone()
        return self._run_dict(row)

    def stage_attachment(
        self,
        *,
        session_id: str,
        owner_id: str,
        filename: str,
        media_type: str,
        size_bytes: int,
        sha256: str,
        semantic_role: str = "",
        duration_ms: int | None = None,
        retention_seconds: int = DEFAULT_RETENTION_SECONDS,
        retention_indefinite: bool = False,
        upload_required: bool | None = None,
    ) -> dict[str, Any]:
        self.get_session(session_id, owner_id=owner_id, include_deleted=False)
        digest = str(sha256).lower()
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise ValueError("sha256 must be 64 lowercase hexadecimal characters")
        normalized_media_type = (
            str(media_type or "").split(";", 1)[0].strip().casefold()
        )
        is_audio = normalized_media_type.startswith("audio/")
        normalized_role = str(semantic_role or "").strip().casefold()
        if is_audio:
            normalized_role = normalized_role or "audio_attachment"
            if normalized_role not in {"voice_message", "audio_attachment"}:
                raise ValueError(
                    "audio semantic_role must be voice_message or audio_attachment"
                )
            if duration_ms is not None and int(duration_ms) < 0:
                raise ValueError("duration_ms must be non-negative")
            if not retention_indefinite and int(retention_seconds) < MIN_RETENTION_SECONDS:
                raise ValueError("audio retention must be at least 60 seconds")
        elif normalized_role:
            raise ValueError("semantic_role is only supported for audio attachments")
        requires_upload = is_audio if upload_required is None else bool(upload_required)
        attachment_id, now = _new_id("att"), _utc_now()
        with self._lock, self._connect() as connection:
            connection.execute(
                """INSERT INTO session_attachments(attachment_id,session_id,owner_id,filename,
                   media_type,size_bytes,sha256,semantic_role,duration_ms,
                   retention_seconds,retention_indefinite,upload_required,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    attachment_id,
                    str(session_id),
                    str(owner_id),
                    str(filename),
                    normalized_media_type,
                    max(0, int(size_bytes)),
                    digest,
                    normalized_role,
                    int(duration_ms) if duration_ms is not None else None,
                    None if retention_indefinite else int(retention_seconds),
                    int(bool(retention_indefinite)),
                    int(requires_upload),
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM session_attachments WHERE attachment_id=?",
                (attachment_id,),
            ).fetchone()
        result = dict(row)
        result["retention_indefinite"] = bool(result["retention_indefinite"])
        result["upload_required"] = bool(result["upload_required"])
        return result

    def upload_attachment_bytes(
        self,
        *,
        session_id: str,
        owner_id: str,
        attachment_id: str,
        payload: bytes,
    ) -> dict[str, Any]:
        """Validate and atomically materialize one staged audio attachment."""

        if not isinstance(payload, bytes):
            raise ValueError("attachment payload must be bytes")
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """SELECT * FROM session_attachments
                   WHERE attachment_id=? AND session_id=? AND owner_id=?""",
                (str(attachment_id), str(session_id), str(owner_id)),
            ).fetchone()
        if row is None:
            raise SessionNotFound("attachment not found")
        attachment = dict(row)
        if attachment["state"] != "staged":
            raise SessionConflict("only staged attachments can be uploaded")
        actual_digest = hashlib.sha256(payload).hexdigest()
        if len(payload) != int(attachment["size_bytes"]):
            raise SessionConflict("attachment size does not match staged metadata")
        if actual_digest != str(attachment["sha256"]):
            raise SessionConflict("attachment digest does not match staged metadata")
        if not str(attachment["media_type"]).casefold().startswith("audio/"):
            raise SessionConflict("native audio upload requires an audio attachment")

        existing_asset = str(attachment.get("asset_id") or "")
        if existing_asset:
            try:
                self.audio_assets.describe(
                    existing_asset, owner_id=owner_id, session_id=session_id
                )
            except AudioAssetError as exc:
                raise SessionConflict("staged audio asset is unavailable") from exc
            return attachment

        audio_format = normalize_audio_format(
            Path(str(attachment["filename"])).suffix,
            mime_type=str(attachment["media_type"]),
        )
        asset = self.audio_assets.create(
            payload,
            owner_id=owner_id,
            session_id=session_id,
            direction="input",
            mime_type=str(attachment["media_type"]),
            audio_format=audio_format,
            asset_id=str(attachment_id),
            filename=str(attachment["filename"]),
            duration_ms=attachment.get("duration_ms"),
            retention_seconds=int(
                attachment.get("retention_seconds") or DEFAULT_RETENTION_SECONDS
            ),
            retention_indefinite=bool(attachment.get("retention_indefinite")),
            correlation={"attachment_id": str(attachment_id)},
        )
        now = _utc_now()
        with self._lock, self._connect() as connection:
            connection.execute(
                """UPDATE session_attachments SET asset_id=?, uploaded_at=?,
                       duration_ms=COALESCE(duration_ms, ?)
                   WHERE attachment_id=? AND state='staged'""",
                (
                    asset["asset_id"],
                    now,
                    asset.get("duration_ms"),
                    str(attachment_id),
                ),
            )
            updated = connection.execute(
                "SELECT * FROM session_attachments WHERE attachment_id=?",
                (str(attachment_id),),
            ).fetchone()
        return dict(updated)

    def commit_attachment(
        self, *, session_id: str, owner_id: str, attachment_id: str
    ) -> dict[str, Any]:
        now = _utc_now()
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM session_attachments WHERE attachment_id=? AND session_id=? AND owner_id=?",
                (str(attachment_id), str(session_id), str(owner_id)),
            ).fetchone()
            if row is None:
                raise SessionNotFound("attachment not found")
            if bool(row["upload_required"]) and not str(row["asset_id"] or ""):
                raise SessionConflict("attachment bytes must be uploaded before commit")
            if str(row["asset_id"] or ""):
                try:
                    self.audio_assets.describe(
                        str(row["asset_id"]),
                        owner_id=owner_id,
                        session_id=session_id,
                    )
                except AudioAssetError as exc:
                    raise SessionConflict("uploaded attachment is unavailable") from exc
            connection.execute(
                "UPDATE session_attachments SET state='committed', committed_at=COALESCE(committed_at,?) WHERE attachment_id=?",
                (now, str(attachment_id)),
            )
            updated = connection.execute(
                "SELECT * FROM session_attachments WHERE attachment_id=?",
                (str(attachment_id),),
            ).fetchone()
        result = dict(updated)
        result["retention_indefinite"] = bool(result["retention_indefinite"])
        result["upload_required"] = bool(result["upload_required"])
        return result

    def attachment_bytes(
        self, *, session_id: str, owner_id: str, attachment_id: str
    ) -> tuple[dict[str, Any], bytes]:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """SELECT * FROM session_attachments
                   WHERE attachment_id=? AND session_id=? AND owner_id=?
                     AND state='committed'""",
                (str(attachment_id), str(session_id), str(owner_id)),
            ).fetchone()
        if row is None or not str(row["asset_id"] or ""):
            raise SessionNotFound("attachment not found")
        return self.audio_assets.read_bytes(
            str(row["asset_id"]), owner_id=owner_id, session_id=session_id
        )

    def attachment_canonical_part(
        self,
        *,
        session_id: str,
        owner_id: str,
        attachment_id: str,
        item_index: int,
        semantic_role: str | None = None,
    ) -> dict[str, Any]:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """SELECT * FROM session_attachments
                   WHERE attachment_id=? AND session_id=? AND owner_id=?
                     AND state='committed'""",
                (str(attachment_id), str(session_id), str(owner_id)),
            ).fetchone()
        if row is None or not str(row["asset_id"] or ""):
            raise SessionNotFound("attachment not found")
        metadata, local_path = self.audio_assets.authorized_path(
            str(row["asset_id"]), owner_id=owner_id, session_id=session_id
        )
        role = str(semantic_role or row["semantic_role"] or "audio_attachment")
        return {
            "type": "media",
            "item_index": int(item_index),
            "attachment_id": str(attachment_id),
            "modality": "audio",
            "kind": "voice" if role == "voice_message" else "audio",
            "semantic_role": role,
            "mime_type": str(row["media_type"]),
            "filename": str(row["filename"]),
            "caption": "",
            "duration_ms": row["duration_ms"],
            "local_ref": str(local_path),
            "size_bytes": int(metadata["size_bytes"]),
            "sha256": str(metadata["sha256"]),
            "transport": {},
        }

    def audio_asset_bytes(
        self, *, session_id: str, owner_id: str, asset_id: str
    ) -> tuple[dict[str, Any], bytes]:
        return self.audio_assets.read_bytes(
            asset_id, owner_id=owner_id, session_id=session_id
        )

    def claim_output_audio_asset(
        self,
        *,
        session_id: str,
        owner_id: str,
        request_id: str,
        asset_id: str,
    ) -> dict[str, Any]:
        return self.audio_assets.claim(
            asset_id,
            owner_id=owner_id,
            session_id=session_id,
            request_id=request_id,
        )

    def audio_asset_path(
        self, *, session_id: str, owner_id: str, asset_id: str
    ) -> tuple[dict[str, Any], Path]:
        return self.audio_assets.authorized_path(
            asset_id, owner_id=owner_id, session_id=session_id
        )

    def acquire_audio_asset(
        self, *, session_id: str, owner_id: str, asset_id: str
    ) -> dict[str, Any]:
        return self.audio_assets.acquire(
            asset_id, owner_id=owner_id, session_id=session_id
        )

    def release_audio_asset(
        self, *, session_id: str, owner_id: str, asset_id: str
    ) -> dict[str, Any]:
        return self.audio_assets.release(
            asset_id, owner_id=owner_id, session_id=session_id
        )

    def archive_audio_asset(
        self, *, session_id: str, owner_id: str, asset_id: str
    ) -> dict[str, Any]:
        return self.audio_assets.set_indefinite(
            asset_id, owner_id=owner_id, session_id=session_id
        )

    def create_output_audio_asset(
        self,
        payload: bytes,
        *,
        session_id: str,
        owner_id: str,
        run_id: str,
        request_id: str,
        mime_type: str,
        audio_format: str,
        filename: str = "",
        duration_ms: int | None = None,
        retention_seconds: int = DEFAULT_RETENTION_SECONDS,
        retention_indefinite: bool = False,
        provider: str = "",
        model: str = "",
    ) -> dict[str, Any]:
        self.get_run(run_id, owner_id=owner_id)
        asset = self.audio_assets.create(
            payload,
            owner_id=owner_id,
            session_id=session_id,
            direction="output",
            mime_type=mime_type,
            audio_format=audio_format,
            filename=filename,
            duration_ms=duration_ms,
            retention_seconds=retention_seconds,
            retention_indefinite=retention_indefinite,
            correlation={
                "run_id": str(run_id),
                "request_id": str(request_id),
                "provider": str(provider),
                "model": str(model),
            },
        )
        return {
            "type": "audio",
            "asset_id": asset["asset_id"],
            "mime_type": asset["mime_type"],
            "format": asset["format"],
            "duration_ms": asset.get("duration_ms"),
            "size_bytes": asset["size_bytes"],
            "sha256": asset["sha256"],
            "retention_expires_at": asset.get("retention_expires_at"),
            "retention_indefinite": asset["retention_indefinite"],
        }

    def cleanup_audio_assets(self) -> list[dict[str, Any]]:
        expired = self.audio_assets.cleanup()
        if not expired:
            return []
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for asset in expired:
                asset_id = str(asset["asset_id"])
                connection.execute(
                    """UPDATE session_attachments SET state='expired'
                       WHERE asset_id=? AND state='committed'""",
                    (asset_id,),
                )
                session_id = str(asset.get("session_id") or "")
                if not session_id:
                    continue
                session_exists = connection.execute(
                    "SELECT 1 FROM sessions WHERE session_id=?",
                    (session_id,),
                ).fetchone()
                if session_exists is None:
                    continue
                run_id = str(
                    dict(asset.get("correlation") or {}).get("run_id") or ""
                ) or None
                if run_id is not None:
                    run_exists = connection.execute(
                        "SELECT 1 FROM runs WHERE run_id=? AND session_id=?",
                        (run_id, session_id),
                    ).fetchone()
                    if run_exists is None:
                        run_id = None
                self._append_event(
                    connection,
                    session_id=session_id,
                    run_id=run_id,
                    kind="audio.asset.expired",
                    status="expired",
                    phase="retention",
                    summary="Retained audio bytes expired",
                    detail={"asset_id": asset_id},
                    outbox=True,
                )
        return expired

    def record_voice_transcript(
        self,
        *,
        request_id: str,
        attachment_id: str,
        text: str,
        provenance: str,
        safe_voice_state: str,
    ) -> dict[str, Any]:
        clean = str(text or "").strip()
        state = str(safe_voice_state or "released").strip().casefold()
        if state not in {
            "released",
            "ready",
            "pending_confirmation",
            "discarded",
            "unavailable",
        }:
            raise ValueError("invalid Safe Voice transcript state")
        if not clean and state != "unavailable":
            raise ValueError("voice transcript cannot be empty")
        now = _utc_now()
        transcript_id = _new_id("transcript")
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            run = connection.execute(
                "SELECT * FROM runs WHERE request_id=?", (str(request_id),)
            ).fetchone()
            if run is None:
                raise SessionNotFound(str(request_id))
            attachment = connection.execute(
                """SELECT * FROM session_attachments
                   WHERE attachment_id=? AND session_id=?""",
                (str(attachment_id), str(run["session_id"])),
            ).fetchone()
            if attachment is None:
                raise SessionConflict("transcript attachment is not part of the Session")
            existing = connection.execute(
                """SELECT * FROM voice_transcripts
                   WHERE run_id=? AND attachment_id=?
                   ORDER BY created_at ASC LIMIT 1""",
                (str(run["run_id"]), str(attachment_id)),
            ).fetchone()
            if existing is not None:
                stored_state = str(existing["safe_voice_state"])
                state_is_compatible = stored_state == state or (
                    state == "pending_confirmation"
                    and stored_state in {"released", "discarded"}
                )
                if (
                    str(existing["text"]) != clean
                    or str(existing["provenance"])
                    != str(provenance or "local_stt")
                    or not state_is_compatible
                ):
                    raise SessionConflict(
                        "voice transcript replay conflicts with the stored record"
                    )
                return dict(existing)
            connection.execute(
                """INSERT INTO voice_transcripts(
                       transcript_id,session_id,run_id,message_id,attachment_id,
                       text,provenance,safe_voice_state,created_at
                   ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    transcript_id,
                    str(run["session_id"]),
                    str(run["run_id"]),
                    str(run["user_message_id"]),
                    str(attachment_id),
                    clean,
                    str(provenance or "local_stt"),
                    state,
                    now,
                ),
            )
            if state == "released":
                self._append_voice_transcript_projection(
                    connection,
                    message_id=str(run["user_message_id"]),
                    transcript=clean,
                )
            transcript_event_kind = (
                "voice.input.transcript_unavailable"
                if state == "unavailable"
                else "voice.input.transcript_ready"
            )
            self._append_event(
                connection,
                session_id=str(run["session_id"]),
                run_id=str(run["run_id"]),
                kind=transcript_event_kind,
                status=state,
                phase="transcription",
                summary=(
                    "Local input transcript unavailable"
                    if state == "unavailable"
                    else "Local input transcript available"
                ),
                detail={
                    "transcript_id": transcript_id,
                    "attachment_id": str(attachment_id),
                    "text": clean,
                    "provenance": str(provenance or "local_stt"),
                    "safe_voice_state": state,
                },
                outbox=True,
            )
            if state == "unavailable":
                self._append_event(
                    connection,
                    session_id=str(run["session_id"]),
                    run_id=str(run["run_id"]),
                    kind="voice.warning",
                    status="degraded",
                    phase="transcription",
                    summary=(
                        "Local voice transcription is unavailable; the native "
                        "audio response will continue."
                    ),
                    detail={
                        "transcript_id": transcript_id,
                        "attachment_id": str(attachment_id),
                        "warning_code": "local_stt_unavailable",
                    },
                    outbox=True,
                )
            if state == "pending_confirmation":
                self._append_event(
                    connection,
                    session_id=str(run["session_id"]),
                    run_id=str(run["run_id"]),
                    kind="voice.input.transcript_pending_confirmation",
                    status=state,
                    phase="safe_voice",
                    summary="Safe Voice confirmation required",
                    detail={
                        "transcript_id": transcript_id,
                        "attachment_id": str(attachment_id),
                        "text": clean,
                        "provenance": str(provenance or "local_stt"),
                        "safe_voice_state": state,
                    },
                    outbox=True,
                )
            row = connection.execute(
                "SELECT * FROM voice_transcripts WHERE transcript_id=?",
                (transcript_id,),
            ).fetchone()
        return dict(row)

    def require_voice_transcript_confirmation(
        self, *, request_id: str
    ) -> dict[str, Any]:
        """Move deferred native STT to Safe Voice only when a stage needs it."""

        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """SELECT vt.* FROM voice_transcripts AS vt
                   JOIN runs AS r ON r.run_id=vt.run_id
                   WHERE r.request_id=?
                   ORDER BY vt.created_at ASC, vt.transcript_id ASC""",
                (str(request_id),),
            ).fetchall()
            if not rows:
                raise SessionNotFound("voice transcript not found")
            eligible = [
                row for row in rows if str(row["safe_voice_state"]) == "ready"
            ]
            if not eligible:
                pending = [
                    row
                    for row in rows
                    if str(row["safe_voice_state"]) == "pending_confirmation"
                ]
                if pending:
                    return dict(pending[-1])
                raise SessionConflict(
                    "voice transcript is not awaiting a transcript consumer"
                )
            for row in eligible:
                connection.execute(
                    "UPDATE voice_transcripts SET safe_voice_state='pending_confirmation' "
                    "WHERE transcript_id=?",
                    (str(row["transcript_id"]),),
                )
                self._append_event(
                    connection,
                    session_id=str(row["session_id"]),
                    run_id=str(row["run_id"]),
                    kind="voice.input.transcript_pending_confirmation",
                    status="pending_confirmation",
                    phase="safe_voice",
                    summary="Safe Voice confirmation required",
                    detail={
                        "transcript_id": str(row["transcript_id"]),
                        "attachment_id": str(row["attachment_id"]),
                        "text": str(row["text"]),
                        "provenance": str(row["provenance"]),
                        "safe_voice_state": "pending_confirmation",
                    },
                    outbox=True,
                )
            updated = connection.execute(
                "SELECT * FROM voice_transcripts WHERE transcript_id=?",
                (str(eligible[-1]["transcript_id"]),),
            ).fetchone()
        return dict(updated)

    def release_ready_voice_transcript(
        self,
        *,
        request_id: str,
        reason: str = "native_audio_direct_completed",
    ) -> dict[str, Any]:
        """Release STT after native chat completes without another consumer."""

        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """SELECT vt.*, r.user_message_id FROM voice_transcripts AS vt
                   JOIN runs AS r ON r.run_id=vt.run_id
                   WHERE r.request_id=?
                   ORDER BY vt.created_at ASC, vt.transcript_id ASC""",
                (str(request_id),),
            ).fetchall()
            if not rows:
                raise SessionNotFound("voice transcript not found")
            eligible = [
                row for row in rows if str(row["safe_voice_state"]) == "ready"
            ]
            if not eligible:
                released = [
                    row
                    for row in rows
                    if str(row["safe_voice_state"]) == "released"
                ]
                if released:
                    return dict(released[-1])
                raise SessionConflict(
                    "voice transcript cannot be auto-released after Safe Voice started"
                )
            for row in eligible:
                connection.execute(
                    "UPDATE voice_transcripts SET safe_voice_state='released' "
                    "WHERE transcript_id=?",
                    (str(row["transcript_id"]),),
                )
                self._append_voice_transcript_projection(
                    connection,
                    message_id=str(row["user_message_id"]),
                    transcript=str(row["text"]),
                )
                self._append_event(
                    connection,
                    session_id=str(row["session_id"]),
                    run_id=str(row["run_id"]),
                    kind="voice.input.transcript_released",
                    status="released",
                    phase="transcription",
                    summary="Local input transcript released after native audio chat",
                    detail={
                        "transcript_id": str(row["transcript_id"]),
                        "attachment_id": str(row["attachment_id"]),
                        "release_reason": str(reason),
                    },
                    outbox=True,
                )
            updated = connection.execute(
                "SELECT * FROM voice_transcripts WHERE transcript_id=?",
                (str(eligible[-1]["transcript_id"]),),
            ).fetchone()
        return dict(updated)

    def reconcile_completed_native_audio_transcript(
        self, *, request_id: str
    ) -> dict[str, Any]:
        """Repair the pre-gate beta state for a completed native audio reply.

        Early native-audio builds opened Safe Voice as soon as STT completed,
        even when a no-tool Direct response had already answered from original
        audio.  This narrowly releases only that impossible-to-resume state: a
        completed Run with a durable assistant audio part.
        """

        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """SELECT vt.*, r.user_message_id, r.state AS run_state
                   FROM voice_transcripts AS vt
                   JOIN runs AS r ON r.run_id=vt.run_id
                   WHERE r.request_id=?
                   ORDER BY vt.created_at ASC, vt.transcript_id ASC""",
                (str(request_id),),
            ).fetchall()
            if not rows:
                raise SessionNotFound("voice transcript not found")
            if any(str(row["run_state"]) != "completed" for row in rows):
                raise SessionConflict(
                    "only a completed native audio Run can be reconciled"
                )
            assistant_rows = connection.execute(
                """SELECT content_json FROM messages
                   WHERE run_id=? AND role='assistant'""",
                (str(rows[0]["run_id"]),),
            ).fetchall()
            has_native_audio = False
            for assistant in assistant_rows:
                try:
                    content = json.loads(str(assistant["content_json"] or "[]"))
                except (TypeError, ValueError):
                    content = []
                if isinstance(content, list) and any(
                    isinstance(part, Mapping)
                    and str(part.get("type") or "") == "audio"
                    and bool(str(part.get("asset_id") or "").strip())
                    for part in content
                ):
                    has_native_audio = True
                    break
            if not has_native_audio:
                raise SessionConflict(
                    "completed Run has no durable native audio response"
                )
            eligible = [
                row
                for row in rows
                if str(row["safe_voice_state"]) == "pending_confirmation"
            ]
            if not eligible:
                released = [
                    row
                    for row in rows
                    if str(row["safe_voice_state"]) == "released"
                ]
                if released:
                    return dict(released[-1])
                raise SessionConflict(
                    "completed native audio transcript is not reconcilable"
                )
            for row in eligible:
                connection.execute(
                    "UPDATE voice_transcripts SET safe_voice_state='released' "
                    "WHERE transcript_id=?",
                    (str(row["transcript_id"]),),
                )
                self._append_voice_transcript_projection(
                    connection,
                    message_id=str(row["user_message_id"]),
                    transcript=str(row["text"]),
                )
                self._append_event(
                    connection,
                    session_id=str(row["session_id"]),
                    run_id=str(row["run_id"]),
                    kind="voice.input.transcript_released",
                    status="released",
                    phase="migration",
                    summary=(
                        "Deferred input transcript released after native audio "
                        "Direct migration"
                    ),
                    detail={
                        "transcript_id": str(row["transcript_id"]),
                        "attachment_id": str(row["attachment_id"]),
                        "release_reason": "pre_gate_native_direct_reconciliation",
                    },
                    outbox=True,
                )
            updated = connection.execute(
                "SELECT * FROM voice_transcripts WHERE transcript_id=?",
                (str(eligible[-1]["transcript_id"]),),
            ).fetchone()
        return dict(updated)

    @staticmethod
    def _append_voice_transcript_projection(
        connection: sqlite3.Connection,
        *,
        message_id: str,
        transcript: str,
    ) -> None:
        """Add accepted speech to the user text projection exactly once."""

        clean = str(transcript or "").strip()
        if not clean:
            return
        row = connection.execute(
            "SELECT text FROM messages WHERE message_id=?", (str(message_id),)
        ).fetchone()
        if row is None:
            raise SessionNotFound("voice transcript Message not found")
        current = str(row["text"] or "").strip()
        segments = [segment.strip() for segment in current.split("\n\n")]
        if clean in segments:
            return
        projected = f"{current}\n\n{clean}" if current else clean
        connection.execute(
            "UPDATE messages SET text=? WHERE message_id=?",
            (projected, str(message_id)),
        )

    def append_native_audio_runtime_event(
        self,
        *,
        request_id: str,
        source_event_id: str,
        event_kind: str,
        summary: str,
        phase: str,
        content: Iterable[Mapping[str, Any]] = (),
        resolution: str = "",
        target_event_id: str = "",
    ) -> dict[str, Any] | None:
        stable_source_id = str(source_event_id or "").strip()
        if not stable_source_id:
            return None
        parts = [dict(part) for part in content if isinstance(part, Mapping)]
        if contains_persistent_inline_media(parts):
            raise SessionConflict("runtime audio Event cannot contain inline bytes")
        has_audio = any(
            part.get("type") == "audio" and str(part.get("asset_id") or "")
            for part in parts
        )
        now = _utc_now()
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """SELECT e.* FROM runtime_event_correlations AS c
                   JOIN run_events AS e ON e.event_id=c.event_id
                   WHERE c.source_event_id=?""",
                (stable_source_id,),
            ).fetchone()
            if existing is not None:
                result = dict(existing)
                result["detail"] = _json_object(result.pop("detail_json"))
                return result
            run = connection.execute(
                """SELECT r.*, s.owner_id FROM runs AS r
                   JOIN sessions AS s ON s.session_id=r.session_id
                   WHERE r.request_id=?""",
                (str(request_id),),
            ).fetchone()
            if run is None:
                return None
            canonical_kind = ""
            detail: dict[str, Any] = {}
            if has_audio:
                canonical_kind = "assistant.output.available"
                for part in parts:
                    if part.get("type") != "audio":
                        continue
                    self.audio_assets.claim(
                        str(part["asset_id"]),
                        owner_id=str(run["owner_id"]),
                        session_id=str(run["session_id"]),
                        request_id=str(request_id),
                    )
                content_json = _json(parts)
                content_hash = hashlib.sha256(
                    content_json.encode("utf-8")
                ).hexdigest()
                existing_message = connection.execute(
                    """SELECT message_id FROM messages
                       WHERE run_id=? AND role='assistant' AND content_hash=?
                       ORDER BY ordinal ASC LIMIT 1""",
                    (str(run["run_id"]), content_hash),
                ).fetchone()
                if existing_message is None:
                    message_id = _new_id("msg")
                    text_projection = "\n".join(
                        str(part.get("text") or "").strip()
                        for part in parts
                        if part.get("type") == "text"
                        and str(part.get("text") or "").strip()
                    ).strip()
                    connection.execute(
                        """
                        INSERT INTO messages(
                            message_id, session_id, run_id, ordinal,
                            context_generation, role, author_id, source,
                            content_json, text, content_hash, created_at
                        ) VALUES (?, ?, ?, ?, ?, 'assistant', ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            message_id,
                            str(run["session_id"]),
                            str(run["run_id"]),
                            self._next_ordinal(connection, str(run["session_id"])),
                            int(run["context_generation"]),
                            str(run["agent_id"]),
                            str(run["agent_id"]),
                            content_json,
                            text_projection,
                            content_hash,
                            now,
                        ),
                    )
                else:
                    message_id = str(existing_message["message_id"])
                detail = {
                    "message_id": message_id,
                    "request_id": str(request_id),
                    "phase": str(phase or "immediate"),
                    "disposition": "unresolved",
                    "content": parts,
                }
            elif str(event_kind) == "voice_fallback_started":
                canonical_kind = "voice.fallback.started"
                detail = {"request_id": str(request_id), "phase": str(phase)}
            elif str(event_kind) == "voice_warning":
                canonical_kind = "voice.warning"
                detail = {"request_id": str(request_id), "warning": str(summary)}
            elif str(event_kind) == "initial_resolution" and target_event_id:
                target = connection.execute(
                    """SELECT kind FROM runtime_event_correlations
                       WHERE source_event_id=?""",
                    (str(target_event_id),),
                ).fetchone()
                if target is None or str(target["kind"]) != "assistant.output.available":
                    return None
                canonical_kind = "assistant.output.resolved"
                detail = {
                    "request_id": str(request_id),
                    "target_event_id": str(target_event_id),
                    "resolution": str(resolution or "acknowledgement"),
                }
            else:
                return None
            event = self._append_event(
                connection,
                session_id=str(run["session_id"]),
                run_id=str(run["run_id"]),
                kind=canonical_kind,
                status=(
                    str(resolution or "resolved")
                    if canonical_kind == "assistant.output.resolved"
                    else "available"
                ),
                phase=str(phase or "immediate"),
                summary=str(summary or canonical_kind),
                detail=detail,
                outbox=True,
            )
            connection.execute(
                """INSERT INTO runtime_event_correlations(
                       source_event_id,session_id,run_id,event_id,kind,created_at
                   ) VALUES(?,?,?,?,?,?)""",
                (
                    stable_source_id,
                    str(run["session_id"]),
                    str(run["run_id"]),
                    event["event_id"],
                    canonical_kind,
                    now,
                ),
            )
        return event

    def decide_voice_transcript(
        self, *, request_id: str, confirmed: bool
    ) -> dict[str, Any]:
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """SELECT vt.*, r.user_message_id FROM voice_transcripts AS vt
                   JOIN runs AS r ON r.run_id=vt.run_id
                   WHERE r.request_id=?
                   ORDER BY vt.created_at ASC, vt.transcript_id ASC""",
                (str(request_id),),
            ).fetchall()
            if not rows:
                raise SessionNotFound("voice transcript not found")
            state = "released" if confirmed else "discarded"
            eligible = [
                row
                for row in rows
                if str(row["safe_voice_state"]) == "pending_confirmation"
            ]
            if not eligible:
                matching = [
                    row for row in rows if str(row["safe_voice_state"]) == state
                ]
                if matching:
                    return dict(matching[-1])
                raise SessionConflict(
                    "voice transcript is not waiting for Safe Voice confirmation"
                )
            for row in eligible:
                connection.execute(
                    "UPDATE voice_transcripts SET safe_voice_state=? WHERE transcript_id=?",
                    (state, str(row["transcript_id"])),
                )
                if confirmed:
                    self._append_voice_transcript_projection(
                        connection,
                        message_id=str(row["user_message_id"]),
                        transcript=str(row["text"]),
                    )
                self._append_event(
                    connection,
                    session_id=str(row["session_id"]),
                    run_id=str(row["run_id"]),
                    kind=(
                        "voice.input.transcript_confirmed"
                        if confirmed
                        else "voice.input.transcript_discarded"
                    ),
                    status=state,
                    phase="safe_voice",
                    summary=(
                        "Safe Voice transcript confirmed"
                        if confirmed
                        else "Safe Voice transcript discarded"
                    ),
                    detail={"transcript_id": str(row["transcript_id"])},
                    outbox=True,
                )
            updated = connection.execute(
                "SELECT * FROM voice_transcripts WHERE transcript_id=?",
                (str(eligible[-1]["transcript_id"]),),
            ).fetchone()
        return dict(updated)

    def decide_voice_transcript_by_id(
        self,
        *,
        session_id: str,
        owner_id: str,
        transcript_id: str,
        confirmed: bool,
    ) -> dict[str, Any]:
        """Apply an authenticated generic-client Safe Voice decision."""

        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """SELECT vt.*, r.user_message_id, r.request_id
                   FROM voice_transcripts AS vt
                   JOIN runs AS r ON r.run_id=vt.run_id
                   JOIN sessions AS s ON s.session_id=vt.session_id
                   WHERE vt.transcript_id=? AND vt.session_id=? AND s.owner_id=?""",
                (str(transcript_id), str(session_id), str(owner_id)),
            ).fetchone()
            if row is None:
                raise SessionNotFound("voice transcript not found")
            current = str(row["safe_voice_state"])
            desired = "released" if confirmed else "discarded"
            if current not in {"pending_confirmation", desired}:
                raise SessionConflict(
                    f"voice transcript cannot change from {current} to {desired}"
                )
            if current == "pending_confirmation":
                connection.execute(
                    "UPDATE voice_transcripts SET safe_voice_state=? "
                    "WHERE transcript_id=?",
                    (desired, str(transcript_id)),
                )
                if confirmed:
                    self._append_voice_transcript_projection(
                        connection,
                        message_id=str(row["user_message_id"]),
                        transcript=str(row["text"]),
                    )
                self._append_event(
                    connection,
                    session_id=str(row["session_id"]),
                    run_id=str(row["run_id"]),
                    kind=(
                        "voice.input.transcript_confirmed"
                        if confirmed
                        else "voice.input.transcript_discarded"
                    ),
                    status=desired,
                    phase="safe_voice",
                    summary=(
                        "Safe Voice transcript confirmed"
                        if confirmed
                        else "Safe Voice transcript discarded"
                    ),
                    detail={"transcript_id": str(transcript_id)},
                    outbox=True,
                )
            updated = connection.execute(
                """SELECT vt.*, r.request_id FROM voice_transcripts AS vt
                   JOIN runs AS r ON r.run_id=vt.run_id
                   WHERE vt.transcript_id=?""",
                (str(transcript_id),),
            ).fetchone()
        return dict(updated)

    def create_approval(
        self,
        *,
        run_id: str,
        owner_id: str,
        fencing_token: int,
        scope: Mapping[str, Any],
    ) -> dict[str, Any]:
        run = self.get_run(run_id, owner_id=owner_id)
        if int(run["fencing_token"]) != int(fencing_token) or run["state"] != "running":
            raise StaleFencingToken("approval origin is no longer authoritative")
        approval_id, now = _new_id("approval"), _utc_now()
        with self._lock, self._connect() as connection:
            connection.execute(
                """INSERT INTO run_approvals(approval_id,session_id,run_id,owner_id,attempt,
                   fencing_token,scope_json,created_at) VALUES(?,?,?,?,?,?,?,?)""",
                (
                    approval_id,
                    run["session_id"],
                    str(run_id),
                    str(owner_id),
                    int(run["attempt"]),
                    int(fencing_token),
                    _json(dict(scope)),
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM run_approvals WHERE approval_id=?", (approval_id,)
            ).fetchone()
        result = dict(row)
        result["scope"] = _json_object(result.pop("scope_json"))
        return result

    def decide_approval(
        self, *, approval_id: str, owner_id: str, decision: str
    ) -> dict[str, Any]:
        resolved = str(decision).lower()
        if resolved not in {"approved", "denied"}:
            raise ValueError("decision must be approved or denied")
        now = _utc_now()
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM run_approvals WHERE approval_id=? AND owner_id=?",
                (str(approval_id), str(owner_id)),
            ).fetchone()
            if row is None:
                raise SessionNotFound("approval not found")
            run = connection.execute(
                "SELECT * FROM runs WHERE run_id=?", (row["run_id"],)
            ).fetchone()
            if str(row["state"]) == "pending" and (
                run is None
                or run["state"] != "running"
                or int(run["fencing_token"]) != int(row["fencing_token"])
            ):
                raise StaleFencingToken("approval expired with its originating attempt")
            if str(row["state"]) == "pending":
                connection.execute(
                    "UPDATE run_approvals SET state='decided',decision=?,decided_at=? WHERE approval_id=?",
                    (resolved, now, str(approval_id)),
                )
                self._append_event(
                    connection,
                    session_id=str(row["session_id"]),
                    run_id=str(row["run_id"]),
                    kind="approval.decided",
                    status=resolved,
                    phase="control",
                    summary=f"Approval {resolved}",
                    detail={"approval_id": str(approval_id), "decision": resolved},
                    outbox=True,
                )
            updated = connection.execute(
                "SELECT * FROM run_approvals WHERE approval_id=?", (str(approval_id),)
            ).fetchone()
        result = dict(updated)
        result["scope"] = _json_object(result.pop("scope_json"))
        return result

    def reconcile_incomplete_runs(self) -> list[dict[str, Any]]:
        """Terminalize Runs whose in-memory executor was lost on restart.

        The current runtime has no durable queue or safe execution-stack replay.
        Leaving either an accepted ``queued`` Run or a claimed ``running`` Run
        non-terminal would make clients wait forever.  Reconciliation therefore
        fences every pre-existing non-terminal Run as ``interrupted``, preserves
        its user Message and evidence, and appends one durable terminal Event.
        A later user continuation is a new child Run with a new idempotency key.
        """

        now = _utc_now()
        reconciled_ids: list[str] = []
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT r.* FROM runs AS r
                JOIN sessions AS s ON s.session_id = r.session_id
                WHERE s.instance_id = ? AND r.state IN ('queued', 'running')
                ORDER BY r.created_at, r.run_id
                """,
                (self.instance_id,),
            ).fetchall()
            for run in rows:
                prior_state = str(run["state"])
                run_id = str(run["run_id"])
                session_id = str(run["session_id"])
                reason = (
                    "HASHI restarted before the accepted Run began"
                    if prior_state == "queued"
                    else "HASHI restarted while the Run was executing"
                )
                updated = connection.execute(
                    """
                    UPDATE runs
                    SET state = 'interrupted', fencing_token = fencing_token + 1,
                        worker_id = NULL, error_code = 'runtime_restart_interrupted',
                        error_text = ?, completed_at = ?, updated_at = ?
                    WHERE run_id = ? AND state = ?
                    """,
                    (reason, now, now, run_id, prior_state),
                )
                if updated.rowcount != 1:
                    continue
                connection.execute(
                    """
                    UPDATE run_attempts
                    SET state = 'interrupted', finished_at = ?
                    WHERE run_id = ? AND state = 'running'
                    """,
                    (now, run_id),
                )
                self._release_run_audio_leases(
                    connection, run_id=run_id, released_at=now
                )
                event = self._append_event(
                    connection,
                    session_id=session_id,
                    run_id=run_id,
                    kind="run.interrupted",
                    status="interrupted",
                    phase="recovery",
                    summary=reason,
                    detail={
                        "error_code": "runtime_restart_interrupted",
                        "prior_state": prior_state,
                    },
                    outbox=True,
                )
                projection = {
                    "run_id": run_id,
                    "session_id": session_id,
                    "state": "interrupted",
                    "user_message_id": str(run["user_message_id"]),
                    "final_message_id": None,
                    "error": reason,
                    "error_code": "runtime_restart_interrupted",
                    "prior_state": prior_state,
                    "latest_event_sequence": event["sequence"],
                }
                connection.execute(
                    """
                    INSERT INTO run_projection_records(
                        run_id, session_id, projection_json, updated_at
                    ) VALUES (?, ?, ?, ?)
                    ON CONFLICT(run_id) DO UPDATE SET
                        projection_json = excluded.projection_json,
                        updated_at = excluded.updated_at
                    """,
                    (run_id, session_id, _json(projection), now),
                )
                connection.execute(
                    """
                    UPDATE sessions
                    SET updated_at = ?, revision = revision + 1
                    WHERE session_id = ?
                    """,
                    (now, session_id),
                )
                reconciled_ids.append(run_id)

            if not reconciled_ids:
                return []
            placeholders = ",".join("?" for _ in reconciled_ids)
            result = connection.execute(
                f"SELECT * FROM runs WHERE run_id IN ({placeholders}) "
                "ORDER BY created_at, run_id",
                reconciled_ids,
            ).fetchall()
        return [self._run_dict(row) for row in result]

    def get_run(self, run_id: str, *, owner_id: str | None = None) -> dict[str, Any]:
        clauses = ["r.run_id = ?", "s.instance_id = ?"]
        params: list[Any] = [str(run_id), self.instance_id]
        if owner_id is not None:
            clauses.append("s.owner_id = ?")
            params.append(str(owner_id))
        with self._lock, self._connect() as connection:
            row = connection.execute(
                f"""
                SELECT r.* FROM runs AS r JOIN sessions AS s ON s.session_id = r.session_id
                WHERE {" AND ".join(clauses)}
                """,
                params,
            ).fetchone()
        if row is None:
            raise SessionNotFound(str(run_id))
        return self._run_dict(row)

    def get_run_by_request(
        self,
        request_id: str,
        *,
        owner_id: str | None = None,
        agent_id: str | None = None,
    ) -> dict[str, Any]:
        clauses = ["r.request_id = ?", "s.instance_id = ?"]
        params: list[Any] = [str(request_id), self.instance_id]
        if owner_id is not None:
            clauses.append("s.owner_id = ?")
            params.append(str(owner_id))
        if agent_id is not None:
            clauses.append("r.agent_id = ?")
            params.append(str(agent_id).lower())
        with self._lock, self._connect() as connection:
            row = connection.execute(
                f"""
                SELECT r.* FROM runs AS r
                JOIN sessions AS s ON s.session_id = r.session_id
                WHERE {" AND ".join(clauses)}
                """,
                params,
            ).fetchone()
        if row is None:
            raise SessionNotFound(str(request_id))
        return self._run_dict(row)

    def update_session(
        self,
        session_id: str,
        *,
        owner_id: str,
        expected_revision: int,
        title: str | None = None,
    ) -> dict[str, Any]:
        clean_title = str(title or "").strip()
        if title is not None and not clean_title:
            raise ValueError("title cannot be empty")
        now = _utc_now()
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT * FROM sessions
                WHERE session_id = ? AND instance_id = ? AND owner_id = ?
                """,
                (str(session_id), self.instance_id, str(owner_id)),
            ).fetchone()
            if row is None:
                raise SessionNotFound(str(session_id))
            if int(row["revision"]) != int(expected_revision):
                raise SessionConflict("Session revision does not match")
            if title is not None:
                connection.execute(
                    """
                    UPDATE sessions SET title = ?, title_source = 'user',
                        revision = revision + 1, updated_at = ? WHERE session_id = ?
                    """,
                    (clean_title, now, str(session_id)),
                )
            updated = connection.execute(
                "SELECT * FROM sessions WHERE session_id = ?", (str(session_id),)
            ).fetchone()
            return self._session_dict(updated)

    def messages(
        self,
        session_id: str,
        *,
        owner_id: str | None = None,
        after_ordinal: int = 0,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        self.get_session(session_id, owner_id=owner_id)
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM messages WHERE session_id = ? AND ordinal > ?
                ORDER BY ordinal ASC LIMIT ?
                """,
                (
                    str(session_id),
                    max(0, int(after_ordinal)),
                    max(1, min(int(limit), 1000)),
                ),
            ).fetchall()
        return [self._message_dict(row) for row in rows]

    def recent_exchanges(
        self,
        session_id: str,
        *,
        context_generation: int | None = None,
        limit: int = 8,
    ) -> list[dict[str, Any]]:
        session = self.get_session(session_id)
        generation = int(context_generation or session["context_generation"])
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT r.run_id, u.ordinal AS sequence, u.message_id AS user_message_id,
                       a.message_id AS assistant_message_id,
                       u.created_at AS user_ts, a.created_at AS assistant_ts,
                       u.source AS user_source, a.source AS assistant_source,
                       u.text AS user_text, a.text AS assistant_text,
                       (SELECT GROUP_CONCAT(DISTINCT vt.provenance)
                          FROM voice_transcripts AS vt
                         WHERE vt.message_id=u.message_id
                           AND vt.safe_voice_state='released')
                           AS user_transcript_provenance,
                       CASE WHEN a.content_json LIKE '%provider_audio_transcript%'
                            THEN 'provider_audio_transcript' ELSE '' END
                           AS assistant_transcript_provenance
                FROM runs AS r
                JOIN messages AS u ON u.message_id = r.user_message_id
                JOIN messages AS a ON a.message_id = r.final_message_id
                WHERE r.session_id = ? AND r.context_generation = ?
                  AND r.state = 'completed'
                  AND u.visibility = 'visible' AND a.visibility = 'visible'
                  AND u.history_eligible = 1 AND a.history_eligible = 1
                ORDER BY u.ordinal DESC LIMIT ?
                """,
                (str(session_id), generation, max(1, min(int(limit), 100))),
            ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def recent_agent_exchanges(
        self,
        *,
        owner_id: str,
        agent_id: str,
        limit: int = 10,
        excluded_sources: Iterable[str] = (),
    ) -> list[dict[str, Any]]:
        """Return recent completed Bridge exchanges across Session boundaries.

        Session-local history remains the authority for ordinary turn context.
        Explicit continuity operations such as ``/handoff`` instead need the
        owner's recent Agent timeline regardless of which Session is currently
        selected. Deleted Sessions are intentionally excluded; archived
        Sessions remain eligible because archival retains their history.
        """

        normalized_sources = sorted(
            {
                str(source).strip().lower()
                for source in excluded_sources
                if str(source).strip()
            }
        )
        source_clause = ""
        source_params: list[Any] = []
        if normalized_sources:
            placeholders = ",".join("?" for _ in normalized_sources)
            source_clause = f" AND LOWER(u.source) NOT IN ({placeholders})"
            source_params.extend(normalized_sources)

        params: list[Any] = [
            self.instance_id,
            str(owner_id),
            str(agent_id).strip().lower(),
            *source_params,
            max(1, min(int(limit), 100)),
        ]
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT r.run_id, r.session_id, r.context_generation,
                       u.ordinal AS session_sequence,
                       u.message_id AS user_message_id,
                       a.message_id AS assistant_message_id,
                       u.created_at AS user_ts, a.created_at AS assistant_ts,
                       u.source AS user_source, a.source AS assistant_source,
                       u.text AS user_text, a.text AS assistant_text,
                       (SELECT GROUP_CONCAT(DISTINCT vt.provenance)
                          FROM voice_transcripts AS vt
                         WHERE vt.message_id=u.message_id
                           AND vt.safe_voice_state='released')
                           AS user_transcript_provenance,
                       CASE WHEN a.content_json LIKE '%provider_audio_transcript%'
                            THEN 'provider_audio_transcript' ELSE '' END
                           AS assistant_transcript_provenance
                FROM runs AS r
                JOIN sessions AS s ON s.session_id = r.session_id
                JOIN messages AS u ON u.message_id = r.user_message_id
                JOIN messages AS a ON a.message_id = r.final_message_id
                WHERE s.instance_id = ? AND s.owner_id = ? AND s.agent_id = ?
                  AND s.status != 'deleted'
                  AND r.state = 'completed'
                  AND u.visibility = 'visible' AND a.visibility = 'visible'
                  AND u.history_eligible = 1 AND a.history_eligible = 1
                  {source_clause}
                ORDER BY u.created_at DESC, a.created_at DESC, r.run_id DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def last_user_message_at(
        self,
        *,
        agent_id: str,
        owner_id: str | None = None,
    ) -> str | None:
        clauses = [
            "s.instance_id = ?",
            "s.agent_id = ?",
            "m.role = 'user'",
        ]
        params: list[Any] = [self.instance_id, str(agent_id)]
        if owner_id is not None:
            clauses.append("s.owner_id = ?")
            params.append(str(owner_id))
        with self._lock, self._connect() as connection:
            row = connection.execute(
                f"""
                SELECT MAX(m.created_at) AS created_at
                FROM messages AS m
                JOIN sessions AS s ON s.session_id = m.session_id
                WHERE {" AND ".join(clauses)}
                """,
                params,
            ).fetchone()
        value = row["created_at"] if row is not None else None
        return str(value) if value else None

    def start_fresh_generation(
        self, session_id: str, *, reason: str = "fresh"
    ) -> dict[str, Any]:
        now = _utc_now()
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM sessions WHERE session_id = ? AND status = 'active'",
                (str(session_id),),
            ).fetchone()
            if row is None:
                raise SessionNotFound(str(session_id))
            active_run = connection.execute(
                """
                SELECT run_id FROM runs
                WHERE session_id = ? AND state NOT IN (
                    'completed', 'failed', 'stopped', 'superseded', 'interrupted'
                ) LIMIT 1
                """,
                (str(session_id),),
            ).fetchone()
            if active_run is not None:
                raise SessionConflict(
                    "fresh context is blocked while the Session has an active Run"
                )
            generation = int(row["context_generation"]) + 1
            connection.execute(
                """
                UPDATE sessions SET context_generation = ?, revision = revision + 1,
                    updated_at = ? WHERE session_id = ?
                """,
                (generation, now, str(session_id)),
            )
            connection.execute(
                """
                INSERT INTO session_context_generations(session_id, generation, reason, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (str(session_id), generation, str(reason), now),
            )
            self._append_event(
                connection,
                session_id=str(session_id),
                run_id=None,
                kind="session.context_generation_started",
                status="active",
                phase="control",
                summary="Fresh context generation started",
                detail={"generation": generation, "reason": str(reason)},
                outbox=True,
            )
            updated = connection.execute(
                "SELECT * FROM sessions WHERE session_id = ?", (str(session_id),)
            ).fetchone()
            return self._session_dict(updated)

    def set_workzone(self, session_id: str, workzone: str | None) -> dict[str, Any]:
        now = _utc_now()
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            updated = connection.execute(
                """
                UPDATE sessions SET workzone = ?, revision = revision + 1, updated_at = ?
                WHERE session_id = ? AND status = 'active'
                """,
                (str(workzone) if workzone else None, now, str(session_id)),
            )
            if updated.rowcount != 1:
                raise SessionNotFound(str(session_id))
            self._append_event(
                connection,
                session_id=str(session_id),
                run_id=None,
                kind="session.workzone_changed",
                status="active",
                phase="control",
                summary="Session Workzone changed",
                detail={"workzone": str(workzone) if workzone else None},
            )
            row = connection.execute(
                "SELECT * FROM sessions WHERE session_id = ?", (str(session_id),)
            ).fetchone()
            return self._session_dict(row)

    def archive_session(
        self, session_id: str, *, deleted: bool = False
    ) -> dict[str, Any]:
        now = _utc_now()
        target = "deleted" if deleted else "archived"
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM sessions WHERE session_id = ?", (str(session_id),)
            ).fetchone()
            if row is None:
                raise SessionNotFound(str(session_id))
            if bool(row["is_default"]):
                raise SessionConflict(
                    "the permanent default Session cannot be archived"
                )
            connection.execute(
                """
                UPDATE sessions SET status = ?, deleted_at = ?, revision = revision + 1,
                    updated_at = ? WHERE session_id = ?
                """,
                (target, now if deleted else None, now, str(session_id)),
            )
            connection.execute(
                "DELETE FROM channel_bindings WHERE session_id = ?", (str(session_id),)
            )
            self._append_event(
                connection,
                session_id=str(session_id),
                run_id=None,
                kind=f"session.{target}",
                status=target,
                phase="control",
                summary=f"Session {target}",
                detail={"records_deleted": False},
                outbox=True,
            )
            updated = connection.execute(
                "SELECT * FROM sessions WHERE session_id = ?", (str(session_id),)
            ).fetchone()
            return self._session_dict(updated)

    def backend_binding(
        self,
        *,
        agent_id: str,
        session_id: str,
        context_generation: int,
        backend_id: str,
    ) -> str | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT backend_thread_id FROM backend_bindings
                WHERE agent_id = ? AND session_id = ? AND context_generation = ?
                  AND backend_id = ?
                """,
                (
                    str(agent_id).lower(),
                    str(session_id),
                    int(context_generation),
                    str(backend_id),
                ),
            ).fetchone()
        return str(row["backend_thread_id"]) if row is not None else None

    def save_backend_binding(
        self,
        *,
        agent_id: str,
        session_id: str,
        context_generation: int,
        backend_id: str,
        backend_thread_id: str | None,
    ) -> None:
        with self._lock, self._connect() as connection:
            if not backend_thread_id:
                connection.execute(
                    """
                    DELETE FROM backend_bindings WHERE agent_id = ? AND session_id = ?
                      AND context_generation = ? AND backend_id = ?
                    """,
                    (
                        str(agent_id).lower(),
                        str(session_id),
                        int(context_generation),
                        str(backend_id),
                    ),
                )
                return
            connection.execute(
                """
                INSERT INTO backend_bindings(
                    agent_id, session_id, context_generation, backend_id,
                    backend_thread_id, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(agent_id, session_id, context_generation, backend_id)
                DO UPDATE SET backend_thread_id = excluded.backend_thread_id,
                              updated_at = excluded.updated_at
                """,
                (
                    str(agent_id).lower(),
                    str(session_id),
                    int(context_generation),
                    str(backend_id),
                    str(backend_thread_id),
                    _utc_now(),
                ),
            )

    def events(
        self,
        session_id: str,
        *,
        owner_id: str | None = None,
        after_sequence: int = 0,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        self.get_session(session_id, owner_id=owner_id)
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM run_events WHERE session_id = ? AND sequence > ?
                ORDER BY sequence ASC LIMIT ?
                """,
                (
                    str(session_id),
                    max(0, int(after_sequence)),
                    max(1, min(int(limit), 2000)),
                ),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["detail"] = _json_object(item.pop("detail_json", "{}"))
            result.append(item)
        return result

    def create_event_consumer(
        self,
        *,
        session_id: str,
        owner_id: str,
        consumer_id: str | None = None,
    ) -> dict[str, Any]:
        self.get_session(session_id, owner_id=owner_id)
        resolved_id = str(consumer_id or _new_id("consumer"))
        now = _utc_now()
        with self._lock, self._connect() as connection:
            existing = connection.execute(
                "SELECT * FROM event_consumers WHERE consumer_id = ?",
                (resolved_id,),
            ).fetchone()
            if existing is not None:
                if str(existing["session_id"]) != str(session_id) or str(
                    existing["owner_id"]
                ) != str(owner_id):
                    raise SessionConflict("event consumer belongs to another Session")
                return dict(existing)
            connection.execute(
                """
                INSERT INTO event_consumers(
                    consumer_id, session_id, owner_id,
                    acknowledged_sequence, issued_through_sequence, updated_at
                ) VALUES (?, ?, ?, 0, 0, ?)
                """,
                (resolved_id, str(session_id), str(owner_id), now),
            )
            row = connection.execute(
                "SELECT * FROM event_consumers WHERE consumer_id = ?",
                (resolved_id,),
            ).fetchone()
        return dict(row)

    def poll_event_consumer(
        self,
        *,
        session_id: str,
        owner_id: str,
        consumer_id: str,
        limit: int = 500,
    ) -> dict[str, Any]:
        self.get_session(session_id, owner_id=owner_id)
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            consumer = connection.execute(
                """
                SELECT * FROM event_consumers
                WHERE consumer_id = ? AND session_id = ? AND owner_id = ?
                """,
                (str(consumer_id), str(session_id), str(owner_id)),
            ).fetchone()
            if consumer is None:
                raise SessionNotFound("event consumer not found")
            acknowledged = int(consumer["acknowledged_sequence"])
            rows = connection.execute(
                """
                SELECT * FROM run_events
                WHERE session_id = ? AND sequence > ?
                ORDER BY sequence ASC LIMIT ?
                """,
                (
                    str(session_id),
                    acknowledged,
                    max(1, min(int(limit), 2000)),
                ),
            ).fetchall()
            issued = max(
                int(consumer["issued_through_sequence"]),
                int(rows[-1]["sequence"]) if rows else acknowledged,
            )
            connection.execute(
                """
                UPDATE event_consumers
                SET issued_through_sequence = ?, updated_at = ?
                WHERE consumer_id = ?
                """,
                (issued, _utc_now(), str(consumer_id)),
            )
            bounds = connection.execute(
                """
                SELECT COALESCE(MIN(sequence), 0) AS earliest,
                       COALESCE(MAX(sequence), 0) AS latest
                FROM run_events WHERE session_id = ?
                """,
                (str(session_id),),
            ).fetchone()
        events = []
        for row in rows:
            item = dict(row)
            item["detail"] = _json_object(item.pop("detail_json", "{}"))
            events.append(item)
        return {
            "consumer_id": str(consumer_id),
            "acknowledged_sequence": acknowledged,
            "issued_through_sequence": issued,
            "earliest_available_sequence": int(bounds["earliest"]),
            "latest_sequence": int(bounds["latest"]),
            "events": events,
        }

    def acknowledge_event_consumer(
        self,
        *,
        session_id: str,
        owner_id: str,
        consumer_id: str,
        sequence: int,
    ) -> dict[str, Any]:
        requested = max(0, int(sequence))
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT * FROM event_consumers
                WHERE consumer_id = ? AND session_id = ? AND owner_id = ?
                """,
                (str(consumer_id), str(session_id), str(owner_id)),
            ).fetchone()
            if row is None:
                raise SessionNotFound("event consumer not found")
            acknowledged = int(row["acknowledged_sequence"])
            issued = int(row["issued_through_sequence"])
            if requested > issued:
                raise SessionConflict(
                    "ACK cannot advance beyond events issued to this consumer"
                )
            resolved = max(acknowledged, requested)
            connection.execute(
                """
                UPDATE event_consumers
                SET acknowledged_sequence = ?, updated_at = ?
                WHERE consumer_id = ?
                """,
                (resolved, _utc_now(), str(consumer_id)),
            )
            updated = connection.execute(
                "SELECT * FROM event_consumers WHERE consumer_id = ?",
                (str(consumer_id),),
            ).fetchone()
        return dict(updated)

    def snapshot(
        self, session_id: str, *, owner_id: str | None = None
    ) -> dict[str, Any]:
        session = self.get_session(session_id, owner_id=owner_id)
        with self._lock, self._connect() as connection:
            projections = connection.execute(
                """
                SELECT projection_json FROM run_projection_records
                WHERE session_id = ? ORDER BY updated_at, run_id
                """,
                (str(session_id),),
            ).fetchall()
            bounds = connection.execute(
                """
                SELECT COALESCE(MIN(sequence), 0) AS earliest,
                       COALESCE(MAX(sequence), 0) AS latest
                FROM run_events WHERE session_id = ?
                """,
                (str(session_id),),
            ).fetchone()
        return {
            "session": session,
            "messages": self.messages(session_id, owner_id=owner_id, limit=1000),
            "runs": [_json_object(row["projection_json"]) for row in projections],
            "earliest_available_sequence": int(
                bounds["earliest"] if bounds is not None else 0
            ),
            "latest_sequence": int(bounds["latest"] if bounds is not None else 0),
            "snapshot_revision": int(session["revision"]),
        }

    def promotion_candidates(
        self,
        *,
        agent_id: str,
        session_ids: Iterable[str] | None = None,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        clauses = ["r.agent_id = ?", "r.state = 'completed'", "p.run_id IS NULL"]
        params: list[Any] = [str(agent_id).lower()]
        selected = [str(item) for item in (session_ids or ()) if str(item)]
        if selected:
            clauses.append(f"r.session_id IN ({','.join('?' for _ in selected)})")
            params.extend(selected)
        params.append(max(1, min(int(limit), 5000)))
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT r.run_id, r.session_id, r.user_message_id,
                       r.final_message_id AS assistant_message_id,
                       u.ordinal AS user_ordinal, a.ordinal AS assistant_ordinal,
                       u.created_at AS user_ts, a.created_at AS assistant_ts,
                       u.source, u.text AS user_text, a.text AS assistant_text
                FROM runs AS r
                JOIN messages AS u ON u.message_id = r.user_message_id
                JOIN messages AS a ON a.message_id = r.final_message_id
                LEFT JOIN agent_memory_records AS p ON p.run_id = r.run_id
                WHERE {" AND ".join(clauses)}
                ORDER BY a.created_at, a.ordinal, r.run_id LIMIT ?
                """,
                params,
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["memory_origin_ref"] = (
                f"session:{item['session_id']}:run:{item['run_id']}"
            )
            result.append(item)
        return result

    def record_promoted(self, *, agent_id: str, candidate: Mapping[str, Any]) -> bool:
        now = _utc_now()
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            inserted = connection.execute(
                """
                INSERT OR IGNORE INTO agent_memory_records(
                    promotion_record_id, agent_id, session_id, run_id,
                    user_message_id, assistant_message_id, memory_origin_ref, promoted_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _new_id("pmr"),
                    str(agent_id).lower(),
                    str(candidate["session_id"]),
                    str(candidate["run_id"]),
                    str(candidate["user_message_id"]),
                    str(candidate["assistant_message_id"]),
                    str(candidate["memory_origin_ref"]),
                    now,
                ),
            )
            if inserted.rowcount != 1:
                return False
            ordinal = int(candidate.get("assistant_ordinal") or 0)
            connection.execute(
                """
                INSERT INTO memory_promotion_watermarks(
                    agent_id, session_id, promoted_through_ordinal, promoted_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(agent_id, session_id) DO UPDATE SET
                    promoted_through_ordinal = MAX(
                        promoted_through_ordinal, excluded.promoted_through_ordinal
                    ),
                    promoted_at = excluded.promoted_at
                """,
                (str(agent_id).lower(), str(candidate["session_id"]), ordinal, now),
            )
            return True

    def promotion_status(self, *, agent_id: str) -> dict[str, Any]:
        with self._lock, self._connect() as connection:
            schedule = connection.execute(
                "SELECT * FROM memory_promotion_schedules WHERE agent_id = ?",
                (str(agent_id).lower(),),
            ).fetchone()
            promoted = connection.execute(
                "SELECT COUNT(*) AS value FROM agent_memory_records WHERE agent_id = ?",
                (str(agent_id).lower(),),
            ).fetchone()
        return {
            "schedule": dict(schedule)
            if schedule is not None
            else {
                "agent_id": str(agent_id).lower(),
                "enabled": 1,
                "local_time": "00:00",
                "timezone": "local",
                "last_local_date": None,
            },
            "promoted_count": int(promoted["value"] if promoted is not None else 0),
            "pending_count": len(
                self.promotion_candidates(agent_id=agent_id, limit=5000)
            ),
        }

    def set_promotion_schedule(
        self,
        *,
        agent_id: str,
        enabled: bool | None = None,
        local_time: str | None = None,
        timezone_name: str | None = None,
    ) -> dict[str, Any]:
        current = self.promotion_status(agent_id=agent_id)["schedule"]
        resolved_enabled = (
            bool(current.get("enabled", 1)) if enabled is None else bool(enabled)
        )
        resolved_time = str(local_time or current.get("local_time") or "00:00")
        parts = resolved_time.split(":")
        if len(parts) != 2 or not all(part.isdigit() for part in parts):
            raise ValueError("promotion time must be HH:MM")
        hour, minute = (int(parts[0]), int(parts[1]))
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError("promotion time must be HH:MM")
        resolved_time = f"{hour:02d}:{minute:02d}"
        resolved_timezone = str(timezone_name or current.get("timezone") or "local")
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO memory_promotion_schedules(
                    agent_id, enabled, local_time, timezone, last_local_date, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(agent_id) DO UPDATE SET
                    enabled = excluded.enabled,
                    local_time = excluded.local_time,
                    timezone = excluded.timezone,
                    updated_at = excluded.updated_at
                """,
                (
                    str(agent_id).lower(),
                    int(resolved_enabled),
                    resolved_time,
                    resolved_timezone,
                    current.get("last_local_date"),
                    _utc_now(),
                ),
            )
        return self.promotion_status(agent_id=agent_id)["schedule"]

    def mark_promotion_schedule_ran(self, *, agent_id: str, local_date: str) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE memory_promotion_schedules
                SET last_local_date = ?, updated_at = ? WHERE agent_id = ?
                """,
                (str(local_date), _utc_now(), str(agent_id).lower()),
            )

    def session_workspace(self, session_id: str, context_generation: int) -> Path:
        safe_session = "".join(
            character
            for character in str(session_id)
            if character.isalnum() or character in {"_", "-"}
        )
        if safe_session != str(session_id) or not safe_session:
            raise ValueError("invalid session_id")
        generation = max(1, int(context_generation))
        path = self.workspaces_root / safe_session / f"generation_{generation}"
        path.mkdir(parents=True, exist_ok=True)
        return path


def _handoff_reloaded_session_store_class(
    previous_class: type | None,
    current_class: type,
) -> type:
    """Install reloaded behavior without invalidating live store instances."""

    if not isinstance(previous_class, type) or previous_class is current_class:
        return current_class

    protected = {"__dict__", "__module__", "__qualname__", "__weakref__"}
    previous_names = set(vars(previous_class))
    current_namespace = vars(current_class)
    for name in previous_names - set(current_namespace):
        if name in protected:
            continue
        try:
            delattr(previous_class, name)
        except (AttributeError, TypeError):
            pass
    for name, value in current_namespace.items():
        if name in protected:
            continue
        setattr(previous_class, name, value)
    return previous_class


# Exception identity matters to consumers that catch these classes directly.
# Restore the live identities first, then hand the complete new SessionStore
# implementation onto the live class.  A cold import has no previous classes
# and simply keeps the definitions above.
for _error_name, _previous_error_class in _PRE_RELOAD_SESSION_ERROR_CLASSES.items():
    globals()[_error_name] = _handoff_reloaded_session_store_class(
        _previous_error_class,
        globals()[_error_name],
    )
SessionStore = _handoff_reloaded_session_store_class(
    _PRE_RELOAD_SESSION_STORE_CLASS,
    SessionStore,
)


__all__ = [
    "TERMINAL_RUN_STATES",
    "AcceptedRun",
    "IdempotencyConflict",
    "SessionConflict",
    "SessionNotFound",
    "SessionStore",
    "SessionStoreError",
]
