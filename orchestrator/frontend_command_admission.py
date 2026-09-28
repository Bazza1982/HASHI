"""Durable, connector-neutral admission for frontend command invocations."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


def _request_digest(payload: Mapping[str, Any], invocation: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        {"request": dict(payload), "invocation": dict(invocation)},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    if len(encoded) > 65536:
        raise ValueError("frontend command request exceeds the storage limit")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class FrontendCommandReservation:
    """A durable reservation that must be completed only after handler success."""

    store: Any
    session_id: str
    owner_id: str
    client_id: str
    request_id: str
    request_digest: str
    state: str
    replayed: bool
    response: Mapping[str, Any] | None = None
    event_id: str | None = None

    def complete(self, response: Mapping[str, Any]) -> dict[str, Any]:
        return self.store.complete_frontend_command_invocation(
            session_id=self.session_id,
            owner_id=self.owner_id,
            client_id=self.client_id,
            request_id=self.request_id,
            request_digest=self.request_digest,
            response=response,
        )


def reserve_frontend_command_invocation(
    runtime: Any,
    *,
    session_id: str,
    owner_id: str,
    client_id: str,
    request_id: str,
    context_generation: int,
    payload: Mapping[str, Any],
    invocation: Mapping[str, Any],
) -> FrontendCommandReservation:
    """Persist the command identity before a connector invokes its handler.

    The payload is used only to bind the idempotency digest; SessionStore keeps
    the typed invocation, not the raw request body.
    """

    from orchestrator.runtime_session import ensure_store

    identity = {
        "session_id": str(session_id or "").strip(),
        "owner_id": str(owner_id or "").strip(),
        "client_id": str(client_id or "").strip(),
        "request_id": str(request_id or "").strip(),
    }
    if any(not value for value in identity.values()):
        raise ValueError("frontend command admission identity is required")
    if type(context_generation) is not int or context_generation < 1:
        raise ValueError("frontend command context generation is invalid")

    normalized_invocation = dict(invocation)
    digest = _request_digest(payload, normalized_invocation)
    store = ensure_store(runtime)
    result = store.reserve_frontend_command_invocation(
        session_id=identity["session_id"],
        owner_id=identity["owner_id"],
        client_id=identity["client_id"],
        request_id=identity["request_id"],
        request_digest=digest,
        context_generation=context_generation,
        invocation=normalized_invocation,
    )
    return FrontendCommandReservation(
        store=store,
        session_id=identity["session_id"],
        owner_id=identity["owner_id"],
        client_id=identity["client_id"],
        request_id=identity["request_id"],
        request_digest=digest,
        state=str(result.get("state") or ""),
        replayed=bool(result.get("replayed")),
        response=result.get("response")
        if isinstance(result.get("response"), Mapping)
        else None,
        event_id=str(result.get("event_id") or "") or None,
    )


def reserve_telegram_command_invocation(
    runtime: Any,
    update: Any,
    *,
    command_name: str,
    arguments: list[str],
    transport_id: str | int,
    ingress_transport: str,
    request_payload: Mapping[str, Any],
) -> FrontendCommandReservation:
    """Build typed Telegram identity and reserve it before command side effects."""

    from orchestrator.command_interaction_bridge import (
        build_frontend_command_invocation,
    )
    from orchestrator.frontend_connector_registry import (
        endpoint_id_for,
        require_connector_operation,
    )
    from orchestrator.runtime_session import current_session_for_update, owner_id
    from orchestrator.slash_command_audit import redact_args

    query = getattr(update, "callback_query", None)
    actor = getattr(getattr(update, "effective_user", None), "id", None)
    if actor is None:
        actor = getattr(getattr(query, "from_user", None), "id", None)
    if type(actor) is not int:
        raise ValueError("Telegram command actor is unavailable")

    chat = getattr(update, "effective_chat", None)
    if chat is None and query is not None:
        chat = getattr(getattr(query, "message", None), "chat", None)
    channel_key = str(
        getattr(update, "_hashi_session_channel_key", None)
        or getattr(chat, "id", None)
        or getattr(getattr(query, "message", None), "chat_id", None)
        or "default"
    )
    session = current_session_for_update(runtime, update)
    resolved_owner = owner_id(
        runtime,
        str(getattr(update, "_hashi_session_owner_id", None) or "") or None,
    )
    transport = str(ingress_transport or "").strip().casefold()
    require_connector_operation("telegram", "ingress", "command")
    endpoint_id = endpoint_id_for(
        "telegram",
        ingress_transport=transport,
        channel_key=channel_key,
    )
    client_id = f"telegram_native:{endpoint_id}"
    identity_digest = hashlib.sha256(
        f"{transport}\0{transport_id}".encode("utf-8")
    ).hexdigest()[:40]
    request_id = f"tgcmd_{identity_digest}"
    payload = {"client_id": client_id, "request_id": request_id}
    metadata = {
        "connector_id": "telegram",
        "ingress_transport": transport,
        "session_surface": "telegram",
        "connection_binding": channel_key,
        "session_id": str(session["session_id"]),
        "context_generation": int(session["context_generation"]),
        "instance_id": str(
            getattr(runtime.global_config, "instance_id", None) or "HASHI"
        ),
    }
    invocation = build_frontend_command_invocation(
        payload,
        metadata,
        actor=actor,
        command_name=command_name,
        arguments=redact_args(command_name, arguments),
    )
    return reserve_frontend_command_invocation(
        runtime,
        session_id=str(session["session_id"]),
        owner_id=resolved_owner,
        client_id=client_id,
        request_id=request_id,
        context_generation=int(session["context_generation"]),
        payload=request_payload,
        invocation=invocation,
    )


__all__ = [
    "FrontendCommandReservation",
    "reserve_frontend_command_invocation",
    "reserve_telegram_command_invocation",
]
