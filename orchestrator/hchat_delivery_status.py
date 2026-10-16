"""Typed, presentation-only status notices for HChat delivery outcomes."""

from __future__ import annotations

import hashlib
import json
from typing import Any


STATUS_PREFIX = "[hashi hchat delivery status v1] "
DELIVERY_STATUS_STATES = frozenset(
    {
        "awaiting_confirmation",
        "executed",
        "displayed",
        "discarded",
        "confirmation_expired",
        "confirmation_superseded",
        "transport_expired",
        "capability_unsupported",
    }
)


def default_hchat_conversation_id(message_id: str) -> str:
    """Derive the stable default conversation used across HChat fallbacks."""

    normalized = str(message_id or "").strip()
    if not normalized:
        raise ValueError("HChat message identity is required")
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return f"conv-{digest[:24]}"


def delivery_status_message_id(original_message_id: str, state: str) -> str:
    """Return one stable wire-safe identity for an immutable status transition."""

    original = str(original_message_id or "").strip()
    normalized_state = str(state or "").strip().lower()
    if not original or normalized_state not in DELIVERY_STATUS_STATES:
        raise ValueError("invalid HChat delivery status identity")
    digest = hashlib.sha256(
        f"{original}\0{normalized_state}".encode("utf-8")
    ).hexdigest()
    return f"hstatus-{digest[:40]}"


def encode_delivery_status(
    *,
    original_message_id: str,
    state: str,
    reason: str | None = None,
) -> str:
    """Encode a compact status envelope inside text-compatible transports."""

    original = str(original_message_id or "").strip()
    normalized_state = str(state or "").strip().lower()
    if not original or len(original) > 256:
        raise ValueError("invalid original HChat message identity")
    if normalized_state not in DELIVERY_STATUS_STATES:
        raise ValueError("invalid HChat delivery status state")
    payload: dict[str, Any] = {
        "original_message_id": original,
        "state": normalized_state,
    }
    normalized_reason = str(reason or "").strip()
    if normalized_reason:
        if len(normalized_reason) > 128:
            raise ValueError("HChat delivery status reason is too long")
        payload["reason"] = normalized_reason
    return STATUS_PREFIX + json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )


def decode_delivery_status(text: str) -> dict[str, str] | None:
    """Decode a status envelope; malformed reserved envelopes fail closed."""

    value = str(text or "")
    if not value.startswith(STATUS_PREFIX):
        return None
    try:
        payload = json.loads(value[len(STATUS_PREFIX) :])
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid HChat delivery status envelope") from exc
    if not isinstance(payload, dict) or not set(payload).issubset(
        {"original_message_id", "state", "reason"}
    ):
        raise ValueError("invalid HChat delivery status envelope")
    original = str(payload.get("original_message_id") or "").strip()
    state = str(payload.get("state") or "").strip().lower()
    reason = str(payload.get("reason") or "").strip()
    if not original or len(original) > 256 or state not in DELIVERY_STATUS_STATES:
        raise ValueError("invalid HChat delivery status envelope")
    if len(reason) > 128:
        raise ValueError("invalid HChat delivery status envelope")
    result = {"original_message_id": original, "state": state}
    if reason:
        result["reason"] = reason
    return result


def delivery_status_display_text(status: dict[str, str]) -> str:
    """Render a truthful user-visible status without replaying message content."""

    state = str(status.get("state") or "").strip().lower()
    original = str(status.get("original_message_id") or "").strip()
    labels = {
        "awaiting_confirmation": "is waiting for receiver confirmation and has not been executed",
        "executed": "was confirmed and released for execution",
        "displayed": "was confirmed and displayed to the receiver",
        "discarded": "was discarded by the receiver and was not executed",
        "confirmation_expired": "was not confirmed within 15 minutes and expired without execution",
        "confirmation_superseded": "was superseded by newer session context and was not executed",
        "transport_expired": "expired in transport before delivery",
        "capability_unsupported": "was not sent because the peer lacks freshness protection",
    }
    if state not in labels or not original:
        raise ValueError("invalid HChat delivery status")
    return f"HChat delivery status ({original}): {labels[state]}."


def delivery_status_matches_outbound_correlation(
    correlation: dict[str, Any] | None,
    *,
    conversation_id: str,
    from_instance: str,
    from_agent: str,
    to_instance: str,
    to_agent: str,
) -> bool:
    """Check that a reverse status follows the exact original HChat route."""

    if not isinstance(correlation, dict):
        return False
    expected = {
        "conversation_id": conversation_id,
        "from_instance": to_instance,
        "from_agent": to_agent,
        "to_instance": from_instance,
        "to_agent": from_agent,
    }
    return all(
        str(correlation.get(key) or "").strip().casefold()
        == str(value or "").strip().casefold()
        for key, value in expected.items()
    )


__all__ = [
    "DELIVERY_STATUS_STATES",
    "STATUS_PREFIX",
    "default_hchat_conversation_id",
    "decode_delivery_status",
    "delivery_status_matches_outbound_correlation",
    "delivery_status_display_text",
    "delivery_status_message_id",
    "encode_delivery_status",
]
