"""Persistent clean-context boundaries for stateless agent backends.

``/fresh`` must preserve durable logs and searchable memory while preventing
anything recorded before the command from returning as implicit prompt
context.  This module owns the small workspace-state watermark used by the
independent history sources that cannot be cleared safely.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import time
from typing import Any, Mapping

from orchestrator.workspace_state import WorkspaceStateStore


STATE_KEY = "fresh_context"
STATE_VERSION = 1


def _state_store(runtime: Any) -> WorkspaceStateStore | Any | None:
    manager = getattr(runtime, "backend_manager", None)
    store = getattr(manager, "state_store", None)
    if store is not None and callable(getattr(store, "read", None)):
        return store
    workspace = getattr(runtime, "workspace_dir", None)
    if workspace is None:
        workspace = getattr(getattr(runtime, "config", None), "workspace_dir", None)
    if workspace is None:
        return None
    return WorkspaceStateStore(Path(workspace))


def _timestamp_epoch(value: Any) -> float:
    if isinstance(value, (int, float)):
        return max(0.0, float(value))
    text = str(value or "").strip()
    if not text:
        return 0.0
    try:
        return max(0.0, float(text))
    except (TypeError, ValueError):
        pass
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.astimezone()
        return max(0.0, parsed.timestamp())
    except (TypeError, ValueError, OSError, OverflowError):
        return 0.0


def state(runtime: Any) -> dict[str, Any]:
    store = _state_store(runtime)
    if store is None:
        return {}
    payload = store.read()
    block = payload.get(STATE_KEY) if isinstance(payload, Mapping) else None
    if (
        not isinstance(block, Mapping)
        or int(block.get("version") or 0) != STATE_VERSION
    ):
        return {}
    return dict(block)


def cutoff_epoch(runtime: Any) -> float:
    return _timestamp_epoch(state(runtime).get("cutoff_epoch"))


def workspace_cutoff_epoch(workspace_dir: str | Path) -> float:
    """Read the durable cutoff without requiring a fully constructed runtime."""

    payload = WorkspaceStateStore(Path(workspace_dir)).read()
    block = payload.get(STATE_KEY) if isinstance(payload, Mapping) else None
    if not isinstance(block, Mapping) or int(block.get("version") or 0) != STATE_VERSION:
        return 0.0
    return _timestamp_epoch(block.get("cutoff_epoch"))


def automatic_context_suppressed(runtime: Any) -> bool:
    return bool(state(runtime).get("automatic_context_suppressed"))


def start_boundary(runtime: Any, *, now_epoch: float | None = None) -> dict[str, Any]:
    """Persist a new boundary before mutable context caches are cleared."""

    store = _state_store(runtime)
    if store is None:
        raise OSError("workspace state store is unavailable")
    cutoff = float(time.time() if now_epoch is None else now_epoch)
    created_at = (
        datetime.fromtimestamp(cutoff, timezone.utc).isoformat().replace("+00:00", "Z")
    )

    def update(payload: dict[str, Any]) -> dict[str, Any]:
        previous = payload.get(STATE_KEY)
        previous = dict(previous) if isinstance(previous, Mapping) else {}
        payload[STATE_KEY] = {
            "version": STATE_VERSION,
            "generation": max(0, int(previous.get("generation") or 0)) + 1,
            "cutoff_epoch": cutoff,
            "created_at": created_at,
            # Context providers such as Memory+, dual-brain continuity, and
            # scheduler recovery cannot all expose a timestamped source view.
            # Keep them out until the user explicitly resumes memory context.
            "automatic_context_suppressed": True,
            # Habit retrieval is an HER-internal advisory source rather than a
            # pre-turn provider.  Fence it independently so /memory on does
            # not silently opt the user back into learned advice.
            "habit_context_suppressed": True,
        }
        return payload

    payload = store.update(update)
    block = payload.get(STATE_KEY)
    if not isinstance(block, Mapping):
        raise OSError("fresh context boundary was not persisted")
    return dict(block)


def resume_automatic_context(runtime: Any) -> bool:
    """Allow explicit memory controls to resume non-timeline context sources."""

    store = _state_store(runtime)
    if store is None:
        return False

    def update(payload: dict[str, Any]) -> dict[str, Any]:
        block = payload.get(STATE_KEY)
        if isinstance(block, Mapping):
            updated = dict(block)
            updated["automatic_context_suppressed"] = False
            payload[STATE_KEY] = updated
        return payload

    payload = store.update(update)
    block = payload.get(STATE_KEY)
    return isinstance(block, Mapping) and not bool(
        block.get("automatic_context_suppressed")
    )


def habit_context_suppressed(runtime: Any) -> bool:
    return bool(state(runtime).get("habit_context_suppressed"))


def resume_habit_context(runtime: Any) -> bool:
    store = _state_store(runtime)
    if store is None:
        return False

    def update(payload: dict[str, Any]) -> dict[str, Any]:
        block = payload.get(STATE_KEY)
        if isinstance(block, Mapping):
            updated = dict(block)
            updated["habit_context_suppressed"] = False
            payload[STATE_KEY] = updated
        return payload

    payload = store.update(update)
    block = payload.get(STATE_KEY)
    return isinstance(block, Mapping) and not bool(
        block.get("habit_context_suppressed")
    )


def entry_is_after_boundary(runtime: Any, value: Any) -> bool:
    cutoff = cutoff_epoch(runtime)
    if cutoff <= 0:
        return True
    epoch = _timestamp_epoch(value)
    return epoch >= cutoff if epoch > 0 else False


def item_predates_boundary(runtime: Any, item: Any) -> bool:
    cutoff = cutoff_epoch(runtime)
    if cutoff <= 0:
        return False
    created = _timestamp_epoch(
        item.get("created_at")
        if isinstance(item, Mapping)
        else getattr(item, "created_at", None)
    )
    return bool(created > 0 and created < cutoff)


__all__ = [
    "STATE_KEY",
    "automatic_context_suppressed",
    "cutoff_epoch",
    "entry_is_after_boundary",
    "habit_context_suppressed",
    "item_predates_boundary",
    "resume_automatic_context",
    "resume_habit_context",
    "start_boundary",
    "state",
    "workspace_cutoff_epoch",
]
