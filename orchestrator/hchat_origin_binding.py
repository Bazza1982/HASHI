"""Persist the local conversation that initiated one HChat message.

The binding is produced by the active Run, never by an incoming peer. It is
operational routing data in the existing Session database, not an additional
user authorization. The additive table is safe for older readers to ignore.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any


class HChatOriginConflict(ValueError):
    """The source Run or its previously recorded binding is no longer current."""


def _ensure_schema(connection: Any) -> None:
    connection.execute(
        """CREATE TABLE IF NOT EXISTS hchat_outbound_origins (
               message_id TEXT PRIMARY KEY,
               instance_id TEXT NOT NULL,
               owner_id TEXT NOT NULL,
               agent_id TEXT NOT NULL,
               session_id TEXT NOT NULL,
               run_id TEXT NOT NULL,
               request_id TEXT NOT NULL,
               context_generation INTEGER NOT NULL,
               target TEXT NOT NULL,
               originated_at TEXT NOT NULL,
               FOREIGN KEY(session_id) REFERENCES sessions(session_id) ON DELETE CASCADE,
               FOREIGN KEY(run_id) REFERENCES runs(run_id)
           )"""
    )


def bind_outbound_origin(
    store: Any, *, message_id: str, owner_id: str, agent_id: str,
    session_id: str, run_id: str, request_id: str, context_generation: int,
    fencing_token: int, target: str,
) -> dict[str, Any]:
    """Bind before sending, after proving the exact current local Run."""
    identity = {
        "message_id": str(message_id).strip(),
        "instance_id": str(store.instance_id).strip().upper(),
        "owner_id": str(owner_id).strip(),
        "agent_id": str(agent_id).strip().casefold(),
        "session_id": str(session_id).strip(),
        "run_id": str(run_id).strip(),
        "request_id": str(request_id).strip(),
        "context_generation": int(context_generation),
        "target": str(target).strip().casefold(),
    }
    if any(value in ("", None) for value in identity.values()) or identity["context_generation"] < 1:
        raise HChatOriginConflict("HChat source Run identity is incomplete")
    with store._lock, store._connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        _ensure_schema(connection)
        run = connection.execute(
            """SELECT r.run_id, r.request_id, r.context_generation, r.fencing_token,
                      r.state, r.agent_id AS run_agent, s.instance_id, s.owner_id,
                      s.agent_id, s.session_id, s.status, s.context_generation AS session_generation
               FROM runs r JOIN sessions s ON s.session_id = r.session_id
               WHERE r.run_id=? AND s.instance_id=?""",
            (identity["run_id"], identity["instance_id"]),
        ).fetchone()
        if run is None or any((
            run["request_id"] != identity["request_id"],
            run["owner_id"] != identity["owner_id"],
            run["agent_id"] != identity["agent_id"],
            run["run_agent"] != identity["agent_id"],
            run["session_id"] != identity["session_id"],
            run["context_generation"] != identity["context_generation"],
            run["session_generation"] != identity["context_generation"],
            run["fencing_token"] != int(fencing_token),
            run["state"] != "running",
            run["status"] != "active",
        )):
            raise HChatOriginConflict("HChat source Run is no longer current")
        existing = connection.execute(
            "SELECT * FROM hchat_outbound_origins WHERE message_id=?", (identity["message_id"],)
        ).fetchone()
        if existing is not None:
            if any(existing[key] != value for key, value in identity.items()):
                raise HChatOriginConflict("HChat message is already bound to another origin")
            return dict(existing)
        originated_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        connection.execute(
            """INSERT INTO hchat_outbound_origins
               (message_id, instance_id, owner_id, agent_id, session_id, run_id,
                request_id, context_generation, target, originated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (*identity.values(), originated_at),
        )
        return {**identity, "originated_at": originated_at}


def find_outbound_origin(store: Any, message_id: str) -> dict[str, Any] | None:
    """Return the exact still-valid origin, or no binding for legacy sends."""
    with store._lock, store._connection() as connection:
        _ensure_schema(connection)
        row = connection.execute(
            """SELECT o.*, s.status AS session_status,
                      s.context_generation AS current_generation, s.owner_id AS current_owner,
                      s.agent_id AS current_agent
               FROM hchat_outbound_origins o JOIN sessions s ON s.session_id=o.session_id
               WHERE o.message_id=? AND o.instance_id=?""",
            (str(message_id).strip(), str(store.instance_id).strip().upper()),
        ).fetchone()
    if row is None:
        return None
    result = dict(row)
    if (result["session_status"] != "active"
            or result["current_generation"] != result["context_generation"]
            or result["current_owner"] != result["owner_id"]
            or result["current_agent"] != result["agent_id"]):
        raise HChatOriginConflict("HChat source conversation is no longer current")
    return result


def resolve_reply_origin(
    store: Any, *, correlation: dict[str, Any] | None, in_reply_to: str,
    conversation_id: str, from_instance: str, from_agent: str,
    to_instance: str, to_agent: str,
) -> dict[str, Any] | None:
    """Resolve a reply only through both the local origin and reverse route."""
    from orchestrator.hchat_delivery_status import delivery_status_matches_outbound_correlation
    from remote.internet_address import LocalAddress, parse_hchat_address

    origin = find_outbound_origin(store, in_reply_to)
    if origin is None:
        return None
    if not delivery_status_matches_outbound_correlation(
        correlation, conversation_id=conversation_id, from_instance=from_instance,
        from_agent=from_agent, to_instance=to_instance, to_agent=to_agent,
    ):
        raise HChatOriginConflict("HChat reply does not match the outbound route")
    target = parse_hchat_address(origin["target"])
    if (not isinstance(target, LocalAddress)
            or target.agent.casefold() != str(from_agent).strip().casefold()
            or (target.instance_id or origin["instance_id"]).upper() != str(from_instance).strip().upper()
            or origin["instance_id"] != str(to_instance).strip().upper()
            or origin["agent_id"] != str(to_agent).strip().casefold()):
        raise HChatOriginConflict("HChat reply does not match the source conversation")
    return origin


def publish_hchat_reply(
    store: Any, *, session_id: str, owner_id: str, agent_id: str,
    idempotency_key: str, text: str, message_context: dict[str, Any],
    expected_context_generation: int | None,
) -> dict[str, Any]:
    """Publish once, retaining the first observed age on a matching retry."""
    with store._lock, store._connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        message_id = store._presentation_message_id(session_id, idempotency_key)
        existing = connection.execute(
            "SELECT message_context_json FROM messages WHERE message_id=? AND session_id=?",
            (message_id, session_id),
        ).fetchone()
        context = dict(message_context)
        if existing is not None:
            prior_context = json.loads(existing["message_context_json"] or "{}")
            if isinstance(prior_context, dict) and "message_age" in prior_context:
                context["message_age"] = prior_context["message_age"]
        return store.append_presentation_message(
            session_id=session_id, owner_id=owner_id, agent_id=agent_id,
            role="assistant", text=text, source="hchat-reply",
            idempotency_key=idempotency_key, presentation_channel="hchat-reply",
            history_eligible=False, message_context=context,
            expected_context_generation=expected_context_generation, _connection=connection,
        )
