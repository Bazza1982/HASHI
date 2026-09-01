"""Durable provider-neutral state for HER v2 fixed-backend sessions.

The HASHI SessionStore owns the external conversation binding.  This store owns
HER's logical thread after that binding enters the backend: accepted messages,
materialised PCM/resource state, ordering, and terminal turn results.  No
provider-native thread, reasoning field, or SDK object is persisted here.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _object(value: str | bytes | None) -> dict[str, Any]:
    if not value:
        return {}
    parsed = json.loads(value)
    return dict(parsed) if isinstance(parsed, Mapping) else {}


def _array(value: str | bytes | None) -> list[Any]:
    if not value:
        return []
    parsed = json.loads(value)
    return list(parsed) if isinstance(parsed, list) else []


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _resource_key(resource: Mapping[str, Any]) -> str:
    for field in ("attachment_id", "asset_id", "local_ref", "sha256"):
        value = str(resource.get(field) or "").strip()
        if value:
            return f"{field}:{value}"
    return "digest:" + _digest(dict(resource)).removeprefix("sha256:")


def _resource_map(
    resources: list[Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    normalized: dict[str, dict[str, Any]] = {}
    for raw in resources:
        if not isinstance(raw, Mapping):
            raise HerSessionStoreError(
                "invalid_resource_delta", "Every resource must be an object."
            )
        resource = dict(raw)
        key = _resource_key(resource)
        existing = normalized.get(key)
        if existing is not None and existing != resource:
            raise HerSessionStoreError(
                "invalid_resource_delta",
                "A resource identity occurs more than once with conflicting content.",
            )
        normalized[key] = resource
    return normalized


class HerSessionStoreError(RuntimeError):
    """Typed durable-session conflict raised before model or tool work."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = str(code)


class HerSessionStore:
    """SQLite-backed materialised session state plus an append-only event log."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
            except Exception:
                connection.rollback()
                raise
            else:
                connection.commit()

    def _initialize(self) -> None:
        with self._lock, self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS her_sessions (
                    session_id TEXT PRIMARY KEY,
                    schema_version INTEGER NOT NULL DEFAULT 1,
                    instance_id TEXT NOT NULL DEFAULT '',
                    agent_id TEXT NOT NULL,
                    owner_id TEXT NOT NULL DEFAULT '',
                    hashi_conversation_id TEXT NOT NULL,
                    context_generation INTEGER NOT NULL,
                    workzone_identity TEXT NOT NULL,
                    epoch INTEGER NOT NULL,
                    state_version INTEGER NOT NULL,
                    canonical_sequence INTEGER NOT NULL,
                    pcm_revision INTEGER NOT NULL,
                    resource_revision INTEGER NOT NULL,
                    pcm_digest TEXT NOT NULL DEFAULT '',
                    resource_digest TEXT NOT NULL DEFAULT '',
                    pcm_json TEXT NOT NULL,
                    resources_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    last_turn_id TEXT,
                    last_routing_revision INTEGER NOT NULL DEFAULT 0,
                    provider_context_generation INTEGER NOT NULL DEFAULT 0,
                    last_route_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS her_turns (
                    session_id TEXT NOT NULL,
                    turn_id TEXT NOT NULL,
                    request_id TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    pcm_revision INTEGER NOT NULL DEFAULT 0,
                    resource_revision INTEGER NOT NULL DEFAULT 0,
                    authority_digest TEXT NOT NULL DEFAULT '',
                    user_message TEXT NOT NULL,
                    assistant_text TEXT,
                    error_text TEXT,
                    status TEXT NOT NULL,
                    accepted_at TEXT NOT NULL,
                    completed_at TEXT,
                    routing_revision INTEGER NOT NULL DEFAULT 0,
                    capability_revision INTEGER NOT NULL DEFAULT 0,
                    pricing_revision TEXT NOT NULL DEFAULT '',
                    route_snapshot_json TEXT NOT NULL DEFAULT '{}',
                    PRIMARY KEY(session_id, turn_id),
                    UNIQUE(session_id, message_id),
                    UNIQUE(session_id, idempotency_key),
                    FOREIGN KEY(session_id) REFERENCES her_sessions(session_id)
                        ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_her_turns_recent
                    ON her_turns(session_id, sequence DESC);

                CREATE TABLE IF NOT EXISTS her_session_events (
                    session_id TEXT NOT NULL,
                    canonical_sequence INTEGER NOT NULL,
                    event_id TEXT NOT NULL UNIQUE,
                    kind TEXT NOT NULL,
                    state_version INTEGER NOT NULL DEFAULT 0,
                    payload_json TEXT NOT NULL,
                    payload_digest TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(session_id, canonical_sequence),
                    FOREIGN KEY(session_id) REFERENCES her_sessions(session_id)
                        ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS her_active_turn_recovery (
                    session_id TEXT NOT NULL,
                    turn_id TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active',
                    current_stage TEXT NOT NULL DEFAULT 'initial',
                    routing_revision INTEGER NOT NULL DEFAULT 0,
                    capability_revision INTEGER NOT NULL DEFAULT 0,
                    pricing_revision TEXT NOT NULL DEFAULT '',
                    route_snapshot_json TEXT NOT NULL DEFAULT '{}',
                    strategy_json TEXT NOT NULL DEFAULT '{}',
                    plan_json TEXT NOT NULL DEFAULT '{}',
                    tool_receipts_json TEXT NOT NULL DEFAULT '[]',
                    side_effects_json TEXT NOT NULL DEFAULT '[]',
                    remaining_work_json TEXT NOT NULL DEFAULT '{}',
                    safe_to_resume INTEGER NOT NULL DEFAULT 1,
                    recovery_disposition TEXT NOT NULL DEFAULT '',
                    recovery_source_turn_id TEXT NOT NULL DEFAULT '',
                    last_event_sequence INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(session_id, turn_id),
                    FOREIGN KEY(session_id, turn_id)
                        REFERENCES her_turns(session_id, turn_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS her_settled_checkpoints (
                    session_id TEXT PRIMARY KEY,
                    checkpoint_version INTEGER NOT NULL DEFAULT 1,
                    settled_through_turn_id TEXT NOT NULL,
                    settled_through_turn_sequence INTEGER NOT NULL,
                    routing_revision INTEGER NOT NULL DEFAULT 0,
                    provider_context_generation INTEGER NOT NULL DEFAULT 0,
                    payload_json TEXT NOT NULL,
                    payload_digest TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(session_id) REFERENCES her_sessions(session_id)
                        ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS her_provider_requests (
                    provider_request_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    turn_id TEXT NOT NULL DEFAULT '',
                    parent_request_id TEXT NOT NULL DEFAULT '',
                    phase TEXT NOT NULL DEFAULT '',
                    provider TEXT NOT NULL DEFAULT '',
                    model TEXT NOT NULL DEFAULT '',
                    input_tokens INTEGER NOT NULL DEFAULT 0,
                    output_tokens INTEGER NOT NULL DEFAULT 0,
                    thinking_tokens INTEGER NOT NULL DEFAULT 0,
                    prompt_cache_hit_tokens INTEGER,
                    prompt_cache_miss_tokens INTEGER,
                    token_source TEXT NOT NULL DEFAULT 'estimated',
                    -- Deprecated compatibility mirrors.  New writes keep
                    -- valuation authority in her_provider_request_valuations.
                    cost_usd REAL,
                    cost_source TEXT NOT NULL DEFAULT 'unknown',
                    provider_call_latency_ms REAL,
                    attempt INTEGER NOT NULL DEFAULT 1,
                    retry_count INTEGER NOT NULL DEFAULT 0,
                    recovery_kind TEXT NOT NULL DEFAULT 'none',
                    compact INTEGER NOT NULL DEFAULT 0,
                    routing_revision INTEGER NOT NULL DEFAULT 0,
                    capability_revision INTEGER NOT NULL DEFAULT 0,
                    pricing_revision TEXT NOT NULL DEFAULT 'unknown',
                    status TEXT NOT NULL DEFAULT 'completed',
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(session_id) REFERENCES her_sessions(session_id)
                        ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS her_provider_request_valuations (
                    provider_request_id TEXT NOT NULL,
                    pricing_revision TEXT NOT NULL,
                    cost_usd REAL,
                    cost_source TEXT NOT NULL DEFAULT 'unknown',
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(provider_request_id, pricing_revision),
                    FOREIGN KEY(provider_request_id)
                        REFERENCES her_provider_requests(provider_request_id)
                        ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_her_provider_requests_session
                    ON her_provider_requests(session_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_her_provider_requests_turn
                    ON her_provider_requests(session_id, turn_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_her_provider_request_valuations_revision
                    ON her_provider_request_valuations(pricing_revision, created_at);
                """
            )
            migrations = {
                "her_sessions": {
                    "schema_version": "INTEGER NOT NULL DEFAULT 1",
                    "instance_id": "TEXT NOT NULL DEFAULT ''",
                    "owner_id": "TEXT NOT NULL DEFAULT ''",
                    "pcm_digest": "TEXT NOT NULL DEFAULT ''",
                    "resource_digest": "TEXT NOT NULL DEFAULT ''",
                    "last_routing_revision": "INTEGER NOT NULL DEFAULT 0",
                    "provider_context_generation": "INTEGER NOT NULL DEFAULT 0",
                    "last_route_json": "TEXT NOT NULL DEFAULT '{}'",
                },
                "her_turns": {
                    "pcm_revision": "INTEGER NOT NULL DEFAULT 0",
                    "resource_revision": "INTEGER NOT NULL DEFAULT 0",
                    "authority_digest": "TEXT NOT NULL DEFAULT ''",
                    "routing_revision": "INTEGER NOT NULL DEFAULT 0",
                    "capability_revision": "INTEGER NOT NULL DEFAULT 0",
                    "pricing_revision": "TEXT NOT NULL DEFAULT ''",
                    "route_snapshot_json": "TEXT NOT NULL DEFAULT '{}'",
                },
                "her_session_events": {
                    "state_version": "INTEGER NOT NULL DEFAULT 0",
                    "payload_digest": "TEXT NOT NULL DEFAULT ''",
                },
                "her_active_turn_recovery": {
                    "recovery_source_turn_id": "TEXT NOT NULL DEFAULT ''",
                },
            }
            for table, declarations in migrations.items():
                existing_columns = {
                    str(row["name"])
                    for row in connection.execute(
                        f"PRAGMA table_info({table})"
                    ).fetchall()
                }
                for column, declaration in declarations.items():
                    if column not in existing_columns:
                        connection.execute(
                            f"ALTER TABLE {table} ADD COLUMN {column} {declaration}"
                        )

            # One-time compatibility lift for databases created while costs
            # still lived beside immutable request facts.  All subsequent
            # reads and writes use the valuation table as their authority.
            provider_columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(her_provider_requests)"
                ).fetchall()
            }
            if {"cost_usd", "cost_source", "pricing_revision"} <= provider_columns:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO her_provider_request_valuations(
                        provider_request_id, pricing_revision, cost_usd,
                        cost_source, created_at
                    )
                    SELECT provider_request_id,
                           COALESCE(NULLIF(pricing_revision, ''), 'legacy'),
                           cost_usd, COALESCE(NULLIF(cost_source, ''), 'unknown'),
                           created_at
                    FROM her_provider_requests
                    """
                )

    @staticmethod
    def _session_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        result = dict(row)
        result["pcm"] = _object(result.pop("pcm_json", "{}"))
        result["resources"] = _object(result.pop("resources_json", "{}"))
        result["last_route"] = _object(result.pop("last_route_json", "{}"))
        return result

    @staticmethod
    def _turn_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        result = dict(row)
        result["route_snapshot"] = _object(
            result.pop("route_snapshot_json", "{}")
        )
        return result

    def session(self, session_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM her_sessions WHERE session_id = ?",
                (str(session_id),),
            ).fetchone()
        return self._session_dict(row)

    def turn_by_idempotency(
        self, session_id: str, idempotency_key: str
    ) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM her_turns
                WHERE session_id = ? AND idempotency_key = ?
                """,
                (str(session_id), str(idempotency_key)),
            ).fetchone()
        return self._turn_dict(row)

    def recent_completed_turns(
        self, session_id: str, *, limit: int = 8
    ) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM her_turns
                WHERE session_id = ? AND status = 'completed'
                ORDER BY sequence DESC LIMIT ?
                """,
                (str(session_id), max(0, int(limit))),
            ).fetchall()
        return [dict(row) for row in reversed(rows)]

    @staticmethod
    def _assert_binding(
        row: sqlite3.Row,
        *,
        instance_id: str,
        agent_id: str,
        owner_id: str,
        hashi_conversation_id: str,
        context_generation: int,
        workzone_identity: str,
    ) -> None:
        expected = (
            str(instance_id).casefold(),
            str(agent_id).casefold(),
            str(owner_id),
            str(hashi_conversation_id),
            int(context_generation),
            str(workzone_identity),
        )
        observed = (
            str(row["instance_id"]).casefold(),
            str(row["agent_id"]).casefold(),
            str(row["owner_id"]),
            str(row["hashi_conversation_id"]),
            int(row["context_generation"]),
            str(row["workzone_identity"]),
        )
        if observed != expected:
            raise HerSessionStoreError(
                "session_binding_conflict",
                "HER session binding does not match the authoritative HASHI conversation.",
            )

    @staticmethod
    def _next_event(
        connection: sqlite3.Connection,
        *,
        session_id: str,
        kind: str,
        event_id: str,
        payload: Mapping[str, Any],
    ) -> int:
        existing = connection.execute(
            """
            SELECT session_id, canonical_sequence, kind, payload_json
            FROM her_session_events WHERE event_id = ?
            """,
            (str(event_id),),
        ).fetchone()
        if existing is not None:
            if str(existing["session_id"]) != str(session_id):
                raise HerSessionStoreError(
                    "event_id_conflict", "Canonical event ID belongs to another Session."
                )
            if (
                str(existing["kind"]) != str(kind)
                or str(existing["payload_json"]) != _json(dict(payload))
            ):
                raise HerSessionStoreError(
                    "event_id_conflict",
                    "Canonical event ID was reused with different immutable facts.",
                )
            return int(existing["canonical_sequence"])
        row = connection.execute(
            "SELECT canonical_sequence, state_version FROM her_sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        sequence = int(row["canonical_sequence"] if row is not None else 0) + 1
        state_version = int(row["state_version"] if row is not None else 0)
        event_payload = dict(payload)
        event_digest = _digest(
            {
                "session_id": session_id,
                "canonical_sequence": sequence,
                "event_id": event_id,
                "kind": kind,
                "state_version": state_version,
                "payload": event_payload,
            }
        )
        connection.execute(
            """
            INSERT INTO her_session_events(
                session_id, canonical_sequence, event_id, kind,
                state_version, payload_json, payload_digest, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                sequence,
                event_id,
                kind,
                state_version,
                _json(event_payload),
                event_digest,
                _utc_now(),
            ),
        )
        connection.execute(
            "UPDATE her_sessions SET canonical_sequence = ? WHERE session_id = ?",
            (sequence, session_id),
        )
        return sequence

    def open_session(
        self,
        *,
        session_id: str,
        instance_id: str,
        agent_id: str,
        owner_id: str,
        hashi_conversation_id: str,
        context_generation: int,
        workzone_identity: str,
        epoch: int,
        pcm_revision: int,
        pcm: Mapping[str, Any],
        resource_revision: int,
        resources: Mapping[str, Any],
        turn_id: str,
        request_id: str,
        message_id: str,
        idempotency_key: str,
        user_message: str,
    ) -> dict[str, Any]:
        now = _utc_now()
        pcm_state = dict(pcm)
        pcm_digest = _digest(pcm_state)
        resource_state = dict(resources)
        initial_attachments = list(resource_state.get("attachments") or [])
        initial_resource_map = _resource_map(initial_attachments)
        resource_digest = str(resource_state.get("digest") or "")
        if resource_digest != _digest(initial_resource_map):
            raise HerSessionStoreError(
                "resource_digest_conflict",
                "HER resource snapshot digest does not match its attachment state.",
            )
        authority_digest = _digest(
            {"pcm_digest": pcm_digest, "resource_digest": resource_digest}
        )
        with self._transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM her_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if existing is not None:
                self._assert_binding(
                    existing,
                    instance_id=instance_id,
                    agent_id=agent_id,
                    owner_id=owner_id,
                    hashi_conversation_id=hashi_conversation_id,
                    context_generation=context_generation,
                    workzone_identity=workzone_identity,
                )
                duplicate = connection.execute(
                    """
                    SELECT * FROM her_turns
                    WHERE session_id = ? AND idempotency_key = ?
                    """,
                    (session_id, idempotency_key),
                ).fetchone()
                if duplicate is not None:
                    if (
                        str(duplicate["message_id"]) != message_id
                        or str(duplicate["user_message"]) != user_message
                    ):
                        raise HerSessionStoreError(
                            "duplicate_message_conflict",
                            "Idempotency key was already accepted with different content.",
                        )
                    return {
                        "session": self._session_dict(existing),
                        "turn": self._turn_dict(duplicate),
                        "duplicate": True,
                    }
                raise HerSessionStoreError(
                    "state_version_conflict",
                    "HER session already exists; append or resume it instead of reopening.",
                )

            connection.execute(
                """
                INSERT INTO her_sessions(
                    session_id, schema_version, instance_id, agent_id, owner_id,
                    hashi_conversation_id,
                    context_generation, workzone_identity, epoch,
                    state_version, canonical_sequence, pcm_revision,
                    resource_revision, pcm_digest, resource_digest,
                    pcm_json, resources_json, status,
                    last_turn_id, created_at, updated_at
                ) VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?, 1, 0, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?)
                """,
                (
                    session_id,
                    str(instance_id).casefold(),
                    str(agent_id).casefold(),
                    str(owner_id),
                    hashi_conversation_id,
                    int(context_generation),
                    workzone_identity,
                    int(epoch),
                    int(pcm_revision),
                    int(resource_revision),
                    pcm_digest,
                    resource_digest,
                    _json(pcm_state),
                    _json(resource_state),
                    turn_id,
                    now,
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO her_turns(
                    session_id, turn_id, request_id, message_id,
                    idempotency_key, sequence, pcm_revision, resource_revision,
                    authority_digest, user_message, status, accepted_at
                ) VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?, 'active', ?)
                """,
                (
                    session_id,
                    turn_id,
                    request_id,
                    message_id,
                    idempotency_key,
                    int(pcm_revision),
                    int(resource_revision),
                    authority_digest,
                    user_message,
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO her_active_turn_recovery(
                    session_id, turn_id, status, current_stage,
                    remaining_work_json, safe_to_resume, created_at, updated_at
                ) VALUES (?, ?, 'active', 'initial', ?, 1, ?, ?)
                """,
                (
                    session_id,
                    turn_id,
                    _json({"goal": user_message, "status": "accepted"}),
                    now,
                    now,
                ),
            )
            self._next_event(
                connection,
                session_id=session_id,
                event_id=f"{session_id}:opened",
                kind="session_opened",
                payload={
                    "epoch": int(epoch),
                    "state_version": 1,
                    "pcm_revision": int(pcm_revision),
                    "resource_revision": int(resource_revision),
                    "pcm_digest": pcm_digest,
                    "resource_digest": resource_digest,
                },
            )
            sequence = self._next_event(
                connection,
                session_id=session_id,
                event_id=f"{turn_id}:accepted",
                kind="turn_accepted",
                payload={"turn_id": turn_id, "message_id": message_id},
            )
            session_row = connection.execute(
                "SELECT * FROM her_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            turn_row = connection.execute(
                "SELECT * FROM her_turns WHERE session_id = ? AND turn_id = ?",
                (session_id, turn_id),
            ).fetchone()
            return {
                "session": self._session_dict(session_row),
                "turn": self._turn_dict(turn_row),
                "duplicate": False,
                "canonical_sequence": sequence,
            }

    @staticmethod
    def _recovery_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        result = dict(row)
        for source, target, kind in (
            ("route_snapshot_json", "route_snapshot", "object"),
            ("strategy_json", "strategy", "object"),
            ("plan_json", "plan", "object"),
            ("tool_receipts_json", "tool_receipts", "array"),
            ("side_effects_json", "side_effects", "array"),
            ("remaining_work_json", "remaining_work", "object"),
        ):
            raw = result.pop(source, "{}" if kind == "object" else "[]")
            result[target] = _object(raw) if kind == "object" else _array(raw)
        result["safe_to_resume"] = bool(result.get("safe_to_resume"))
        return result

    def active_turn_recovery(
        self, session_id: str, turn_id: str | None = None
    ) -> dict[str, Any] | None:
        query = "SELECT * FROM her_active_turn_recovery WHERE session_id = ?"
        arguments: tuple[Any, ...] = (str(session_id),)
        if turn_id:
            query += " AND turn_id = ?"
            arguments = (str(session_id), str(turn_id))
        else:
            query += " ORDER BY created_at DESC LIMIT 1"
        with self._lock, self._connect() as connection:
            row = connection.execute(query, arguments).fetchone()
        return self._recovery_dict(row)

    def settled_checkpoint(self, session_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM her_settled_checkpoints WHERE session_id = ?",
                (str(session_id),),
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["payload"] = _object(result.pop("payload_json", "{}"))
        return result

    def consume_recovery_context(
        self, *, session_id: str, current_turn_id: str
    ) -> dict[str, Any] | None:
        """Bind one interrupted canonical state to the next Turn transactionally.

        The source is deliberately left terminated until the receiving Turn
        settles.  Its evidence is also copied into the receiving projection so
        another process failure advances the recovery chain instead of losing
        the original side-effect boundary.
        """

        with self._transaction() as connection:
            current = connection.execute(
                """
                SELECT * FROM her_active_turn_recovery
                WHERE session_id = ? AND turn_id = ? AND status = 'active'
                """,
                (session_id, current_turn_id),
            ).fetchone()
            if current is None:
                raise HerSessionStoreError(
                    "unknown_active_turn",
                    "Recovery context can be bound only to an active HER Turn.",
                )
            existing_source = str(current["recovery_source_turn_id"] or "")
            if existing_source:
                row = connection.execute(
                    """
                    SELECT * FROM her_active_turn_recovery
                    WHERE session_id = ? AND turn_id = ?
                    """,
                    (session_id, existing_source),
                ).fetchone()
            else:
                row = connection.execute(
                    """
                    SELECT source.* FROM her_active_turn_recovery AS source
                    WHERE source.session_id = ? AND source.turn_id != ?
                      AND source.status = 'terminated'
                      AND source.recovery_disposition NOT IN ('cancelled', 'settled')
                      AND NOT EXISTS (
                          SELECT 1 FROM her_active_turn_recovery AS child
                          WHERE child.session_id = source.session_id
                            AND child.recovery_source_turn_id = source.turn_id
                            AND child.status IN ('active', 'terminated')
                      )
                    ORDER BY source.created_at DESC LIMIT 1
                    """,
                    (session_id, current_turn_id),
                ).fetchone()
            state = self._recovery_dict(row)
            if state is None:
                return None
            payload = {
                "format": "her-active-turn-recovery-context-v1",
                "authority": "quoted_recovery_context",
                "interrupted_turn_id": str(state.get("turn_id") or ""),
                "current_stage": str(state.get("current_stage") or ""),
                "routing_revision": int(state.get("routing_revision") or 0),
                "strategy": state.get("strategy") or {},
                "plan": state.get("plan") or {},
                "tool_receipts": state.get("tool_receipts") or [],
                "side_effects": state.get("side_effects") or [],
                "remaining_work": state.get("remaining_work") or {},
                "safe_to_resume": bool(state.get("safe_to_resume")),
                "recovery_disposition": str(
                    state.get("recovery_disposition") or ""
                ),
                "limitations": [
                    "This records externally observable work, not hidden reasoning.",
                    "UNKNOWN_SIDE_EFFECT entries must be investigated, never replayed automatically.",
                ],
            }
            if not existing_source:
                current_state = self._recovery_dict(current) or {}
                source_receipts = list(state.get("tool_receipts") or [])
                current_receipts = list(current_state.get("tool_receipts") or [])
                source_effects = [
                    {
                        **dict(item),
                        "inherited_from_turn_id": str(
                            item.get("inherited_from_turn_id")
                            or state.get("turn_id")
                            or ""
                        ),
                    }
                    for item in (state.get("side_effects") or [])
                    if isinstance(item, Mapping)
                ]
                current_effects = list(current_state.get("side_effects") or [])
                current_remaining = dict(current_state.get("remaining_work") or {})
                connection.execute(
                    """
                    UPDATE her_active_turn_recovery
                    SET strategy_json = ?, plan_json = ?, tool_receipts_json = ?,
                        side_effects_json = ?, remaining_work_json = ?,
                        safe_to_resume = ?, recovery_source_turn_id = ?,
                        updated_at = ?
                    WHERE session_id = ? AND turn_id = ?
                    """,
                    (
                        _json(current_state.get("strategy") or state.get("strategy") or {}),
                        _json(current_state.get("plan") or state.get("plan") or {}),
                        _json(
                            self._bounded_tool_receipts(
                                [*source_receipts, *current_receipts],
                                [*source_effects, *current_effects],
                            )
                        ),
                        _json(
                            self._bounded_side_effects(
                                [*source_effects, *current_effects]
                            )
                        ),
                        _json(
                            {
                                **current_remaining,
                                "recovery_source_turn_id": str(state.get("turn_id") or ""),
                                "inherited_recovery": state.get("remaining_work") or {},
                            }
                        ),
                        1
                        if bool(current_state.get("safe_to_resume", True))
                        and bool(state.get("safe_to_resume"))
                        else 0,
                        str(state.get("turn_id") or ""),
                        _utc_now(),
                        session_id,
                        current_turn_id,
                    ),
                )
            self._next_event(
                connection,
                session_id=session_id,
                event_id=(
                    f"{current_turn_id}:recovery-context:"
                    f"{str(state.get('turn_id') or '')}"
                ),
                kind="active_turn_recovery_context_consumed",
                payload={
                    "current_turn_id": current_turn_id,
                    "interrupted_turn_id": str(state.get("turn_id") or ""),
                    "safe_to_resume": bool(state.get("safe_to_resume")),
                    "recovery_disposition": str(
                        state.get("recovery_disposition") or ""
                    ),
                },
            )
            return payload

    def _archive_recovery_ancestors(
        self,
        connection: sqlite3.Connection,
        *,
        session_id: str,
        turn_id: str,
        now: str,
    ) -> list[str]:
        """Archive a settled/cancelled Turn's handed-off recovery ancestry."""

        archived: list[str] = []
        row = connection.execute(
            """
            SELECT recovery_source_turn_id FROM her_active_turn_recovery
            WHERE session_id = ? AND turn_id = ?
            """,
            (session_id, turn_id),
        ).fetchone()
        source_turn_id = str(row["recovery_source_turn_id"] or "") if row else ""
        seen: set[str] = set()
        while source_turn_id and source_turn_id not in seen:
            seen.add(source_turn_id)
            source = connection.execute(
                """
                SELECT recovery_source_turn_id FROM her_active_turn_recovery
                WHERE session_id = ? AND turn_id = ?
                """,
                (session_id, source_turn_id),
            ).fetchone()
            if source is None:
                break
            connection.execute(
                """
                UPDATE her_active_turn_recovery
                SET status = 'archived', updated_at = ?
                WHERE session_id = ? AND turn_id = ? AND status = 'terminated'
                """,
                (now, session_id, source_turn_id),
            )
            archived.append(source_turn_id)
            source_turn_id = str(source["recovery_source_turn_id"] or "")
        if archived:
            self._next_event(
                connection,
                session_id=session_id,
                event_id=f"{turn_id}:recovery-handoff-settled",
                kind="active_turn_recovery_handoff_settled",
                payload={
                    "turn_id": turn_id,
                    "archived_source_turn_ids": archived,
                },
            )
        return archived

    def freeze_turn_routing(
        self,
        *,
        session_id: str,
        turn_id: str,
        routing_revision: int,
        capability_revision: int,
        pricing_revision: str,
        route_snapshot: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Bind one immutable route to a Turn and detect provider rebuilds."""

        now = _utc_now()
        snapshot = dict(route_snapshot)
        with self._transaction() as connection:
            session = connection.execute(
                "SELECT * FROM her_sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
            turn = connection.execute(
                "SELECT * FROM her_turns WHERE session_id = ? AND turn_id = ?",
                (session_id, turn_id),
            ).fetchone()
            if session is None or turn is None:
                raise HerSessionStoreError(
                    "unknown_turn", "Cannot freeze routing for an unknown HER turn."
                )
            if str(turn["status"]) != "active":
                raise HerSessionStoreError(
                    "turn_not_active", "Routing can be frozen only for an active Turn."
                )
            existing_revision = int(turn["routing_revision"] or 0)
            existing_snapshot = _object(turn["route_snapshot_json"])
            if existing_revision:
                if existing_revision != int(routing_revision) or existing_snapshot != snapshot:
                    raise HerSessionStoreError(
                        "turn_route_conflict", "The active Turn route is already frozen."
                    )
                checkpoint = connection.execute(
                    "SELECT * FROM her_settled_checkpoints WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
                event = connection.execute(
                    """
                    SELECT canonical_sequence, payload_json FROM her_session_events
                    WHERE session_id = ? AND event_id = ?
                    """,
                    (
                        session_id,
                        f"{turn_id}:routing-frozen:{existing_revision}",
                    ),
                ).fetchone()
                event_payload = _object(event["payload_json"]) if event else {}
                return {
                    **event_payload,
                    "routing_revision": existing_revision,
                    "provider_context_generation": int(
                        event_payload.get("provider_context_generation")
                        or session["provider_context_generation"]
                        or 0
                    ),
                    "rebuild_from_checkpoint": bool(
                        event_payload.get("rebuild_from_checkpoint", False)
                    ),
                    "canonical_sequence": (
                        int(event["canonical_sequence"]) if event else 0
                    ),
                    "checkpoint": (
                        _object(checkpoint["payload_json"]) if checkpoint else None
                    ),
                }

            checkpoint = connection.execute(
                "SELECT * FROM her_settled_checkpoints WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            checkpoint_payload = _object(checkpoint["payload_json"]) if checkpoint else {}
            previous_revision = int(checkpoint["routing_revision"] or 0) if checkpoint else 0
            previous_snapshot = dict(checkpoint_payload.get("route_snapshot") or {})
            changed = bool(checkpoint) and (
                previous_revision != int(routing_revision)
                or bool(previous_snapshot) and previous_snapshot != snapshot
            )
            generation = int(session["provider_context_generation"] or 0)
            if generation <= 0 or changed:
                generation += 1
            connection.execute(
                """
                UPDATE her_turns
                SET routing_revision = ?, capability_revision = ?,
                    pricing_revision = ?, route_snapshot_json = ?
                WHERE session_id = ? AND turn_id = ?
                """,
                (
                    int(routing_revision),
                    int(capability_revision),
                    str(pricing_revision),
                    _json(snapshot),
                    session_id,
                    turn_id,
                ),
            )
            connection.execute(
                """
                UPDATE her_sessions
                SET last_routing_revision = ?, provider_context_generation = ?,
                    last_route_json = ?, updated_at = ?
                WHERE session_id = ?
                """,
                (int(routing_revision), generation, _json(snapshot), now, session_id),
            )
            connection.execute(
                """
                UPDATE her_active_turn_recovery
                SET routing_revision = ?, capability_revision = ?,
                    pricing_revision = ?, route_snapshot_json = ?, updated_at = ?
                WHERE session_id = ? AND turn_id = ?
                """,
                (
                    int(routing_revision),
                    int(capability_revision),
                    str(pricing_revision),
                    _json(snapshot),
                    now,
                    session_id,
                    turn_id,
                ),
            )
            event_payload = {
                "turn_id": turn_id,
                "routing_revision": int(routing_revision),
                "capability_revision": int(capability_revision),
                "pricing_revision": str(pricing_revision),
                "provider_context_generation": generation,
                "rebuild_from_checkpoint": changed,
                "checkpoint_digest": (
                    str(checkpoint["payload_digest"]) if checkpoint else None
                ),
            }
            sequence = self._next_event(
                connection,
                session_id=session_id,
                event_id=f"{turn_id}:routing-frozen:{int(routing_revision)}",
                kind=(
                    "provider_context_rebuilt_from_checkpoint"
                    if changed
                    else "turn_routing_frozen"
                ),
                payload=event_payload,
            )
            connection.execute(
                """
                UPDATE her_active_turn_recovery SET last_event_sequence = ?
                WHERE session_id = ? AND turn_id = ?
                """,
                (sequence, session_id, turn_id),
            )
            return {
                **event_payload,
                "canonical_sequence": sequence,
                "checkpoint": checkpoint_payload or None,
            }

    @staticmethod
    def _bounded_rows(rows: list[Any], *, limit: int = 256) -> list[Any]:
        return rows[-max(1, int(limit)) :]

    @staticmethod
    def _bounded_side_effects(
        rows: list[Mapping[str, Any]], *, limit: int = 256
    ) -> list[dict[str, Any]]:
        """Bound settled evidence without ever dropping unresolved effects."""

        normalized = [dict(item) for item in rows if isinstance(item, Mapping)]
        cap = max(1, int(limit))
        if len(normalized) <= cap:
            return normalized
        unresolved = {
            index
            for index, item in enumerate(normalized)
            if item.get("state") in {"pending", "unknown"}
        }
        resolved_budget = max(0, cap - len(unresolved))
        resolved_indices = [
            index for index in range(len(normalized)) if index not in unresolved
        ]
        resolved = (
            resolved_indices[-resolved_budget:] if resolved_budget else []
        )
        keep = unresolved | set(resolved)
        return [item for index, item in enumerate(normalized) if index in keep]

    @staticmethod
    def _unresolved_side_effects(rows: list[Any]) -> list[dict[str, Any]]:
        return [
            dict(item)
            for item in rows
            if isinstance(item, Mapping)
            and item.get("state") in {"pending", "unknown"}
        ]

    @classmethod
    def _bounded_tool_receipts(
        cls,
        rows: list[Mapping[str, Any]],
        effects: list[Mapping[str, Any]],
        *,
        limit: int = 256,
    ) -> list[dict[str, Any]]:
        """Bound settled receipts while retaining every unresolved counterpart."""

        normalized = [dict(item) for item in rows if isinstance(item, Mapping)]
        cap = max(1, int(limit))
        if len(normalized) <= cap:
            return normalized
        unresolved_effects = cls._unresolved_side_effects(list(effects))
        unresolved_operations = {
            str(item.get("operation_id") or "")
            for item in unresolved_effects
            if str(item.get("operation_id") or "")
        }
        unresolved_legacy_calls = {
            str(item.get("tool_call_id") or "")
            for item in unresolved_effects
            if not str(item.get("operation_id") or "")
            and str(item.get("tool_call_id") or "")
        }
        keep_unresolved: set[int] = set()
        for index, receipt in enumerate(normalized):
            operation_id = str(receipt.get("operation_id") or "")
            call_id = str(receipt.get("tool_call_id") or "")
            status = str(receipt.get("status") or "").casefold()
            if (
                operation_id in unresolved_operations
                or not operation_id
                and call_id in unresolved_legacy_calls
                or not bool(receipt.get("completed"))
                or status not in {"success", "completed"}
            ):
                keep_unresolved.add(index)
        settled_budget = max(0, cap - len(keep_unresolved))
        settled_indices = [
            index for index in range(len(normalized)) if index not in keep_unresolved
        ]
        keep = keep_unresolved | set(
            settled_indices[-settled_budget:] if settled_budget else []
        )
        return [item for index, item in enumerate(normalized) if index in keep]

    @classmethod
    def _project_runtime_event(
        cls,
        row: sqlite3.Row,
        *,
        event: str,
        stage: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        strategy = _object(row["strategy_json"])
        plan = _object(row["plan_json"])
        receipts = _array(row["tool_receipts_json"])
        effects = _array(row["side_effects_json"])
        remaining = _object(row["remaining_work_json"])
        status = str(row["status"])
        current_stage = str(row["current_stage"])
        disposition = str(row["recovery_disposition"])

        if event == "request_received":
            remaining = {
                "goal": str(payload.get("request") or remaining.get("goal") or ""),
                "status": "in_progress",
            }
        elif event == "stage_started":
            current_stage = stage or current_stage
            remaining = {**remaining, "current_stage": current_stage}
        elif event == "strategy_recorded":
            strategy = dict(payload)
        elif event == "stage_completed":
            if stage in {"planning", "replanning"}:
                output = payload.get("output")
                plan = dict(output) if isinstance(output, Mapping) else {"text": str(output or "")}
            raw_receipts = payload.get("tool_receipts")
            if isinstance(raw_receipts, list):
                receipts = cls._bounded_tool_receipts(
                    [
                        *receipts,
                        *(
                            dict(item)
                            for item in raw_receipts
                            if isinstance(item, Mapping)
                        ),
                    ],
                    effects,
                )
        elif event == "tool_intent":
            if not bool(payload.get("read_only", False)):
                effects = cls._bounded_side_effects(
                    [
                        *effects,
                        {
                            "operation_id": str(payload.get("operation_id") or ""),
                            "invocation_id": str(payload.get("invocation_id") or ""),
                            "attempt": max(1, int(payload.get("attempt") or 1)),
                            "tool_call_id": str(payload.get("tool_call_id") or ""),
                            "tool_name": str(payload.get("tool_name") or ""),
                            "arguments_sha256": str(payload.get("arguments_sha256") or ""),
                            "state": "pending",
                        },
                    ]
                )
        elif event == "tool_receipt":
            receipt = dict(payload.get("receipt") or {})
            if receipt:
                receipt.setdefault(
                    "operation_id", str(payload.get("operation_id") or "")
                )
                receipts.append(receipt)
            call_id = str(payload.get("tool_call_id") or receipt.get("tool_call_id") or "")
            operation_id = str(payload.get("operation_id") or "")
            for effect in effects:
                effect_operation_id = str(effect.get("operation_id") or "")
                matches = (
                    effect_operation_id == operation_id
                    if operation_id and effect_operation_id
                    else not operation_id
                    and not effect_operation_id
                    and str(effect.get("tool_call_id") or "") == call_id
                )
                if matches:
                    receipt_status = str(receipt.get("status") or "").casefold()
                    receipt_completed = bool(receipt.get("completed"))
                    if receipt_completed:
                        # A completed failure may have made a partial change,
                        # but it is not an ambiguous in-flight operation: the
                        # same Agent thread received a durable failed receipt
                        # and can recover or verify before finishing. Never
                        # replay it automatically; retain the failed state and
                        # receipt details for audit. UNKNOWN_SIDE_EFFECT is
                        # reserved for operations with no completed receipt.
                        effect["state"] = (
                            "completed"
                            if receipt_status in {"success", "completed"}
                            else "failed"
                        )
                    else:
                        effect["state"] = "unknown"
                    effect["receipt_status"] = receipt_status
            receipts = cls._bounded_tool_receipts(receipts, effects)
        elif event == "transition":
            target = str(payload.get("to") or "")
            remaining = {**remaining, "lifecycle": target}
            if target in {
                "COMPLETED",
                "COMPLETED_WITH_LIMITATIONS",
            }:
                status = "settled"
                remaining["status"] = "completed"
            elif target in {"FAILED", "ERROR", "STOPPED", "PENDING_USER_INPUT"}:
                status = "terminated"
                disposition = target.casefold()
                remaining["status"] = "terminated"

        pending_effect = any(
            item.get("state") in {"pending", "unknown"} for item in effects
        )
        effects = cls._bounded_side_effects(effects)
        safe = not pending_effect
        return {
            "status": status,
            "current_stage": current_stage,
            "strategy_json": _json(strategy),
            "plan_json": _json(plan),
            "tool_receipts_json": _json(receipts),
            "side_effects_json": _json(effects),
            "remaining_work_json": _json(remaining),
            "safe_to_resume": 1 if safe else 0,
            "recovery_disposition": disposition,
        }

    def record_runtime_event(
        self,
        *,
        session_id: str,
        turn_id: str,
        record: Mapping[str, Any],
    ) -> int:
        """Append one canonical runtime event and update recovery projection."""

        external_event_id = str(record.get("event_id") or "").strip()
        if not external_event_id:
            raise HerSessionStoreError("invalid_event", "Runtime event ID is required.")
        event = str(record.get("event") or "runtime_event")
        stage = str(record.get("stage") or "")
        payload = record.get("payload")
        payload = dict(payload) if isinstance(payload, Mapping) else {}
        canonical_payload = {
            "turn_id": turn_id,
            "external_event_id": external_event_id,
            "event": event,
            "stage": stage,
            "role": str(record.get("role") or ""),
            "provider": str(record.get("provider") or ""),
            "model": str(record.get("model") or ""),
            "attempt": max(1, int(record.get("attempt") or 1)),
            "plan_id": record.get("plan_id"),
            "facts": payload,
        }
        with self._transaction() as connection:
            existing = connection.execute(
                """
                SELECT canonical_sequence, kind, payload_json
                FROM her_session_events
                WHERE event_id = ?
                """,
                (f"runtime:{external_event_id}",),
            ).fetchone()
            if existing is not None:
                if (
                    str(existing["kind"]) != f"runtime_{event}"
                    or str(existing["payload_json"]) != _json(canonical_payload)
                ):
                    raise HerSessionStoreError(
                        "event_id_conflict",
                        "Runtime event ID was reused with different immutable facts.",
                    )
                return int(existing["canonical_sequence"])
            recovery = connection.execute(
                """
                SELECT * FROM her_active_turn_recovery
                WHERE session_id = ? AND turn_id = ?
                """,
                (session_id, turn_id),
            ).fetchone()
            if recovery is None:
                raise HerSessionStoreError(
                    "unknown_turn", "Canonical runtime event has no recovery state."
                )
            sequence = self._next_event(
                connection,
                session_id=session_id,
                event_id=f"runtime:{external_event_id}",
                kind=f"runtime_{event}",
                payload=canonical_payload,
            )
            projection = self._project_runtime_event(
                recovery, event=event, stage=stage, payload=payload
            )
            connection.execute(
                """
                UPDATE her_active_turn_recovery
                SET status = ?, current_stage = ?, strategy_json = ?, plan_json = ?,
                    tool_receipts_json = ?, side_effects_json = ?,
                    remaining_work_json = ?, safe_to_resume = ?,
                    recovery_disposition = ?, last_event_sequence = ?, updated_at = ?
                WHERE session_id = ? AND turn_id = ?
                """,
                (
                    projection["status"],
                    projection["current_stage"],
                    projection["strategy_json"],
                    projection["plan_json"],
                    projection["tool_receipts_json"],
                    projection["side_effects_json"],
                    projection["remaining_work_json"],
                    projection["safe_to_resume"],
                    projection["recovery_disposition"],
                    sequence,
                    _utc_now(),
                    session_id,
                    turn_id,
                ),
            )
            return sequence

    def record_provider_requests(
        self,
        *,
        session_id: str,
        turn_id: str,
        line_items: list[Mapping[str, Any]],
    ) -> int:
        """Persist immutable facts for each real provider request."""

        inserted = 0
        with self._transaction() as connection:
            if connection.execute(
                "SELECT 1 FROM her_sessions WHERE session_id = ?", (session_id,)
            ).fetchone() is None:
                raise HerSessionStoreError("unknown_session", "HER session is unknown.")
            for index, raw in enumerate(line_items, start=1):
                item = dict(raw)
                provider_request_id = str(
                    item.get("provider_request_id")
                    or item.get("request_id")
                    or f"{turn_id}:provider:{index}"
                )
                cache_hit = item.get("prompt_cache_hit_tokens")
                cache_miss = item.get("prompt_cache_miss_tokens")
                latency = item.get("provider_call_latency_ms")
                facts = {
                    "provider_request_id": provider_request_id,
                    "session_id": str(session_id),
                    "turn_id": str(turn_id or ""),
                    "parent_request_id": str(item.get("parent_request_id") or ""),
                    "phase": str(item.get("phase") or ""),
                    "provider": str(item.get("engine") or item.get("provider") or ""),
                    "model": str(item.get("model") or ""),
                    "input_tokens": max(
                        0, int(item.get("input") or item.get("input_tokens") or 0)
                    ),
                    "output_tokens": max(
                        0, int(item.get("output") or item.get("output_tokens") or 0)
                    ),
                    "thinking_tokens": max(
                        0,
                        int(item.get("thinking") or item.get("thinking_tokens") or 0),
                    ),
                    "prompt_cache_hit_tokens": (
                        None if cache_hit is None else max(0, int(cache_hit))
                    ),
                    "prompt_cache_miss_tokens": (
                        None if cache_miss is None else max(0, int(cache_miss))
                    ),
                    "token_source": str(item.get("token_source") or "estimated"),
                    "provider_call_latency_ms": (
                        None if latency is None else max(0.0, float(latency))
                    ),
                    "attempt": max(1, int(item.get("attempt") or 1)),
                    "retry_count": max(0, int(item.get("retry_count") or 0)),
                    "recovery_kind": str(item.get("recovery_kind") or "none"),
                    "compact": 1 if bool(item.get("compact")) else 0,
                    "routing_revision": max(
                        0, int(item.get("routing_revision") or 0)
                    ),
                    "capability_revision": max(
                        0, int(item.get("capability_revision") or 0)
                    ),
                    "pricing_revision": str(
                        item.get("pricing_revision") or "unknown"
                    ),
                    "status": str(item.get("status") or "completed"),
                }
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO her_provider_requests(
                        provider_request_id, session_id, turn_id, parent_request_id,
                        phase, provider, model, input_tokens, output_tokens,
                        thinking_tokens, prompt_cache_hit_tokens,
                        prompt_cache_miss_tokens, token_source, cost_usd,
                        cost_source, provider_call_latency_ms, attempt, retry_count,
                        recovery_kind, compact, routing_revision,
                        capability_revision, pricing_revision, status, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        facts["provider_request_id"],
                        facts["session_id"],
                        facts["turn_id"],
                        facts["parent_request_id"],
                        facts["phase"],
                        facts["provider"],
                        facts["model"],
                        facts["input_tokens"],
                        facts["output_tokens"],
                        facts["thinking_tokens"],
                        facts["prompt_cache_hit_tokens"],
                        facts["prompt_cache_miss_tokens"],
                        facts["token_source"],
                        None,
                        "separate_valuation",
                        facts["provider_call_latency_ms"],
                        facts["attempt"],
                        facts["retry_count"],
                        facts["recovery_kind"],
                        facts["compact"],
                        facts["routing_revision"],
                        facts["capability_revision"],
                        facts["pricing_revision"],
                        facts["status"],
                        _utc_now(),
                    ),
                )
                if not cursor.rowcount:
                    existing = connection.execute(
                        """
                        SELECT provider_request_id, session_id, turn_id,
                               parent_request_id, phase, provider, model,
                               input_tokens, output_tokens, thinking_tokens,
                               prompt_cache_hit_tokens, prompt_cache_miss_tokens,
                               token_source, provider_call_latency_ms, attempt,
                               retry_count, recovery_kind, compact,
                               routing_revision, capability_revision,
                               pricing_revision, status
                        FROM her_provider_requests
                        WHERE provider_request_id = ?
                        """,
                        (provider_request_id,),
                    ).fetchone()
                    stored = dict(existing) if existing is not None else {}
                    if stored != facts:
                        raise HerSessionStoreError(
                            "provider_request_conflict",
                            "Provider request ID was reused with different immutable facts.",
                        )

                cost_usd = item.get("cost_usd")
                cost_usd = None if cost_usd is None else float(cost_usd)
                cost_source = str(item.get("cost_source") or "unknown")
                connection.execute(
                    """
                    INSERT OR IGNORE INTO her_provider_request_valuations(
                        provider_request_id, pricing_revision, cost_usd,
                        cost_source, created_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        provider_request_id,
                        facts["pricing_revision"],
                        cost_usd,
                        cost_source,
                        _utc_now(),
                    ),
                )
                valuation = connection.execute(
                    """
                    SELECT cost_usd, cost_source
                    FROM her_provider_request_valuations
                    WHERE provider_request_id = ? AND pricing_revision = ?
                    """,
                    (provider_request_id, facts["pricing_revision"]),
                ).fetchone()
                if valuation is None or (
                    valuation["cost_usd"] != cost_usd
                    or str(valuation["cost_source"]) != cost_source
                ):
                    raise HerSessionStoreError(
                        "provider_valuation_conflict",
                        "Provider request valuation conflicts within one price revision.",
                    )
                if cursor.rowcount:
                    inserted += 1
                    self._next_event(
                        connection,
                        session_id=session_id,
                        event_id=f"provider-usage:{provider_request_id}",
                        kind="provider_request_accounted",
                        payload={
                            "turn_id": turn_id,
                            "provider_request_id": provider_request_id,
                            "provider": facts["provider"],
                            "model": facts["model"],
                            "phase": facts["phase"],
                            "compact": bool(facts["compact"]),
                            "pricing_revision": facts["pricing_revision"],
                        },
                    )
        return inserted

    def usage_summary(
        self, session_id: str, *, turn_id: str | None = None
    ) -> dict[str, Any]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT requests.*,
                       valuations.cost_usd AS valuation_cost_usd,
                       valuations.cost_source AS valuation_cost_source,
                       valuations.pricing_revision AS valuation_pricing_revision
                FROM her_provider_requests AS requests
                LEFT JOIN her_provider_request_valuations AS valuations
                  ON valuations.provider_request_id = requests.provider_request_id
                 AND valuations.pricing_revision = requests.pricing_revision
                WHERE requests.session_id = ?
                  AND (? = '' OR requests.turn_id = ?)
                ORDER BY requests.created_at, requests.provider_request_id
                """,
                (str(session_id), str(turn_id or ""), str(turn_id or "")),
            ).fetchall()
        items = [dict(row) for row in rows]

        def aggregate(values: list[dict[str, Any]]) -> dict[str, Any]:
            costs = [item.get("valuation_cost_usd") for item in values]
            pricing_revisions = sorted(
                {
                    str(item.get("valuation_pricing_revision") or "unknown")
                    for item in values
                }
            )
            return {
                "provider_requests": len(values),
                "input_tokens": sum(int(item.get("input_tokens") or 0) for item in values),
                "output_tokens": sum(int(item.get("output_tokens") or 0) for item in values),
                "thinking_tokens": sum(int(item.get("thinking_tokens") or 0) for item in values),
                "prompt_cache_hit_tokens": sum(int(item.get("prompt_cache_hit_tokens") or 0) for item in values),
                "prompt_cache_miss_tokens": sum(int(item.get("prompt_cache_miss_tokens") or 0) for item in values),
                "cost_usd": (
                    round(sum(float(value) for value in costs), 6)
                    if costs and all(value is not None for value in costs)
                    else None
                ),
                "retry_count": sum(int(item.get("retry_count") or 0) for item in values),
                "compact_requests": sum(1 for item in values if item.get("compact")),
                "pricing_revisions": pricing_revisions,
            }

        grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
        by_turn: dict[str, list[dict[str, Any]]] = {}
        for item in items:
            grouped.setdefault(
                (str(item.get("provider") or ""), str(item.get("model") or "")), []
            ).append(item)
            by_turn.setdefault(str(item.get("turn_id") or "maintenance"), []).append(item)
        return {
            "session_id": str(session_id),
            "turn_id": str(turn_id or ""),
            "total": aggregate(items),
            "providers": [
                {"provider": provider, "model": model, **aggregate(values)}
                for (provider, model), values in sorted(grouped.items())
            ],
            "turns": [
                {"turn_id": key, **aggregate(values)}
                for key, values in sorted(by_turn.items())
            ],
        }

    def compare_wip_shadow(
        self,
        *,
        session_id: str,
        turn_id: str,
        wip_event_ids: list[str],
    ) -> dict[str, Any]:
        with self._transaction() as connection:
            rows = connection.execute(
                """
                SELECT payload_json FROM her_session_events
                WHERE session_id = ? AND kind LIKE 'runtime_%'
                """,
                (session_id,),
            ).fetchall()
            canonical_ids = set()
            for row in rows:
                payload = _object(row["payload_json"])
                if str(payload.get("turn_id") or "") != turn_id:
                    continue
                # WIP deliberately omits request_received to prevent recursive
                # prompt growth, so parity compares only its declared shadow
                # projection surface.
                if str(payload.get("event") or "") == "request_received":
                    continue
                if str(payload.get("stage") or "") == "wip_journal":
                    continue
                canonical_ids.add(str(payload.get("external_event_id") or ""))
            canonical_ids.discard("")
            shadow_ids = {str(value) for value in wip_event_ids if str(value)}
            missing = sorted(canonical_ids - shadow_ids)
            extra = sorted(shadow_ids - canonical_ids)
            result = {
                "turn_id": turn_id,
                "canonical_event_count": len(canonical_ids),
                "wip_event_count": len(shadow_ids),
                "missing_from_wip_count": len(missing),
                "missing_from_wip": missing[:24],
                "extra_in_wip_count": len(extra),
                "extra_in_wip": extra[:24],
                "parity": not missing and not extra,
                "authority_phase": "canonical_primary_wip_shadow",
            }
            self._next_event(
                connection,
                session_id=session_id,
                event_id=f"{turn_id}:wip-shadow-compared",
                kind="wip_shadow_compared",
                payload=result,
            )
            return result

    def _commit_settled_checkpoint(
        self,
        connection: sqlite3.Connection,
        *,
        session_id: str,
        turn_id: str,
    ) -> dict[str, Any]:
        session = connection.execute(
            "SELECT * FROM her_sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        turn = connection.execute(
            "SELECT * FROM her_turns WHERE session_id = ? AND turn_id = ?",
            (session_id, turn_id),
        ).fetchone()
        recovery = connection.execute(
            """
            SELECT * FROM her_active_turn_recovery
            WHERE session_id = ? AND turn_id = ?
            """,
            (session_id, turn_id),
        ).fetchone()
        if session is None or turn is None:
            raise HerSessionStoreError(
                "unknown_turn", "Cannot checkpoint an unknown HER turn."
            )
        recent = connection.execute(
            """
            SELECT sequence, turn_id, user_message, assistant_text,
                   routing_revision, status
            FROM her_turns
            WHERE session_id = ? AND status = 'completed'
            ORDER BY sequence DESC LIMIT 8
            """,
            (session_id,),
        ).fetchall()
        recovery_state = self._recovery_dict(recovery) or {}
        payload = {
            "format": "her-settled-session-checkpoint-v1",
            "session_id": session_id,
            "settled_through_turn_id": turn_id,
            "settled_through_turn_sequence": int(turn["sequence"]),
            "state_version": int(session["state_version"]),
            "pcm_revision": int(session["pcm_revision"]),
            "resource_revision": int(session["resource_revision"]),
            "pcm_digest": str(session["pcm_digest"]),
            "resource_digest": str(session["resource_digest"]),
            "routing_revision": int(turn["routing_revision"] or 0),
            "capability_revision": int(turn["capability_revision"] or 0),
            "pricing_revision": str(turn["pricing_revision"] or "unknown"),
            "route_snapshot": _object(turn["route_snapshot_json"]),
            "provider_context_generation": int(
                session["provider_context_generation"] or 0
            ),
            "strategy": recovery_state.get("strategy") or {},
            "plan": recovery_state.get("plan") or {},
            "tool_receipts": recovery_state.get("tool_receipts") or [],
            "side_effects": recovery_state.get("side_effects") or [],
            "remaining_work": {},
            "recent_settled_exchanges": [
                {
                    "sequence": int(item["sequence"]),
                    "turn_id": str(item["turn_id"]),
                    "user": str(item["user_message"] or "")[-12000:],
                    "assistant": str(item["assistant_text"] or "")[-12000:],
                    "routing_revision": int(item["routing_revision"] or 0),
                }
                for item in reversed(recent)
            ],
            "limitations": [
                "Contains settled, externally observable work only.",
                "Provider hidden reasoning and process-local SDK state are absent.",
                "Tool outputs are represented by durable evidence receipts and hashes.",
            ],
        }
        digest = _digest(payload)
        now = _utc_now()
        existing = connection.execute(
            "SELECT created_at FROM her_settled_checkpoints WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        created_at = str(existing["created_at"]) if existing is not None else now
        connection.execute(
            """
            INSERT INTO her_settled_checkpoints(
                session_id, checkpoint_version, settled_through_turn_id,
                settled_through_turn_sequence, routing_revision,
                provider_context_generation, payload_json, payload_digest,
                created_at, updated_at
            ) VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
                checkpoint_version = excluded.checkpoint_version,
                settled_through_turn_id = excluded.settled_through_turn_id,
                settled_through_turn_sequence = excluded.settled_through_turn_sequence,
                routing_revision = excluded.routing_revision,
                provider_context_generation = excluded.provider_context_generation,
                payload_json = excluded.payload_json,
                payload_digest = excluded.payload_digest,
                updated_at = excluded.updated_at
            """,
            (
                session_id,
                turn_id,
                int(turn["sequence"]),
                int(turn["routing_revision"] or 0),
                int(session["provider_context_generation"] or 0),
                _json(payload),
                digest,
                created_at,
                now,
            ),
        )
        self._next_event(
            connection,
            session_id=session_id,
            event_id=f"{turn_id}:settled-checkpoint",
            kind="settled_checkpoint_committed",
            payload={
                "turn_id": turn_id,
                "payload_digest": digest,
                "routing_revision": int(turn["routing_revision"] or 0),
            },
        )
        return payload

    @staticmethod
    def _apply_pcm_operations(
        current: Mapping[str, Any], operations: list[Mapping[str, Any]]
    ) -> dict[str, Any]:
        updated = {str(key): dict(value) for key, value in current.items()}
        for operation in operations:
            kind = str(operation.get("op") or "")
            key = str(operation.get("key") or "")
            if not key or kind not in {"upsert", "remove"}:
                raise HerSessionStoreError(
                    "invalid_pcm_delta", "PCM delta contains an invalid operation."
                )
            if kind == "remove":
                updated.pop(key, None)
                continue
            section = operation.get("section")
            if not isinstance(section, Mapping) or str(section.get("key") or "") != key:
                raise HerSessionStoreError(
                    "invalid_pcm_delta",
                    "PCM upsert must contain its exact keyed section.",
                )
            updated[key] = dict(section)
        return updated

    def append_turn(
        self,
        *,
        session_id: str,
        instance_id: str,
        agent_id: str,
        owner_id: str,
        hashi_conversation_id: str,
        context_generation: int,
        workzone_identity: str,
        epoch: int,
        expected_state_version: int,
        expected_canonical_sequence: int,
        pcm_base_revision: int,
        pcm_target_revision: int,
        pcm_operations: list[Mapping[str, Any]],
        pcm_target_digest: str,
        resource_base_revision: int,
        resource_target_revision: int,
        resource_additions: list[Mapping[str, Any]],
        resource_revocations: list[str],
        resource_target_digest: str,
        turn_id: str,
        request_id: str,
        message_id: str,
        idempotency_key: str,
        user_message: str,
    ) -> dict[str, Any]:
        now = _utc_now()
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM her_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if row is None:
                raise HerSessionStoreError("unknown_session", "HER session is unknown.")
            self._assert_binding(
                row,
                instance_id=instance_id,
                agent_id=agent_id,
                owner_id=owner_id,
                hashi_conversation_id=hashi_conversation_id,
                context_generation=context_generation,
                workzone_identity=workzone_identity,
            )
            duplicate = connection.execute(
                """
                SELECT * FROM her_turns
                WHERE session_id = ? AND idempotency_key = ?
                """,
                (session_id, idempotency_key),
            ).fetchone()
            if duplicate is not None:
                if (
                    str(duplicate["message_id"]) != message_id
                    or str(duplicate["user_message"]) != user_message
                ):
                    raise HerSessionStoreError(
                        "duplicate_message_conflict",
                        "Idempotency key was already accepted with different content.",
                    )
                return {
                    "session": self._session_dict(row),
                    "turn": self._turn_dict(duplicate),
                    "duplicate": True,
                }
            if str(row["status"]) != "open":
                raise HerSessionStoreError(
                    "session_closed", "HER session is not open for new turns."
                )
            if int(row["epoch"]) != int(epoch):
                raise HerSessionStoreError(
                    "stale_session_epoch", "HER session epoch is stale."
                )
            if int(row["state_version"]) != int(expected_state_version):
                raise HerSessionStoreError(
                    "state_version_conflict", "HER session state version changed."
                )
            if int(row["canonical_sequence"]) != int(expected_canonical_sequence):
                raise HerSessionStoreError(
                    "sequence_gap", "HER canonical session sequence changed."
                )
            if int(row["pcm_revision"]) != int(pcm_base_revision):
                raise HerSessionStoreError(
                    "pcm_revision_conflict", "HER PCM base revision changed."
                )
            if int(pcm_target_revision) != int(pcm_base_revision) + 1:
                raise HerSessionStoreError(
                    "pcm_revision_conflict",
                    "HER PCM target revision must advance once.",
                )
            if int(row["resource_revision"]) != int(resource_base_revision):
                raise HerSessionStoreError(
                    "resource_revision_conflict", "HER resource base revision changed."
                )
            if int(resource_target_revision) != int(resource_base_revision) + 1:
                raise HerSessionStoreError(
                    "resource_revision_conflict",
                    "HER resource target revision must advance once.",
                )
            active = connection.execute(
                """
                SELECT turn_id FROM her_turns
                WHERE session_id = ? AND status = 'active' LIMIT 1
                """,
                (session_id,),
            ).fetchone()
            if active is not None:
                raise HerSessionStoreError(
                    "turn_already_active", "HER session already has an active turn."
                )

            pcm = self._apply_pcm_operations(_object(row["pcm_json"]), pcm_operations)
            pcm_digest = _digest(pcm)
            if str(pcm_target_digest or "") != pcm_digest:
                raise HerSessionStoreError(
                    "pcm_digest_conflict",
                    "HER PCM target digest does not match its delta.",
                )
            current_resources = _object(row["resources_json"])
            resource_map = _resource_map(
                list(current_resources.get("attachments") or [])
            )
            additions = _resource_map(resource_additions)
            revocations = {str(item) for item in resource_revocations}
            if revocations - set(resource_map):
                raise HerSessionStoreError(
                    "invalid_resource_delta",
                    "Resource delta revokes an attachment that is not materialised.",
                )
            if revocations & set(additions):
                raise HerSessionStoreError(
                    "invalid_resource_delta",
                    "Resource delta cannot add and revoke the same attachment.",
                )
            resource_map.update(additions)
            for key in revocations:
                resource_map.pop(key, None)
            attachments = [resource_map[key] for key in sorted(resource_map)]
            if str(resource_target_digest or "") != _digest(resource_map):
                raise HerSessionStoreError(
                    "resource_digest_conflict",
                    "HER resource digest does not match its attachment state.",
                )
            resources = {
                "attachments": attachments,
                "digest": str(resource_target_digest),
            }
            authority_digest = _digest(
                {
                    "pcm_digest": pcm_digest,
                    "resource_digest": str(resource_target_digest),
                }
            )
            next_turn_row = connection.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 AS value FROM her_turns WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            turn_sequence = int(next_turn_row["value"])
            state_version = int(row["state_version"]) + 1
            connection.execute(
                """
                INSERT INTO her_turns(
                    session_id, turn_id, request_id, message_id,
                    idempotency_key, sequence, pcm_revision, resource_revision,
                    authority_digest, user_message, status, accepted_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?)
                """,
                (
                    session_id,
                    turn_id,
                    request_id,
                    message_id,
                    idempotency_key,
                    turn_sequence,
                    int(pcm_target_revision),
                    int(resource_target_revision),
                    authority_digest,
                    user_message,
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO her_active_turn_recovery(
                    session_id, turn_id, status, current_stage,
                    remaining_work_json, safe_to_resume, created_at, updated_at
                ) VALUES (?, ?, 'active', 'initial', ?, 1, ?, ?)
                """,
                (
                    session_id,
                    turn_id,
                    _json({"goal": user_message, "status": "accepted"}),
                    now,
                    now,
                ),
            )
            connection.execute(
                """
                UPDATE her_sessions
                SET state_version = ?, pcm_revision = ?, resource_revision = ?,
                    pcm_digest = ?, resource_digest = ?, pcm_json = ?,
                    resources_json = ?, last_turn_id = ?, updated_at = ?
                WHERE session_id = ?
                """,
                (
                    state_version,
                    int(pcm_target_revision),
                    int(resource_target_revision),
                    pcm_digest,
                    str(resource_target_digest),
                    _json(pcm),
                    _json(dict(resources)),
                    turn_id,
                    now,
                    session_id,
                ),
            )
            sequence = self._next_event(
                connection,
                session_id=session_id,
                event_id=f"{turn_id}:accepted",
                kind="turn_accepted",
                payload={
                    "turn_id": turn_id,
                    "message_id": message_id,
                    "state_version": state_version,
                    "pcm_revision": int(pcm_target_revision),
                    "resource_revision": int(resource_target_revision),
                    "pcm_digest": pcm_digest,
                    "resource_digest": str(resource_target_digest),
                    "pcm_operation_count": len(pcm_operations),
                    "resource_addition_count": len(additions),
                    "resource_revocation_count": len(revocations),
                },
            )
            session_row = connection.execute(
                "SELECT * FROM her_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            turn_row = connection.execute(
                "SELECT * FROM her_turns WHERE session_id = ? AND turn_id = ?",
                (session_id, turn_id),
            ).fetchone()
            return {
                "session": self._session_dict(session_row),
                "turn": self._turn_dict(turn_row),
                "duplicate": False,
                "canonical_sequence": sequence,
            }

    def complete_turn(
        self,
        *,
        session_id: str,
        turn_id: str,
        assistant_text: str,
        error_text: str = "",
    ) -> dict[str, Any]:
        now = _utc_now()
        with self._transaction() as connection:
            turn = connection.execute(
                "SELECT * FROM her_turns WHERE session_id = ? AND turn_id = ?",
                (session_id, turn_id),
            ).fetchone()
            if turn is None:
                raise HerSessionStoreError("unknown_turn", "HER turn is unknown.")
            if str(turn["status"]) in {"completed", "failed", "cancelled"}:
                return self._turn_dict(turn) or {}
            recovery = connection.execute(
                """
                SELECT * FROM her_active_turn_recovery
                WHERE session_id = ? AND turn_id = ?
                """,
                (session_id, turn_id),
            ).fetchone()
            effects = _array(recovery["side_effects_json"]) if recovery else []
            unresolved = self._unresolved_side_effects(effects)
            current_unresolved = [
                item
                for item in unresolved
                if not str(item.get("inherited_from_turn_id") or "")
            ]
            effective_error = str(error_text or "")
            if current_unresolved and not effective_error:
                effective_error = (
                    "HER turn reported completion while non-read-only tool side "
                    "effects remained unresolved."
                )
            if not effective_error:
                for effect in effects:
                    if (
                        effect.get("state") in {"pending", "unknown"}
                        and str(effect.get("inherited_from_turn_id") or "")
                    ):
                        effect["state"] = "reconciled"
                        effect["reconciled_by_turn_id"] = turn_id
                unresolved = self._unresolved_side_effects(effects)
            status = "failed" if effective_error else "completed"
            remaining_work = _object(
                recovery["remaining_work_json"] if recovery is not None else "{}"
            )
            remaining_work.update(
                {
                    "status": "failed" if effective_error else "completed",
                    "error": effective_error[:2000],
                    "unresolved_side_effects": unresolved,
                }
            )
            connection.execute(
                """
                UPDATE her_turns
                SET assistant_text = ?, error_text = ?, status = ?, completed_at = ?
                WHERE session_id = ? AND turn_id = ?
                """,
                (
                    assistant_text,
                    effective_error or None,
                    status,
                    now,
                    session_id,
                    turn_id,
                ),
            )
            row = connection.execute(
                "SELECT state_version FROM her_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            state_version = int(row["state_version"]) + 1
            connection.execute(
                "UPDATE her_sessions SET state_version = ?, updated_at = ? WHERE session_id = ?",
                (state_version, now, session_id),
            )
            self._next_event(
                connection,
                session_id=session_id,
                event_id=f"{turn_id}:{status}",
                kind=f"turn_{status}",
                payload={
                    "turn_id": turn_id,
                    "state_version": state_version,
                    "has_error": bool(effective_error),
                    "unresolved_side_effect_count": len(unresolved),
                },
            )
            recovery_status = "terminated" if effective_error else "settled"
            disposition = (
                "UNKNOWN_SIDE_EFFECT"
                if unresolved
                else ("terminal_error" if effective_error else "settled")
            )
            connection.execute(
                """
                UPDATE her_active_turn_recovery
                SET status = ?, side_effects_json = ?, remaining_work_json = ?,
                    safe_to_resume = ?, recovery_disposition = ?, updated_at = ?
                WHERE session_id = ? AND turn_id = ?
                """,
                (
                    recovery_status,
                    _json(self._bounded_side_effects(effects)),
                    _json(remaining_work),
                    0 if unresolved else 1,
                    disposition,
                    now,
                    session_id,
                    turn_id,
                ),
            )
            if not effective_error:
                self._commit_settled_checkpoint(
                    connection, session_id=session_id, turn_id=turn_id
                )
                self._archive_recovery_ancestors(
                    connection,
                    session_id=session_id,
                    turn_id=turn_id,
                    now=now,
                )
            updated = connection.execute(
                "SELECT * FROM her_turns WHERE session_id = ? AND turn_id = ?",
                (session_id, turn_id),
            ).fetchone()
            return self._turn_dict(updated) or {}

    def cancel_turn(
        self,
        *,
        session_id: str,
        turn_id: str,
        reason: str,
    ) -> dict[str, Any]:
        """Cancel one active turn without closing its durable session."""

        now = _utc_now()
        with self._transaction() as connection:
            turn = connection.execute(
                "SELECT * FROM her_turns WHERE session_id = ? AND turn_id = ?",
                (session_id, turn_id),
            ).fetchone()
            if turn is None:
                raise HerSessionStoreError("unknown_turn", "HER turn is unknown.")
            if str(turn["status"]) in {"completed", "failed", "cancelled"}:
                return self._turn_dict(turn) or {}
            clean_reason = str(reason or "HER turn was cancelled.")
            recovery = connection.execute(
                """
                SELECT * FROM her_active_turn_recovery
                WHERE session_id = ? AND turn_id = ?
                """,
                (session_id, turn_id),
            ).fetchone()
            effects = _array(recovery["side_effects_json"]) if recovery else []
            unresolved = self._unresolved_side_effects(effects)
            remaining_work = _object(
                recovery["remaining_work_json"] if recovery is not None else "{}"
            )
            remaining_work.update(
                {
                    "status": "cancelled",
                    "reason": clean_reason[:2000],
                    "unresolved_side_effects": unresolved,
                }
            )
            connection.execute(
                """
                UPDATE her_turns
                SET assistant_text = '', error_text = ?, status = 'cancelled',
                    completed_at = ?
                WHERE session_id = ? AND turn_id = ?
                """,
                (clean_reason, now, session_id, turn_id),
            )
            row = connection.execute(
                "SELECT state_version FROM her_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            state_version = int(row["state_version"]) + 1
            connection.execute(
                "UPDATE her_sessions SET state_version = ?, updated_at = ? WHERE session_id = ?",
                (state_version, now, session_id),
            )
            self._next_event(
                connection,
                session_id=session_id,
                event_id=f"{turn_id}:cancelled",
                kind="turn_cancelled",
                payload={
                    "turn_id": turn_id,
                    "state_version": state_version,
                    "reason": clean_reason,
                    "unresolved_side_effect_count": len(unresolved),
                },
            )
            disposition = "UNKNOWN_SIDE_EFFECT" if unresolved else "cancelled"
            connection.execute(
                """
                UPDATE her_active_turn_recovery
                SET status = 'terminated', remaining_work_json = ?,
                    safe_to_resume = ?, recovery_disposition = ?, updated_at = ?
                WHERE session_id = ? AND turn_id = ?
                """,
                (
                    _json(remaining_work),
                    0 if unresolved else 1,
                    disposition,
                    now,
                    session_id,
                    turn_id,
                ),
            )
            if not unresolved:
                self._archive_recovery_ancestors(
                    connection,
                    session_id=session_id,
                    turn_id=turn_id,
                    now=now,
                )
            updated = connection.execute(
                "SELECT * FROM her_turns WHERE session_id = ? AND turn_id = ?",
                (session_id, turn_id),
            ).fetchone()
            return self._turn_dict(updated) or {}

    def reconcile_interrupted(self) -> int:
        """Fail unfinished process-local turns while preserving their sessions."""

        with self._transaction() as connection:
            rows = connection.execute(
                "SELECT session_id, turn_id FROM her_turns WHERE status = 'active'"
            ).fetchall()
            for row in rows:
                session_id = str(row["session_id"])
                turn_id = str(row["turn_id"])
                now = _utc_now()
                recovery = connection.execute(
                    """
                    SELECT * FROM her_active_turn_recovery
                    WHERE session_id = ? AND turn_id = ?
                    """,
                    (session_id, turn_id),
                ).fetchone()
                if recovery is None:
                    # A pre-control-plane active Turn has no canonical tool
                    # boundary.  Treat that absence as unknown rather than
                    # falsely asserting that replay is safe; the WIP shadow
                    # remains available for operator investigation.
                    legacy_unknown = {
                        "operation_id": f"legacy-precanonical:{turn_id}",
                        "tool_call_id": "",
                        "tool_name": "legacy_wip_recovery_boundary",
                        "arguments_sha256": "",
                        "state": "unknown",
                        "reason": "active Turn predates canonical recovery projection",
                    }
                    connection.execute(
                        """
                        INSERT INTO her_active_turn_recovery(
                            session_id, turn_id, status, current_stage,
                            side_effects_json, remaining_work_json,
                            safe_to_resume, created_at, updated_at
                        ) VALUES (?, ?, 'active', 'legacy_unknown', ?, ?, 0, ?, ?)
                        """,
                        (
                            session_id,
                            turn_id,
                            _json([legacy_unknown]),
                            _json(
                                {
                                    "status": "legacy_active_turn",
                                    "requires_wip_investigation": True,
                                }
                            ),
                            now,
                            now,
                        ),
                    )
                    recovery = connection.execute(
                        """
                        SELECT * FROM her_active_turn_recovery
                        WHERE session_id = ? AND turn_id = ?
                        """,
                        (session_id, turn_id),
                    ).fetchone()
                effects = _array(recovery["side_effects_json"]) if recovery else []
                unresolved = self._unresolved_side_effects(effects)
                disposition = (
                    "UNKNOWN_SIDE_EFFECT"
                    if unresolved
                    else "FAILED_SAFE_REPLAY_REQUIRED"
                )
                reason = (
                    "HER process replacement interrupted the active turn with "
                    "an unresolved non-idempotent tool intent; side effects are unknown."
                    if unresolved
                    else "HER process replacement interrupted the active turn; the "
                    "canonical recovery state was retained for truthful replay."
                )
                connection.execute(
                    """
                    UPDATE her_turns
                    SET status = 'failed', error_text = ?, completed_at = ?
                    WHERE session_id = ? AND turn_id = ?
                    """,
                    (
                        reason,
                        now,
                        session_id,
                        turn_id,
                    ),
                )
                current = connection.execute(
                    "SELECT state_version FROM her_sessions WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
                state_version = int(current["state_version"]) + 1
                connection.execute(
                    "UPDATE her_sessions SET state_version = ?, updated_at = ? WHERE session_id = ?",
                    (state_version, now, session_id),
                )
                self._next_event(
                    connection,
                    session_id=session_id,
                    event_id=f"{turn_id}:process-reconciled",
                    kind="turn_recovery_failed_safe",
                    payload={
                        "turn_id": turn_id,
                        "state_version": state_version,
                        "recovery_disposition": disposition,
                        "unresolved_side_effect_count": len(unresolved),
                        "session_retained": True,
                    },
                )
                connection.execute(
                    """
                    UPDATE her_active_turn_recovery
                    SET status = 'terminated', safe_to_resume = ?,
                        recovery_disposition = ?, remaining_work_json = ?, updated_at = ?
                    WHERE session_id = ? AND turn_id = ?
                    """,
                    (
                        0 if unresolved else 1,
                        disposition,
                        _json(
                            {
                                "status": "interrupted",
                                "reason": reason,
                                "unresolved_side_effects": unresolved,
                            }
                        ),
                        now,
                        session_id,
                        turn_id,
                    ),
                )
            return len(rows)

    def close_session(self, session_id: str) -> bool:
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT status FROM her_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if row is None:
                return False
            if str(row["status"]) == "closed":
                return True
            now = _utc_now()
            connection.execute(
                "UPDATE her_sessions SET status = 'closed', updated_at = ? WHERE session_id = ?",
                (now, session_id),
            )
            self._next_event(
                connection,
                session_id=session_id,
                event_id=f"{session_id}:closed",
                kind="session_closed",
                payload={},
            )
            return True
