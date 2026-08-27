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

    SCHEMA_VERSION = 2

    def __init__(self, db_path: str | Path, *, instance_id: str = "HASHI"):
        self.db_path = Path(db_path)
        self.instance_id = str(instance_id or "HASHI").upper()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.workspaces_root = self.db_path.parent / "session_workspaces"
        self.workspaces_root.mkdir(parents=True, exist_ok=True)
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
                    created_at TEXT NOT NULL,
                    committed_at TEXT,
                    FOREIGN KEY(session_id) REFERENCES sessions(session_id)
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
        return dict(row)

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
    ) -> AcceptedRun:
        clean = str(text or "").strip()
        if not clean:
            raise ValueError("message text is required")
        blocks = list(content or ({"type": "text", "text": clean},))
        digest_payload = {
            "content": blocks,
            "execution_mode": str(execution_mode or ""),
            "parent_run_id": str(parent_run_id or ""),
        }
        digest = hashlib.sha256(_json(digest_payload).encode("utf-8")).hexdigest()
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
                    effective_mode, context_generation, state, parent_run_id,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?)
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
                    generation,
                    parent_run_id,
                    now,
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

    def finish_request(
        self,
        request_id: str,
        *,
        success: bool,
        assistant_text: str | None = None,
        assistant_source: str = "",
        error_text: str | None = None,
        failure_state: str = "failed",
        fencing_token: int | None = None,
    ) -> dict[str, Any] | None:
        now = _utc_now()
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
                clean = str(assistant_text or "").strip()
                if not clean:
                    success = False
                    error_text = (
                        error_text or "backend returned no visible final response"
                    )
                else:
                    final_message_id = _new_id("msg")
                    ordinal = self._next_ordinal(connection, session_id)
                    content = [{"type": "text", "text": clean}]
                    content_json = _json(content)
                    connection.execute(
                        """
                        INSERT INTO messages(
                            message_id, session_id, run_id, ordinal, context_generation,
                            role, author_id, source, content_json, text, content_hash, created_at
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
                            hashlib.sha256(content_json.encode("utf-8")).hexdigest(),
                            now,
                        ),
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
    ) -> dict[str, Any]:
        self.get_session(session_id, owner_id=owner_id, include_deleted=False)
        digest = str(sha256).lower()
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise ValueError("sha256 must be 64 lowercase hexadecimal characters")
        attachment_id, now = _new_id("att"), _utc_now()
        with self._lock, self._connect() as connection:
            connection.execute(
                """INSERT INTO session_attachments(attachment_id,session_id,owner_id,filename,
                   media_type,size_bytes,sha256,created_at) VALUES(?,?,?,?,?,?,?,?)""",
                (
                    attachment_id,
                    str(session_id),
                    str(owner_id),
                    str(filename),
                    str(media_type),
                    max(0, int(size_bytes)),
                    digest,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM session_attachments WHERE attachment_id=?",
                (attachment_id,),
            ).fetchone()
        return dict(row)

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
            connection.execute(
                "UPDATE session_attachments SET state='committed', committed_at=COALESCE(committed_at,?) WHERE attachment_id=?",
                (now, str(attachment_id)),
            )
            updated = connection.execute(
                "SELECT * FROM session_attachments WHERE attachment_id=?",
                (str(attachment_id),),
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
                       u.text AS user_text, a.text AS assistant_text
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
                       u.text AS user_text, a.text AS assistant_text
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


__all__ = [
    "TERMINAL_RUN_STATES",
    "AcceptedRun",
    "IdempotencyConflict",
    "SessionConflict",
    "SessionNotFound",
    "SessionStore",
    "SessionStoreError",
]
