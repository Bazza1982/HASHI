"""Safe API failure presentation; execution facts remain with the operation owner."""
from __future__ import annotations

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
