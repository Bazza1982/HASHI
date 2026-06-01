"""Operator-facing delivery result mapping for Hashi Remote protocol errors."""

from __future__ import annotations

from typing import Any


RESULT_NOTES = {
    "target_not_found": "Target agent does not exist in the resolved remote directory.",
    "target_instance_offline": "Target instance is not currently reachable as a trusted Remote peer.",
    "target_agent_unavailable": "Target instance was reached, but its local runtime could not accept delivery.",
    "auth_failed": "Remote protocol authentication failed.",
    "delivery_rejected": "Remote protocol rejected the message before delivery.",
    "delivery_timed_out": "No authoritative delivery result was obtained before timeout.",
    "internal_error": "Unexpected remote delivery failure.",
}


PROTOCOL_CODE_TO_RESULT = {
    "target_agent_not_found": "target_not_found",
    "target_instance_not_found": "target_instance_offline",
    "forward_failed": "target_instance_offline",
    "peer_offline": "target_instance_offline",
    "peer_stale": "target_instance_offline",
    "target_agent_unavailable": "target_agent_unavailable",
    "local_enqueue_failed": "target_agent_unavailable",
    "auth_required": "auth_failed",
    "auth_failed": "auth_failed",
    "invalid_signature": "auth_failed",
    "handshake_incompatible": "delivery_rejected",
    "handshake_rejected": "delivery_rejected",
    "delivery_expired": "delivery_rejected",
    "invalid_message": "delivery_rejected",
    "invalid_reply": "delivery_rejected",
    "loop_detected": "delivery_rejected",
    "duplicate_message": "delivery_rejected",
    "reply_correlation_missing": "delivery_rejected",
    "reply_correlation_mismatch": "delivery_rejected",
    "attachment_capability_missing": "delivery_rejected",
    "reply_timeout": "delivery_timed_out",
    "timed_out": "delivery_timed_out",
}


def protocol_error_code(payload: dict[str, Any] | None) -> str:
    data = payload or {}
    body = data.get("body") if isinstance(data.get("body"), dict) else {}
    for candidate in (body.get("code"), data.get("code"), data.get("error_code")):
        code = str(candidate or "").strip()
        if code:
            return code
    return ""


def protocol_error_message(payload: dict[str, Any] | None) -> str:
    data = payload or {}
    body = data.get("body") if isinstance(data.get("body"), dict) else {}
    for candidate in (body.get("message"), data.get("error"), data.get("detail"), data.get("message")):
        message = str(candidate or "").strip()
        if message:
            return message
    return ""


def classify_delivery_result(payload: dict[str, Any] | None = None, *, transport_error: str | None = None) -> str:
    if transport_error:
        text = str(transport_error).lower()
        if "401" in text or "403" in text or "auth" in text or "signature" in text:
            return "auth_failed"
        if "timed out" in text or "timeout" in text:
            return "delivery_timed_out"
        if "connection refused" in text or "unreachable" in text or "name or service not known" in text:
            return "target_instance_offline"
        return "target_instance_offline"

    code = protocol_error_code(payload)
    if code:
        return PROTOCOL_CODE_TO_RESULT.get(code, "internal_error")
    return "internal_error"


def delivery_result_note(result: str) -> str:
    return RESULT_NOTES.get(str(result or ""), RESULT_NOTES["internal_error"])


def format_delivery_result(payload: dict[str, Any] | None = None, *, transport_error: str | None = None) -> str:
    result = classify_delivery_result(payload, transport_error=transport_error)
    message = protocol_error_message(payload) if payload else ""
    note = delivery_result_note(result)
    if message and message != note:
        return f"{result}: {note} Detail: {message}"
    return f"{result}: {note}"
