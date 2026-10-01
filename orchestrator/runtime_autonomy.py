"""Agent-scoped, persistent /stop fence for autonomous wakeups.

An explicit later user request resumes automation. The generation also rejects
admissions that began before a concurrent /stop, even if a user has resumed by
the time they reach the ready queue.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from orchestrator.workspace_state import WorkspaceStateStore


STATE_KEY = "autonomous_wakeup"
AUTONOMOUS_SOURCES = frozenset(
    {
        "scheduler",
        "scheduler-retry",
        "scheduler-recovery",
        "scheduler-skill",
        "heartbeat",
        "cron",
        "proactive",
        "background:prompt",
        "background-job-event",
        "background_job_event",
        "hchat",
        "hchat-exchange",
        "startup",
        "system",
        "session_reset",
    }
)


def _store(runtime: Any) -> WorkspaceStateStore | None:
    store = getattr(getattr(runtime, "backend_manager", None), "state_store", None)
    if isinstance(store, WorkspaceStateStore):
        return store
    workspace = getattr(runtime, "workspace_dir", None)
    return WorkspaceStateStore(Path(workspace)) if workspace is not None else None


def _block(value: Any) -> dict[str, Any]:
    raw = value if isinstance(value, Mapping) else {}
    return {
        "paused": raw.get("paused") is True,
        "generation": max(0, int(raw.get("generation") or 0)),
    }


def status(runtime: Any) -> dict[str, Any]:
    store = _store(runtime)
    if store is None:
        current = _block(getattr(runtime, "_autonomous_wakeup_state", None))
    else:
        try:
            current = _block(store.read_strict().get(STATE_KEY))
        except (OSError, ValueError, TypeError):
            if not getattr(runtime, "_autonomous_wakeup_force_paused", False):
                raise
            current = {"paused": True, "generation": 0}
    if getattr(runtime, "_autonomous_wakeup_force_paused", False):
        current["paused"] = True
        current["generation"] = max(
            current["generation"],
            int(getattr(runtime, "_autonomous_wakeup_local_generation", 0)),
        )
    return current


def _publish(runtime: Any, *, paused: bool, advance_generation: bool) -> dict[str, Any]:
    def mutate(state: dict[str, Any]) -> dict[str, Any]:
        previous = _block(state.get(STATE_KEY))
        state[STATE_KEY] = {
            "paused": paused,
            "generation": previous["generation"] + int(advance_generation),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        return state

    store = _store(runtime)
    if store is None:
        state = mutate({STATE_KEY: getattr(runtime, "_autonomous_wakeup_state", {})})
        runtime._autonomous_wakeup_state = state[STATE_KEY]
        return _block(state[STATE_KEY])
    return _block(store.update(mutate).get(STATE_KEY))


def pause(runtime: Any) -> dict[str, Any]:
    try:
        previous_generation = status(runtime)["generation"]
    except (OSError, ValueError, TypeError):
        previous_generation = 0
    runtime._autonomous_wakeup_local_generation = previous_generation + 1
    runtime._autonomous_wakeup_force_paused = True
    published = _publish(runtime, paused=True, advance_generation=True)
    runtime._autonomous_wakeup_force_paused = False
    return published


def resume(runtime: Any) -> dict[str, Any]:
    current = status(runtime)
    if not current["paused"]:
        return current
    published = _publish(
        runtime,
        paused=False,
        advance_generation=bool(getattr(runtime, "_autonomous_wakeup_force_paused", False)),
    )
    runtime._autonomous_wakeup_force_paused = False
    return published


def is_autonomous(source: str, metadata: Mapping[str, Any] | None = None) -> bool:
    normalized = str(source or "").strip().casefold()
    return bool(
        (metadata or {}).get("_hashi_autonomous_wakeup")
        or normalized in AUTONOMOUS_SOURCES
        or normalized.startswith(("scheduler:", "cron:", "heartbeat:", "proactive:", "bridge:"))
    )


def admission_snapshot(
    runtime: Any, source: str, metadata: Mapping[str, Any] | None = None
) -> tuple[bool, int]:
    """Return admission and stop generation; unreadable state fails closed."""
    try:
        current = status(runtime)
        if is_autonomous(source, metadata):
            return not current["paused"], current["generation"]
        current = resume(runtime) if current["paused"] else current
        return True, current["generation"]
    except (OSError, ValueError, TypeError):
        return False, -1


def admission_is_current(runtime: Any, generation: int) -> bool:
    try:
        current = status(runtime)
        return not current["paused"] and current["generation"] == generation
    except (OSError, ValueError, TypeError):
        return False


def lock_for(runtime: Any) -> asyncio.Lock:
    lock = getattr(runtime, "_autonomous_wakeup_lock", None)
    if lock is None:
        lock = asyncio.Lock()
        runtime._autonomous_wakeup_lock = lock
    return lock
