"""Client-neutral projection of authoritative live Agent presentation state."""
from __future__ import annotations

from typing import Any

from orchestrator.flexible_backend_registry import HER_V2_ENGINE


def _optional_bool(runtime: Any, name: str) -> bool | None:
    value = getattr(runtime, name, None)
    return value if isinstance(value, bool) else None


def runtime_presentation_status(runtime: Any) -> dict[str, Any]:
    """Project current runtime facts without duplicating provider catalogues."""

    engine = str(getattr(getattr(runtime, "config", None), "active_backend", "") or "unknown")
    get_model = getattr(runtime, "get_current_model", None)
    get_effort = getattr(runtime, "_get_current_effort", None)
    try:
        model_value = get_model() if callable(get_model) else None
    except (AttributeError, RuntimeError, TypeError, ValueError):
        model_value = None
    try:
        effort = get_effort() if callable(get_effort) else None
    except (AttributeError, RuntimeError, TypeError, ValueError):
        effort = None
    result: dict[str, Any] = {
        "schema_version": 1,
        "source": "live_runtime",
        "engine": engine,
        "model": str(model_value or "unknown"),
        "effort": None if effort is None else str(effort),
        "think": _optional_bool(runtime, "_think"),
        "verbose": _optional_bool(runtime, "_verbose"),
        "commentary": _optional_bool(runtime, "_commentary"),
    }
    if engine != HER_V2_ENGINE:
        # Model Provider is an HER-owned routing fact.  Other Engines expose
        # only their Engine/model pair and never manufacture a provider label.
        return result

    manager = getattr(runtime, "backend_manager", None)
    getter = getattr(manager, "get_her_v2_configuration", None)
    if not callable(getter):
        return result
    try:
        selected = getter()
        quick = selected.target_for_slot("quick")
        pro = selected.target_for_slot("pro")
    except (AttributeError, RuntimeError, TypeError, ValueError):
        return result
    result["her_v2"] = {
        "routing_mode": str(selected.routing_mode),
        "quick": {"provider": str(quick.provider), "model": str(quick.model)},
        "pro": {"provider": str(pro.provider), "model": str(pro.model)},
        "routing_revision": int(selected.routing_revision),
    }
    return result


__all__ = ["runtime_presentation_status"]
