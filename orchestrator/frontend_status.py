"""Client-neutral projection of authoritative live Agent presentation state."""
from __future__ import annotations

from typing import Any

from orchestrator.flexible_backend_registry import HER_V2_ENGINE, public_backend_engine


def _optional_bool(runtime: Any, name: str) -> bool | None:
    value = getattr(runtime, name, None)
    return value if isinstance(value, bool) else None


def runtime_presentation_status(runtime: Any) -> dict[str, Any]:
    """Project current runtime facts without duplicating provider catalogues."""

    storage_engine = str(
        getattr(getattr(runtime, "config", None), "active_backend", "") or "unknown"
    )
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
        "engine": public_backend_engine(storage_engine),
        "model": str(model_value or "unknown"),
        "effort": None if effort is None else str(effort),
        "think": _optional_bool(runtime, "_think"),
        "verbose": _optional_bool(runtime, "_verbose"),
        "commentary": _optional_bool(runtime, "_commentary"),
    }
    if storage_engine != HER_V2_ENGINE:
        # Model Provider is an HER-owned routing fact.  Other Engines expose
        # only their Engine/model pair and never manufacture a provider label.
        return result

    manager = getattr(runtime, "backend_manager", None)
    backend = getattr(manager, "current_backend", None)
    config = getattr(backend, "_v2_config", None)
    profiles = getattr(config, "profiles", None)
    main = profiles.get("main") if isinstance(profiles, dict) else None
    if main is not None:
        provider = str(getattr(main, "engine", "") or "")
        model = str(getattr(main, "model", "") or "")
        if provider and model:
            result["her_v3"] = {"main": {"provider": provider, "model": model}}
    return result


__all__ = ["runtime_presentation_status"]
