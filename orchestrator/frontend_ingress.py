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
    normalize_admission_receipt,
    normalize_frontend_ingress_envelope,
)
from orchestrator.frontend_delivery import freeze_run_delivery_route
from orchestrator.runtime_session import ensure_store, owner_id
from orchestrator.session_store import (
    IdempotencyConflict,
    SessionConflict,
    SessionNotFound,
)

logger = logging.getLogger(__name__)


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
        cmd = str(command_name).strip().lstrip("/")
        args = list(command_arguments or [])
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

        # Dispatch command execution
        cmd_result: dict[str, Any] = {"ok": True, "command": cmd}
        if hasattr(runtime, "command_registry") and runtime.command_registry.has_command(cmd):
            try:
                cmd_obj = runtime.command_registry.get_command(cmd)
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
    connector_id = normalized_env["connector"]["id"]
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
    "admit_frontend_ingress",
    "query_ingress_admission",
]
