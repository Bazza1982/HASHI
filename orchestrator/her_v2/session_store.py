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
                """
            )
            migrations = {
                "her_sessions": {
                    "schema_version": "INTEGER NOT NULL DEFAULT 1",
                    "instance_id": "TEXT NOT NULL DEFAULT ''",
                    "owner_id": "TEXT NOT NULL DEFAULT ''",
                    "pcm_digest": "TEXT NOT NULL DEFAULT ''",
                    "resource_digest": "TEXT NOT NULL DEFAULT ''",
                },
                "her_turns": {
                    "pcm_revision": "INTEGER NOT NULL DEFAULT 0",
                    "resource_revision": "INTEGER NOT NULL DEFAULT 0",
                    "authority_digest": "TEXT NOT NULL DEFAULT ''",
                },
                "her_session_events": {
                    "state_version": "INTEGER NOT NULL DEFAULT 0",
                    "payload_digest": "TEXT NOT NULL DEFAULT ''",
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

    @staticmethod
    def _session_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        result = dict(row)
        result["pcm"] = _object(result.pop("pcm_json", "{}"))
        result["resources"] = _object(result.pop("resources_json", "{}"))
        return result

    @staticmethod
    def _turn_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return dict(row) if row is not None else None

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
        status = "failed" if error_text else "completed"
        with self._transaction() as connection:
            turn = connection.execute(
                "SELECT * FROM her_turns WHERE session_id = ? AND turn_id = ?",
                (session_id, turn_id),
            ).fetchone()
            if turn is None:
                raise HerSessionStoreError("unknown_turn", "HER turn is unknown.")
            if str(turn["status"]) in {"completed", "failed", "cancelled"}:
                return self._turn_dict(turn) or {}
            connection.execute(
                """
                UPDATE her_turns
                SET assistant_text = ?, error_text = ?, status = ?, completed_at = ?
                WHERE session_id = ? AND turn_id = ?
                """,
                (assistant_text, error_text or None, status, now, session_id, turn_id),
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
                    "has_error": bool(error_text),
                },
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
                },
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
                connection.execute(
                    """
                    UPDATE her_turns
                    SET status = 'failed', error_text = ?, completed_at = ?
                    WHERE session_id = ? AND turn_id = ?
                    """,
                    (
                        "HER process replacement interrupted the active turn; the session was retained.",
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
                    payload={"turn_id": turn_id, "state_version": state_version},
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
