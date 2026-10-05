"""Trusted PAO command-to-Run handoff, independent of business source labels."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class CommandDerivedRequestError(ValueError):
    def __init__(self, code, *, outcome="not_admitted"):
        self.error_code = code
        self.request_outcome = outcome
        super().__init__(code)


@dataclass(frozen=True)
class CommandRequestContext:
    chat_id: Any
    metadata: dict
    idempotency_key: str | None


def command_request_context(runtime, update, context, *, purpose):
    """Require authenticated transport evidence and resolve the actual Session.

    Local adapters provide the Connector ID after normalize_compatibility_command;
    native Telegram is identified by its transport Update type, never chat ID.
    The queue still independently freezes its output route at admission.
    """
    from telegram import Update
    from orchestrator import runtime_session
    from orchestrator.frontend_connector_registry import canonical_connector_id, require_connector_operation
    from orchestrator.session_store import SessionConflict, SessionNotFound

    connector = getattr(context, "frontend_connector_id", None)
    if not connector and isinstance(update, Update):
        connector = "telegram"
    if not connector:
        raise CommandDerivedRequestError("command_derived_binding_missing")
    try:
        connector = canonical_connector_id(connector, ingress_transport=connector, surface=connector)
        require_connector_operation(connector, "ingress", "command")
        session = runtime_session.current_session_for_update(runtime, update)
        expected_generation = getattr(update, "_hashi_session_context_generation", None)
        if expected_generation is not None and int(expected_generation) != int(session["context_generation"]):
            raise SessionConflict("command Session generation changed")
        surface = getattr(update, "_hashi_session_surface", None) or ("telegram" if connector == "telegram" else None)
        surface_connector = canonical_connector_id("", surface=surface) if surface else None
        if surface_connector != connector and not (connector == "session_api" and surface_connector == "backend_api"):
            raise ValueError("command Connector route mismatch")
        channel = getattr(update, "_hashi_session_channel_key", None)
        raw_chat = getattr(getattr(update, "effective_chat", None), "id", None)
        if not channel and connector == "telegram":
            channel = str(raw_chat)
        if not channel:
            raise ValueError("command channel missing")
        metadata = {
            "ingress_transport": connector,
            "session_surface": surface,
            "session_channel_key": channel,
            "owner_id": session["owner_id"],
            "session_id": session["session_id"],
            "session_context_generation": int(session["context_generation"]),
        }
    except (ValueError, TypeError, KeyError, SessionConflict, SessionNotFound) as exc:
        raise CommandDerivedRequestError("command_derived_binding_invalid") from exc
    invocation = getattr(update, "update_id", None)
    key = f"command-derived:{connector}:{session['session_id']}:{session['context_generation']}:{invocation}:{purpose}" if invocation is not None else None
    return CommandRequestContext(raw_chat if connector == "telegram" else 0, metadata, key)
