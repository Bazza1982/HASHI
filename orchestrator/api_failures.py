"""Safe API failure presentation; execution facts remain with the operation owner."""
from __future__ import annotations

import re
from typing import Any, Mapping

from orchestrator import ui_language
from orchestrator.operation_failures import OperationFailure


def operation_failure_payload(failure: OperationFailure, *, error_code: str,
                              effect_state: str, locale: str | None = None,
                              observed: Mapping[str, Any] | None = None) -> dict[str, Any]:
    if effect_state not in {"not_started", "not_requested", "unconfirmed"}:
        raise ValueError("invalid operation effect facts")
    observed = dict(observed or {})
    key = "api.failure." + effect_state
    if observed.get("agent_created") is True:
        key = "api.failure.already_created"
    message = ui_language.tr(key, locale=locale) + "\n" + failure.render(locale=locale)
    return {
        "ok": False, "error_code": error_code, "cause_code": failure.error_code,
        "error": message, "message": message,
        "diagnostic_ref": failure.reference, "failure_phase": failure.phase,
        "state": "unknown" if effect_state == "unconfirmed" else "failed",
        "accepted": None if effect_state == "unconfirmed" else False,
        "effect_state": effect_state, "retry_safe": False,
        "failure": failure.diagnostic(), **({"observed": observed} if observed else {}),
    }


def public_operation_failure_message(payload: Any) -> str | None:
    """Read only the bounded, correlated public DTO; never exception/body text."""
    if not isinstance(payload, dict) or payload.get("ok") is not False:
        return None
    reference, message = payload.get("diagnostic_ref"), payload.get("message")
    if (not isinstance(reference, str) or not re.fullmatch(r"(?:operr|fcerr)_[a-f0-9]{24}", reference)
        or not isinstance(message, str) or not message.strip() or len(message) > 2800
        or reference not in message or any(ord(char) < 32 and char not in "\t\n\r" or ord(char) == 127 for char in message)
        or payload.get("retry_safe") is not False):
        return None
    for field in ("cause_code", "failure_phase"):
        value = payload.get(field)
        if not isinstance(value, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,100}", value):
            return None
    effect = payload.get("effect_state")
    if effect not in {"not_started", "not_requested", "unconfirmed"}:
        return None
    if (payload.get("accepted") is not None if effect == "unconfirmed" else payload.get("accepted") is not False):
        return None
    return message.strip()
