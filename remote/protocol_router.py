"""Protocol envelope validation for Hashi Remote routing."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class EnvelopeValidation:
    ok: bool
    status_code: int
    payload: dict[str, Any]
    error: dict[str, Any] | None = None


def _error_payload(code: str, message: str, *, retryable: bool, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "ok": False,
        "message_type": "error",
        "body": {
            "code": code,
            "message": message,
            "retryable": bool(retryable),
            "failed_message_id": payload.get("message_id"),
            "conversation_id": payload.get("conversation_id"),
            "from_instance": payload.get("from_instance"),
            "from_agent": payload.get("from_agent"),
            "to_instance": payload.get("to_instance"),
            "to_agent": payload.get("to_agent"),
            "details": {},
        },
    }


def validate_protocol_envelope(
    payload: dict[str, Any],
    *,
    local_instance: str,
    max_allowed_ttl: int = 8,
) -> EnvelopeValidation:
    data = dict(payload or {})
    message_type = str(data.get("message_type") or "agent_message").strip().lower()
    data["message_type"] = message_type
    message_id = str(data.get("message_id") or "").strip()
    conversation_id = str(data.get("conversation_id") or "").strip()
    from_instance = str(data.get("from_instance") or "").strip().upper()
    from_agent = str(data.get("from_agent") or "").strip().lower()
    to_instance = str(data.get("to_instance") or "").strip().upper()
    to_agent = str(data.get("to_agent") or "").strip().lower()
    if not all([message_id, conversation_id, from_instance, from_agent, to_instance, to_agent]):
        return EnvelopeValidation(
            ok=False,
            status_code=400,
            payload=data,
            error=_error_payload("invalid_message", "Missing required protocol envelope fields", retryable=False, payload=data),
        )
    if message_type == "agent_reply" and not str(data.get("in_reply_to") or "").strip():
        return EnvelopeValidation(
            ok=False,
            status_code=400,
            payload=data,
            error=_error_payload("invalid_reply", "agent_reply requires in_reply_to", retryable=False, payload=data),
        )
    try:
        requested_ttl = int(data.get("ttl") if data.get("ttl") is not None else max_allowed_ttl)
        hop_count = int(data.get("hop_count") or 0)
    except (TypeError, ValueError):
        return EnvelopeValidation(
            ok=False,
            status_code=400,
            payload=data,
            error=_error_payload("delivery_expired", "TTL or hop_count is invalid", retryable=False, payload=data),
        )
    ttl_ceiling = max(1, int(max_allowed_ttl or 8))
    normalized_ttl = min(requested_ttl, ttl_ceiling)
    if normalized_ttl <= 0 or hop_count >= normalized_ttl:
        return EnvelopeValidation(
            ok=False,
            status_code=400,
            payload=data,
            error=_error_payload("delivery_expired", "TTL expired or hop_count exceeded TTL", retryable=False, payload=data),
        )
    route_trace = [str(item).strip().upper() for item in (data.get("route_trace") or []) if str(item).strip()]
    normalized_local = str(local_instance or "").strip().upper()
    if normalized_local and normalized_local in route_trace:
        return EnvelopeValidation(
            ok=False,
            status_code=409,
            payload=data,
            error=_error_payload("loop_detected", "Local instance already present in route_trace", retryable=False, payload=data),
        )
    data.update(
        {
            "message_id": message_id,
            "conversation_id": conversation_id,
            "from_instance": from_instance,
            "from_agent": from_agent,
            "to_instance": to_instance,
            "to_agent": to_agent,
            "ttl": normalized_ttl,
            "hop_count": hop_count,
            "route_trace": route_trace,
        }
    )
    return EnvelopeValidation(ok=True, status_code=200, payload=data)
