"""Unified frontend ingress admission service for HASHI Frontend Connectors.

This module is the single ingress boundary into PAO. It handles normalization,
identity verification, context generation checks, media binding, route freezing,
and delegates to SessionStore and command admission without creating a second
business database.
"""
from __future__ import annotations

import hashlib
import logging
from collections.abc import Mapping
from typing import Any

from orchestrator.frontend_command_admission import (
    FrontendCommandReservation,
    reserve_frontend_command_invocation,
)
from orchestrator.frontend_contracts import (
    FRONTEND_REQUEST_TYPE,
    FRONTEND_REQUEST_VERSION,
    normalize_admission_receipt,
    normalize_frontend_ingress_envelope,
    normalize_frontend_request,
)
from orchestrator.frontend_delivery import freeze_run_delivery_route
from orchestrator.runtime_session import ensure_store, owner_id
from orchestrator.session_store import (
    IdempotencyConflict,
    SessionConflict,
    SessionNotFound,
)

logger = logging.getLogger(__name__)


def accept_runtime_ingress(
    runtime: Any,
    envelope: Mapping[str, Any],
    *,
    request_id: str,
    chat_id: Any,
    prompt: str,
    source: str,
    request_metadata: Mapping[str, Any] | None,
    request_content: Mapping[str, Any] | None,
    idempotency_key: str | None,
) -> tuple[dict[str, Any], Any | None, str, str, str]:
    """Validate the standard envelope, then use PAO's sole Run writer."""

    normalized = normalize_frontend_ingress_envelope(envelope)
    metadata = dict(request_metadata or {})
    operation_content: list[dict[str, Any]] = []
    canonical_parts = (
        request_content.get("parts")
        if isinstance(request_content, Mapping)
        else None
    )
    if isinstance(canonical_parts, (list, tuple)):
        for part in canonical_parts:
            if not isinstance(part, Mapping):
                continue
            part_type = str(part.get("type") or "").strip().casefold()
            if part_type == "text" and str(part.get("text") or ""):
                operation_content.append(
                    {"type": "text", "text": str(part["text"])}
                )
            elif part_type == "media" and str(part.get("attachment_id") or ""):
                operation_content.append(
                    {
                        "type": "attachment_ref",
                        "attachment_id": str(part["attachment_id"]),
                        "ordinal": len(operation_content),
                        "caption": str(part.get("caption") or "") or None,
                    }
                )
    existing_session_content = metadata.get("session_message_content")
    if not operation_content and isinstance(existing_session_content, (list, tuple)):
        for block in existing_session_content:
            if not isinstance(block, Mapping):
                continue
            block_type = str(block.get("type") or "").strip().casefold()
            if block_type == "text" and str(block.get("text") or ""):
                operation_content.append(
                    {"type": "text", "text": str(block["text"])}
                )
            elif block_type in {"media", "attachment", "audio"} and str(
                block.get("attachment_id") or ""
            ):
                operation_content.append(
                    {
                        "type": "attachment_ref",
                        "attachment_id": str(block["attachment_id"]),
                        "ordinal": len(operation_content),
                        "caption": str(block.get("caption") or "") or None,
                    }
                )
            elif block_type == "reply_ref":
                operation_content.append(
                    {
                        "type": "reply_ref",
                        "event_id": str(block.get("event_id") or ""),
                    }
                )
    if not operation_content and str(prompt or ""):
        operation_content.append({"type": "text", "text": str(prompt)})
    expected_idempotency_digest = "sha256:" + hashlib.sha256(
        str(idempotency_key or request_id).encode("utf-8")
    ).hexdigest()
    if normalized["message"]["idempotency_digest"] != expected_idempotency_digest:
        raise SessionConflict("frontend ingress idempotency binding changed")
    expected_instance = str(
        getattr(runtime.global_config, "instance_id", None) or "HASHI"
    ).upper()
    if normalized["instance_id"] != expected_instance:
        raise SessionConflict("frontend ingress instance changed during admission")
    if normalized["target"]["agent_id"] != str(runtime.name).casefold():
        raise SessionConflict("frontend ingress Agent changed during admission")
    if normalized["message"]["request_id"] != str(request_id):
        raise SessionConflict("frontend ingress request changed during admission")
    if normalized["target"]["session_id"] != str(
        metadata.get("session_id") or ""
    ):
        raise SessionConflict("frontend ingress Session changed during admission")

    reply_event_id = str(metadata.get("reply_to_event_id") or "").strip()
    if reply_event_id:
        store = ensure_store(runtime)
        reference = store.frontend_event_reference(
            session_id=normalized["target"]["session_id"],
            owner_id=owner_id(runtime, str(metadata.get("owner_id") or "") or None),
            event_id=reply_event_id,
        )
        if reference is None:
            raise SessionConflict("frontend reply reference is not visible in this Session")
        reply_block = {"type": "reply_ref", "event_id": reply_event_id}
        operation_content = [
            item
            for item in operation_content
            if str(item.get("type") or "").casefold() != "reply_ref"
        ]
        operation_content.insert(0, reply_block)
        for ordinal, item in enumerate(operation_content):
            if str(item.get("type") or "").casefold() == "attachment_ref":
                item["ordinal"] = ordinal

        raw_session_content = metadata.get("session_message_content")
        if isinstance(raw_session_content, (list, tuple)):
            session_content = [
                dict(block)
                for block in raw_session_content
                if isinstance(block, Mapping)
            ]
        else:
            session_content = []
            for item in operation_content:
                item_type = str(item.get("type") or "").casefold()
                if item_type == "text":
                    session_content.append(
                        {"type": "text", "text": str(item.get("text") or "")}
                    )
                elif item_type == "attachment_ref":
                    session_content.append(
                        {
                            "type": "media",
                            "attachment_id": str(item.get("attachment_id") or ""),
                            **(
                                {"caption": str(item["caption"])}
                                if item.get("caption")
                                else {}
                            ),
                        }
                    )
                elif item_type == "reply_ref":
                    session_content.append(dict(reply_block))
        existing_refs = [
            str(block.get("event_id") or "")
            for block in session_content
            if str(block.get("type") or "").casefold() == "reply_ref"
        ]
        if any(event_id != reply_event_id for event_id in existing_refs):
            raise SessionConflict("frontend reply reference changed during admission")
        session_content = [
            block
            for block in session_content
            if str(block.get("type") or "").casefold() != "reply_ref"
        ]
        session_content.insert(0, dict(reply_block))
        metadata["session_message_content"] = session_content

        quote_text = str(reference.get("text") or "")[:4000]
        safe_reference = {
            "event_id": reply_event_id,
            "message_ref": reply_event_id,
            "session_id": normalized["target"]["session_id"],
            "context_generation": int(reference["context_generation"]),
            "role": "assistant",
            "author": str(runtime.name),
            "timestamp": str(reference.get("created_at") or ""),
            "text": quote_text or "[Referenced Agent message]",
        }
        snapshot = metadata.get("message_context_snapshot")
        if isinstance(snapshot, Mapping):
            snapshot = dict(snapshot)
        else:
            snapshot = {}
        snapshot["reply_reference"] = safe_reference
        metadata["message_context_snapshot"] = snapshot

    normalize_frontend_request(
        {
            "type": FRONTEND_REQUEST_TYPE,
            "version": FRONTEND_REQUEST_VERSION,
            "ingress": normalized,
            "operation": {"kind": "message", "content": operation_content},
        }
    )

    from orchestrator import runtime_session

    result = runtime_session.accept_request(
        runtime,
        request_id=request_id,
        chat_id=chat_id,
        prompt=prompt,
        source=source,
        request_metadata=metadata,
        request_content=request_content,
        idempotency_key=idempotency_key,
    )
    session, accepted, _owner, _surface, _channel = result
    if str(session.get("session_id") or "") != normalized["target"]["session_id"]:
        raise SessionConflict("frontend ingress Session binding was not preserved")
    if accepted is not None and str(accepted.request_id) != str(request_id):
        raise SessionConflict("frontend ingress receipt request mismatch")
    return result


def admit_frontend_ingress(
    runtime: Any,
    envelope: Mapping[str, Any],
    *,
    text: str = "",
    content_blocks: list[Mapping[str, Any]] | None = None,
    command_name: str | None = None,
    command_arguments: list[str] | None = None,
    control_action: str | None = None,
    execution_mode: str | None = None,
    parent_run_id: str | None = None,
    response_preferences: Mapping[str, Any] | None = None,
    delivery_preference: Mapping[str, Any] | None = None,
    chat_id: Any | None = None,
) -> dict[str, Any]:
    """Admit an ingress request into PAO.

    Validates envelope, enforces idempotency, checks session/context generation,
    and returns a normalized AdmissionReceipt.
    """
    normalized_env = normalize_frontend_ingress_envelope(envelope)
    from orchestrator.frontend_connector_registry import (
        get_connector_customization,
        require_connector_operation,
    )
    connector_id = normalized_env["connector"]["id"]

    # 1. Instance and Agent verification
    expected_instance = str(
        getattr(runtime.global_config, "instance_id", None) or "HASHI"
    ).upper()
    if normalized_env["instance_id"] != expected_instance:
        return normalize_admission_receipt(
            {
                "type": "hashi.admission-receipt",
                "version": 1,
                "status": "rejected",
                "session_id": normalized_env["target"]["session_id"],
                "run_id": None,
                "request_id": normalized_env["message"]["request_id"],
                "idempotency_digest": normalized_env["message"]["idempotency_digest"],
                "replayed": False,
                "reason": f"instance mismatch: {normalized_env['instance_id']} != {expected_instance}",
            }
        )

    target_agent = normalized_env["target"]["agent_id"]
    runtime_agent = str(getattr(runtime, "name", "") or "").casefold()
    if runtime_agent and target_agent != runtime_agent:
        return normalize_admission_receipt(
            {
                "type": "hashi.admission-receipt",
                "version": 1,
                "status": "rejected",
                "session_id": normalized_env["target"]["session_id"],
                "run_id": None,
                "request_id": normalized_env["message"]["request_id"],
                "idempotency_digest": normalized_env["message"]["idempotency_digest"],
                "replayed": False,
                "reason": f"agent mismatch: {target_agent} != {runtime_agent}",
            }
        )

    store = ensure_store(runtime)
    session_id = normalized_env["target"]["session_id"]
    resolved_owner = owner_id(runtime)

    try:
        session = store.get_session(session_id, owner_id=resolved_owner)
    except SessionNotFound:
        return normalize_admission_receipt(
            {
                "type": "hashi.admission-receipt",
                "version": 1,
                "status": "rejected",
                "session_id": session_id,
                "run_id": None,
                "request_id": normalized_env["message"]["request_id"],
                "idempotency_digest": normalized_env["message"]["idempotency_digest"],
                "replayed": False,
                "reason": f"session not found: {session_id}",
            }
        )

    # 2. Control intent
    if control_action is not None or command_name in {"/cancel", "cancel", "/stop", "stop"}:
        require_connector_operation(connector_id, "ingress", "control")
        action = str(control_action or "cancel").strip().casefold()
        if action in {"cancel", "stop"}:
            endpoint_id = normalized_env["connector"]["endpoint_id"]
            client_id = f"{normalized_env['connector']['id']}:{endpoint_id}"
            request_id = normalized_env["message"]["request_id"]
            invocation = {
                "type": "hashi.frontend-command",
                "version": 2,
                "invocation_id": f"inv_{request_id}",
                "request_id": request_id,
                "connector_id": normalized_env["connector"]["id"],
                "endpoint_id": endpoint_id,
                "session_id": session_id,
                "context_generation": int(session["context_generation"]),
                "command": "cancel",
                "issued_action_id": None,
                "revision": None,
                "arguments": [],
                "actor_digest": "sha256:"
                + hashlib.sha256(resolved_owner.encode("utf-8")).hexdigest(),
                "idempotency_digest": normalized_env["message"]["idempotency_digest"],
                "authorization": {"decision": "allowed", "scope": "session"},
            }
            try:
                reservation = reserve_frontend_command_invocation(
                    runtime,
                    session_id=session_id,
                    owner_id=resolved_owner,
                    client_id=client_id,
                    request_id=request_id,
                    context_generation=int(session["context_generation"]),
                    payload={"action": "cancel"},
                    invocation=invocation,
                )
            except IdempotencyConflict as exc:
                return normalize_admission_receipt(
                    {
                        "type": "hashi.admission-receipt",
                        "version": 1,
                        "status": "conflict",
                        "session_id": session_id,
                        "run_id": None,
                        "request_id": request_id,
                        "idempotency_digest": normalized_env["message"]["idempotency_digest"],
                        "replayed": False,
                        "reason": str(exc),
                    }
                )
            if reservation.replayed:
                if reservation.state == "completed":
                    prior_response = reservation.response or {}
                    return normalize_admission_receipt(
                        {
                            "type": "hashi.admission-receipt",
                            "version": 1,
                            "status": "accepted",
                            "session_id": session_id,
                            "run_id": prior_response.get("cancelled_run_id"),
                            "request_id": request_id,
                            "idempotency_digest": normalized_env["message"]["idempotency_digest"],
                            "replayed": True,
                            "reason": "cancel_replayed",
                        }
                    )
                return normalize_admission_receipt(
                    {
                        "type": "hashi.admission-receipt",
                        "version": 1,
                        "status": "conflict",
                        "session_id": session_id,
                        "run_id": None,
                        "request_id": request_id,
                        "idempotency_digest": normalized_env["message"]["idempotency_digest"],
                        "replayed": True,
                        "reason": "cancel_outcome_unknown",
                    }
                )
            active_runs = store.list_active_runs(
                owner_id=resolved_owner, session_id=session_id
            )
            cancelled_run_id = None
            if active_runs:
                target_run = active_runs[0]
                store.cancel_run(
                    target_run["run_id"],
                    owner_id=resolved_owner,
                    reason="frontend_ingress_cancel",
                )
                cancelled_run_id = target_run["run_id"]
            reservation.complete(
                {
                    "ok": True,
                    "action": "cancel",
                    "cancelled_run_id": cancelled_run_id,
                }
            )
            return normalize_admission_receipt(
                {
                    "type": "hashi.admission-receipt",
                    "version": 1,
                    "status": "accepted",
                    "session_id": session_id,
                    "run_id": cancelled_run_id,
                    "request_id": request_id,
                    "idempotency_digest": normalized_env["message"]["idempotency_digest"],
                    "replayed": False,
                    "reason": "cancelled",
                }
            )

    # 3. Command intent
    if command_name is not None:
        require_connector_operation(connector_id, "ingress", "command")
        cmd = str(command_name).strip().lstrip("/")
        args = list(command_arguments or [])
        customization = get_connector_customization(
            connector_id,
            kind="command_override",
            key=cmd,
        )
        if customization and customization["route"] == "connector_local":
            return normalize_admission_receipt(
                {
                    "type": "hashi.admission-receipt",
                    "version": 1,
                    "status": "rejected",
                    "session_id": session_id,
                    "run_id": None,
                    "request_id": normalized_env["message"]["request_id"],
                    "idempotency_digest": normalized_env["message"]["idempotency_digest"],
                    "replayed": False,
                    "reason": "registered_connector_local_override",
                }
            )
        registry = getattr(runtime, "command_registry", None)
        command_registered = bool(
            registry is not None and registry.has_command(cmd)
        )
        authorizer = getattr(runtime, "_is_command_allowed", None)
        command_allowed = command_registered and (
            not callable(authorizer) or bool(authorizer(cmd))
        )
        endpoint_id = normalized_env["connector"]["endpoint_id"]
        client_id = f"{normalized_env['connector']['id']}:{endpoint_id}"
        request_id = normalized_env["message"]["request_id"]
        invocation = {
            "type": "hashi.frontend-command",
            "version": 2,
            "invocation_id": f"inv_{request_id}",
            "request_id": request_id,
            "connector_id": normalized_env["connector"]["id"],
            "endpoint_id": endpoint_id,
            "session_id": session_id,
            "context_generation": int(session["context_generation"]),
            "command": cmd,
            "issued_action_id": None,
            "revision": None,
            "arguments": args,
            "actor_digest": "sha256:" + hashlib.sha256(resolved_owner.encode("utf-8")).hexdigest(),
            "idempotency_digest": normalized_env["message"]["idempotency_digest"],
            "authorization": {
                "decision": "allowed" if command_allowed else "denied",
                "scope": "session",
            },
        }
        try:
            reservation = reserve_frontend_command_invocation(
                runtime,
                session_id=session_id,
                owner_id=resolved_owner,
                client_id=client_id,
                request_id=request_id,
                context_generation=int(session["context_generation"]),
                payload={"command": cmd, "args": args},
                invocation=invocation,
            )
        except IdempotencyConflict as exc:
            return normalize_admission_receipt(
                {
                    "type": "hashi.admission-receipt",
                    "version": 1,
                    "status": "conflict",
                    "session_id": session_id,
                    "run_id": None,
                    "request_id": request_id,
                    "idempotency_digest": normalized_env["message"]["idempotency_digest"],
                    "replayed": False,
                    "reason": str(exc),
                }
            )

        if reservation.replayed and reservation.state == "completed":
            return normalize_admission_receipt(
                {
                    "type": "hashi.admission-receipt",
                    "version": 1,
                    "status": "accepted",
                    "session_id": session_id,
                    "run_id": None,
                    "request_id": request_id,
                    "idempotency_digest": normalized_env["message"]["idempotency_digest"],
                    "replayed": True,
                    "reason": "command_replayed",
                }
            )
        if reservation.replayed:
            return normalize_admission_receipt(
                {
                    "type": "hashi.admission-receipt",
                    "version": 1,
                    "status": "conflict",
                    "session_id": session_id,
                    "run_id": None,
                    "request_id": request_id,
                    "idempotency_digest": normalized_env["message"]["idempotency_digest"],
                    "replayed": True,
                    "reason": "command_outcome_unknown",
                }
            )

        # Dispatch only a command owned by PAO's registry.  A missing command is
        # a terminal rejection, never a successful no-op invented by FC.
        if not command_registered:
            reservation.complete(
                {"ok": False, "command": cmd, "error_code": "command_not_registered"}
            )
            return normalize_admission_receipt(
                {
                    "type": "hashi.admission-receipt",
                    "version": 1,
                    "status": "rejected",
                    "session_id": session_id,
                    "run_id": None,
                    "request_id": request_id,
                    "idempotency_digest": normalized_env["message"]["idempotency_digest"],
                    "replayed": False,
                    "reason": "command_not_registered",
                }
            )
        if not command_allowed:
            reservation.complete(
                {"ok": False, "command": cmd, "error_code": "command_forbidden"}
            )
            return normalize_admission_receipt(
                {
                    "type": "hashi.admission-receipt",
                    "version": 1,
                    "status": "rejected",
                    "session_id": session_id,
                    "run_id": None,
                    "request_id": request_id,
                    "idempotency_digest": normalized_env["message"]["idempotency_digest"],
                    "replayed": False,
                    "reason": "command_forbidden",
                }
            )

        cmd_result: dict[str, Any] = {"ok": True, "command": cmd}
        if registry.has_command(cmd):
            try:
                cmd_obj = registry.get_command(cmd)
                if hasattr(cmd_obj, "handler"):
                    res = cmd_obj.handler(runtime, *args)
                    if isinstance(res, dict):
                        cmd_result.update(res)
            except Exception as exc:
                logger.warning("Command execution failed for %s: %s", cmd, exc)
                cmd_result = {"ok": False, "error": str(exc)}

        reservation.complete(cmd_result)
        return normalize_admission_receipt(
            {
                "type": "hashi.admission-receipt",
                "version": 1,
                "status": "accepted",
                "session_id": session_id,
                "run_id": None,
                "request_id": request_id,
                "idempotency_digest": normalized_env["message"]["idempotency_digest"],
                "replayed": False,
                "reason": "command_executed",
            }
        )

    # 4. Message intent (Turn / Run admission)
    require_connector_operation(connector_id, "ingress", "message")
    delivery_route = freeze_run_delivery_route(
        message_source_id=connector_id,
        session_surface=connector_id,
        session_channel_key=str(chat_id or "default"),
        chat_id=chat_id,
        telegram_requested=bool(
            delivery_preference and delivery_preference.get("telegram_mirror")
        ),
    )

    request_id = normalized_env["message"]["request_id"]
    idempotency_key = f"{connector_id}:{request_id}"
    blocks = content_blocks or ([{"type": "text", "text": text}] if text else None)

    try:
        accepted = store.accept_run(
            session_id=session_id,
            owner_id=resolved_owner,
            agent_id=runtime.name,
            request_id=request_id,
            text=text,
            source=connector_id,
            idempotency_key=idempotency_key,
            execution_mode=execution_mode,
            content=blocks,
            parent_run_id=parent_run_id,
            response_preferences=response_preferences,
            delivery_route=delivery_route,
        )
        return normalize_admission_receipt(
            {
                "type": "hashi.admission-receipt",
                "version": 1,
                "status": "accepted",
                "session_id": session_id,
                "run_id": accepted.run_id,
                "request_id": accepted.request_id,
                "idempotency_digest": normalized_env["message"]["idempotency_digest"],
                "replayed": accepted.replayed,
                "reason": "run_replayed" if accepted.replayed else "run_accepted",
            }
        )
    except IdempotencyConflict as exc:
        return normalize_admission_receipt(
            {
                "type": "hashi.admission-receipt",
                "version": 1,
                "status": "conflict",
                "session_id": session_id,
                "run_id": None,
                "request_id": request_id,
                "idempotency_digest": normalized_env["message"]["idempotency_digest"],
                "replayed": False,
                "reason": str(exc),
            }
        )
    except SessionConflict as exc:
        return normalize_admission_receipt(
            {
                "type": "hashi.admission-receipt",
                "version": 1,
                "status": "rejected",
                "session_id": session_id,
                "run_id": None,
                "request_id": request_id,
                "idempotency_digest": normalized_env["message"]["idempotency_digest"],
                "replayed": False,
                "reason": str(exc),
            }
        )


def query_ingress_admission(
    runtime: Any,
    *,
    session_id: str,
    request_id: str,
    idempotency_digest: str,
) -> dict[str, Any] | None:
    """Query the persistent admission result for an ingress request."""
    store = ensure_store(runtime)
    resolved_owner = owner_id(runtime)
    try:
        store.get_session(session_id, owner_id=resolved_owner)
    except SessionNotFound:
        return None

    run = store.get_run_by_request(request_id)
    if run is not None and str(run.get("session_id")) == session_id:
        return normalize_admission_receipt(
            {
                "type": "hashi.admission-receipt",
                "version": 1,
                "status": "accepted",
                "session_id": session_id,
                "run_id": str(run.get("run_id")),
                "request_id": request_id,
                "idempotency_digest": idempotency_digest,
                "replayed": True,
                "reason": f"run_state:{run.get('state')}",
            }
        )
    return None


__all__ = [
    "accept_runtime_ingress",
    "admit_frontend_ingress",
    "query_ingress_admission",
]
