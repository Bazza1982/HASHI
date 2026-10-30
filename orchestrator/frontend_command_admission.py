"""Durable, connector-neutral admission for frontend command invocations."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from orchestrator.sys_command_contract import (
    MAX_SYS_COMMAND_CHARS,
    SysCommandContractError,
)

MAX_FRONTEND_COMMAND_REQUEST_BYTES = (MAX_SYS_COMMAND_CHARS * 5) + 65_536


def _request_digest(payload: Mapping[str, Any], invocation: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        {"request": dict(payload), "invocation": dict(invocation)},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    if len(encoded) > MAX_FRONTEND_COMMAND_REQUEST_BYTES:
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
    record_input: bool = False,
) -> FrontendCommandReservation:
    """Persist the command identity before a connector invokes its handler.

    The payload is used only to bind the idempotency digest; SessionStore keeps
    the typed invocation, not the raw request body.
    """

    from orchestrator.runtime_session import ensure_store

    return reserve_frontend_command_in_store(
        ensure_store(runtime),
        session_id=session_id,
        owner_id=owner_id,
        client_id=client_id,
        request_id=request_id,
        context_generation=context_generation,
        payload=payload,
        invocation=invocation,
        record_input=record_input,
    )


def reserve_frontend_command_in_store(
    store: Any,
    *,
    session_id: str,
    owner_id: str,
    client_id: str,
    request_id: str,
    context_generation: int,
    payload: Mapping[str, Any],
    invocation: Mapping[str, Any],
    record_input: bool = False,
) -> FrontendCommandReservation:
    """Reserve a typed command/control against PAO's shared SessionStore.

    Lifecycle controls may target an Agent that has no running Worker, so this
    variant deliberately accepts the authoritative store instead of requiring
    a target runtime merely to locate it.

    Only a validated user command ingress may opt into recording visible input;
    a typed control or audit reservation is not itself a user slash command.
    """

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
    result = store.reserve_frontend_command_invocation(
        session_id=identity["session_id"],
        owner_id=identity["owner_id"],
        client_id=identity["client_id"],
        request_id=identity["request_id"],
        request_digest=digest,
        context_generation=context_generation,
        invocation=normalized_invocation,
        record_input=record_input,
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


async def execute_frontend_command_invocation(
    runtime: Any,
    *,
    compatibility_adapter_id: str,
    connector_id: str,
    session_id: str,
    owner_id: str,
    client_id: str,
    request_id: str,
    context_generation: int,
    actor: int,
    command_name: str,
    arguments: list[str],
    payload: Mapping[str, Any],
    metadata: Mapping[str, Any] | None = None,
    invoke_handler: Any | None = None,
    command_known: bool | None = None,
) -> dict[str, Any]:
    """Execute a frontend command with unified admission, single reservation, and durable outcome."""

    from orchestrator.command_interaction_bridge import (
        build_frontend_command_invocation,
    )
    from orchestrator.frontend_compatibility import (
        normalize_compatibility_operation,
    )
    from orchestrator.frontend_connector_registry import (
        get_connector_customization,
    )
    from orchestrator.slash_command_audit import redact_args

    try:
        normalized = normalize_compatibility_operation(
            compatibility_adapter_id,
            {
                "kind": "command",
                "name": command_name,
                "arguments": list(arguments or []),
            },
        )
    except SysCommandContractError as exc:
        return {
            "ok": False,
            "command": str(command_name or "").casefold(),
            "execution_state": "failed",
            "error_code": exc.error_code,
            "error_message": exc.error_message,
            "connector_id": connector_id,
            "transport_delivery_state": "not_observed",
            "request_outcome": "rejected",
        }
    adapter = normalized["adapter"]
    if (
        adapter["connector_id"] != connector_id
        or adapter["direction"] != "ingress"
        or adapter["route"] != "standard_fc"
    ):
        raise ValueError("frontend command adapter is not a standard ingress route")
    operation = normalized["operation"]
    normalized_cmd = operation["name"]
    normalized_arguments = list(operation["arguments"])
    redacted_arguments = redact_args(normalized_cmd, normalized_arguments)

    meta = dict(metadata or {})
    meta.setdefault("connector_id", connector_id)
    meta.setdefault("session_id", session_id)
    meta.setdefault("context_generation", context_generation)
    meta.setdefault(
        "instance_id",
        str(getattr(runtime.global_config, "instance_id", None) or "HASHI"),
    )
    meta.setdefault("session_surface", meta.get("session_surface") or connector_id)
    meta.setdefault("connection_binding", meta.get("connection_binding") or "default")

    # 1. Check connector-local customization
    customization = get_connector_customization(
        connector_id,
        kind="command_override",
        key=normalized_cmd,
    )
    rejection_code = None
    if customization and customization.get("route") == "connector_local":
        rejection_code = "connector_local_command"

    # 2. Check if command is known
    if rejection_code is None:
        if command_known is not None:
            if not command_known:
                rejection_code = "command_not_found"
        else:
            from orchestrator.admin_local_testing import supported_commands
            known = (
                normalized_cmd in supported_commands(runtime)
                or hasattr(runtime, f"cmd_{normalized_cmd}")
                or (
                    hasattr(runtime, "command_registry")
                    and runtime.command_registry is not None
                    and runtime.command_registry.has_command(normalized_cmd)
                )
            )
            if not known:
                rejection_code = "command_not_found"

    # 3. Check policy
    if rejection_code is None:
        is_allowed = getattr(runtime, "_is_command_allowed", None)
        if callable(is_allowed) and not is_allowed(normalized_cmd):
            rejection_code = "command_disabled"

    if type(actor) is not int:
        raise ValueError("frontend command actor is invalid")
    invocation = build_frontend_command_invocation(
        {"client_id": client_id, "request_id": request_id},
        meta,
        actor=actor,
        command_name=normalized_cmd,
        arguments=redacted_arguments,
        decision="denied" if rejection_code is not None else "allowed",
    )

    # Single-layer reservation before any effects
    ticket = reserve_frontend_command_invocation(
        runtime,
        session_id=session_id,
        owner_id=owner_id,
        client_id=client_id,
        request_id=request_id,
        context_generation=context_generation,
        payload=payload,
        invocation=invocation,
        record_input=True,
    )

    if ticket.state == "completed":
        response = dict(ticket.response or {})
        response["replayed"] = True
        response["command_event_id"] = ticket.event_id
        return response

    if ticket.state != "reserved":
        return {
            "ok": False,
            "command": normalized_cmd,
            "execution_state": "outcome_unknown",
            "error_code": "command_outcome_unknown",
            "replayed": True,
            "outcome_unknown": True,
        }

    # Known failure without side effects (unknown, disabled, or connector-local rejected)
    if rejection_code is not None:
        failed_response = {
            "ok": False,
            "command": normalized_cmd,
            "execution_state": "failed",
            "error_code": rejection_code,
            "connector_id": connector_id,
            "transport_delivery_state": "not_observed",
        }
        completion = ticket.complete(failed_response)
        result = dict(completion.get("response") or failed_response)
        result["command_event_id"] = completion.get("event_id")
        result["replayed"] = bool(completion.get("replayed"))
        return result

    # Execute handler
    handler_result = None
    if invoke_handler is not None:
        handler_result = await invoke_handler()

    if isinstance(handler_result, Mapping):
        res_dict = dict(handler_result)
        is_ok = bool(res_dict.get("ok", True))
        if not is_ok:
            error_code = str(
                res_dict.get("error_code")
                or res_dict.get("error")
                or "command_failed"
            )
            response = {
                "ok": False,
                "command": normalized_cmd,
                "execution_state": "failed",
                "error_code": error_code,
                "connector_id": connector_id,
                "transport_delivery_state": "not_observed",
                "result": res_dict,
            }
            error_message = str(res_dict.get("error_message") or "").strip()
            if error_message:
                response["error_message"] = error_message
        else:
            response = {
                "ok": True,
                "command": normalized_cmd,
                "execution_state": "completed",
                "connector_id": connector_id,
                "transport_delivery_state": "not_observed",
                "result": res_dict,
            }
        if "request_outcome" in res_dict:
            response["request_outcome"] = res_dict["request_outcome"]
    else:
        # None or non-mapping return compatible as success
        response = {
            "ok": True,
            "command": normalized_cmd,
            "execution_state": "completed",
            "connector_id": connector_id,
            "transport_delivery_state": "not_observed",
        }

    completion = ticket.complete(response)
    result = dict(completion.get("response") or response)
    result["command_event_id"] = completion.get("event_id")
    result["replayed"] = bool(completion.get("replayed"))
    return result


async def execute_telegram_command_invocation(
    runtime: Any,
    update: Any,
    *,
    command_name: str,
    arguments: list[str],
    transport_id: str | int,
    ingress_transport: str = "telegram.command",
    request_payload: Mapping[str, Any] | None = None,
    invoke_handler: Any | None = None,
    command_known: bool | None = None,
) -> dict[str, Any]:
    """Build Telegram transport identity and execute via the unified admission service."""

    from orchestrator.frontend_connector_registry import (
        endpoint_id_for,
        require_connector_operation,
    )
    from orchestrator.runtime_session import current_session_for_update, owner_id

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
    payload = dict(request_payload) if request_payload is not None else {
        "client_id": client_id,
        "request_id": request_id,
    }
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

    return await execute_frontend_command_invocation(
        runtime,
        compatibility_adapter_id="telegram.native_command",
        connector_id="telegram",
        session_id=str(session["session_id"]),
        owner_id=resolved_owner,
        client_id=client_id,
        request_id=request_id,
        context_generation=int(session["context_generation"]),
        actor=actor,
        command_name=command_name,
        arguments=arguments,
        payload=payload,
        metadata=metadata,
        invoke_handler=invoke_handler,
        command_known=command_known,
    )


__all__ = [
    "FrontendCommandReservation",
    "execute_frontend_command_invocation",
    "execute_telegram_command_invocation",
    "reserve_frontend_command_in_store",
    "reserve_frontend_command_invocation",
    "reserve_telegram_command_invocation",
]
