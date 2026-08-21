from __future__ import annotations

from typing import Any, Mapping

from adapters.timeout_policy import (
    IDLE_TIMEOUT_KEY,
    parse_positive_timeout,
)
from orchestrator.workspace_state import WorkspaceStateStore


BACKEND_TIMEOUT_STATE_KEY = "backend_timeouts"


def timeout_override_from_state(state: Mapping[str, Any], engine: str) -> dict[str, int]:
    all_overrides = state.get(BACKEND_TIMEOUT_STATE_KEY)
    if all_overrides is None:
        return {}
    if not isinstance(all_overrides, Mapping):
        raise ValueError(f"{BACKEND_TIMEOUT_STATE_KEY} must be an object")
    raw = all_overrides.get(engine)
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise ValueError(f"timeout override for {engine} must be an object")

    override: dict[str, int] = {}
    for key in (IDLE_TIMEOUT_KEY,):
        if key in raw:
            override[key] = parse_positive_timeout(raw[key], label=key.replace("_", " "))
    return override


def read_timeout_override(store: WorkspaceStateStore, engine: str) -> dict[str, int]:
    return timeout_override_from_state(store.read(), engine)


def set_timeout_override(
    store: WorkspaceStateStore,
    engine: str,
    *,
    idle_seconds: int | None = None,
) -> dict[str, int]:
    if idle_seconds is None:
        raise ValueError("idle timeout value is required")
    normalized_idle = (
        parse_positive_timeout(idle_seconds, label="idle timeout")
        if idle_seconds is not None
        else None
    )
    saved: dict[str, int] = {}

    def mutate(state: dict[str, Any]) -> dict[str, Any]:
        nonlocal saved
        raw_all = state.get(BACKEND_TIMEOUT_STATE_KEY)
        all_overrides = dict(raw_all) if isinstance(raw_all, Mapping) else {}
        raw_current = all_overrides.get(engine)
        current = dict(raw_current) if isinstance(raw_current, Mapping) else {}
        if normalized_idle is not None:
            current[IDLE_TIMEOUT_KEY] = normalized_idle
        # Permanently migrate away any historical wall-clock override.
        current.pop("hard_timeout_sec", None)
        saved = timeout_override_from_state(
            {BACKEND_TIMEOUT_STATE_KEY: {engine: current}},
            engine,
        )
        all_overrides[engine] = saved
        state[BACKEND_TIMEOUT_STATE_KEY] = all_overrides
        return state

    store.update(mutate)
    return saved


def clear_timeout_override(store: WorkspaceStateStore, engine: str) -> None:
    def mutate(state: dict[str, Any]) -> dict[str, Any]:
        raw_all = state.get(BACKEND_TIMEOUT_STATE_KEY)
        if not isinstance(raw_all, Mapping):
            state.pop(BACKEND_TIMEOUT_STATE_KEY, None)
            return state
        all_overrides = dict(raw_all)
        all_overrides.pop(engine, None)
        if all_overrides:
            state[BACKEND_TIMEOUT_STATE_KEY] = all_overrides
        else:
            state.pop(BACKEND_TIMEOUT_STATE_KEY, None)
        return state

    store.update(mutate)
