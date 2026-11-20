from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from orchestrator.workspace_state import WorkspaceStateStore


STATE_KEY = "memory_search"
TURNS_STATE_KEY = "memory_turns"
SYNC_STATE_KEY = "memory_sync"


def is_memory_search_enabled(workspace_dir: str | Path) -> bool:
    """Return the persisted per-turn long-term-memory search preference.

    Long-term memory search defaults to ON for normal private Agents so saved
    memories are usable out of the box without hidden opt-in switches. An
    explicit persisted ``enabled: false`` disables retrieval across restarts.
    """

    state = WorkspaceStateStore(Path(workspace_dir)).read()
    block = state.get(STATE_KEY)
    if not isinstance(block, Mapping):
        return True
    return bool(block.get("enabled", True))


def get_memory_sync_preference(workspace_dir: str | Path) -> bool | None:
    """Return explicit BGE memory-sync opt-in/opt-out, or ``None`` when unset."""

    state = WorkspaceStateStore(Path(workspace_dir)).read()
    block = state.get(SYNC_STATE_KEY)
    if not isinstance(block, Mapping) or "enabled" not in block:
        return None
    return bool(block.get("enabled"))


def set_memory_sync_preference(
    workspace_dir: str | Path,
    enabled: bool,
) -> bool:
    """Persist the user's BGE memory-sync setting."""

    normalized = bool(enabled)

    def update(state: dict[str, Any]) -> dict[str, Any]:
        state[SYNC_STATE_KEY] = {
            "version": 1,
            "enabled": normalized,
            "updated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }
        return state

    WorkspaceStateStore(Path(workspace_dir)).update(update)
    return normalized


def set_memory_search_enabled(
    workspace_dir: str | Path,
    enabled: bool,
) -> bool:
    """Persist the user's memory-search setting and return its normalized value."""

    normalized = bool(enabled)

    def update(state: dict[str, Any]) -> dict[str, Any]:
        state[STATE_KEY] = {
            "version": 1,
            "enabled": normalized,
            "updated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }
        return state

    WorkspaceStateStore(Path(workspace_dir)).update(update)
    return normalized


def is_memory_turns_enabled(workspace_dir: str | Path) -> bool:
    """Return the persisted recent-turn injection preference.

    Recent turns historically defaulted to ON.  Keeping that default makes
    existing Agent workspaces backward compatible while allowing an explicit
    pause to survive every Session boundary and process replacement.
    """

    state = WorkspaceStateStore(Path(workspace_dir)).read()
    block = state.get(TURNS_STATE_KEY)
    if not isinstance(block, Mapping):
        return True
    return bool(block.get("enabled", True))


def set_memory_turns_enabled(
    workspace_dir: str | Path,
    enabled: bool,
) -> bool:
    """Persist the recent-turn injection preference independently."""

    normalized = bool(enabled)

    def update(state: dict[str, Any]) -> dict[str, Any]:
        state[TURNS_STATE_KEY] = {
            "version": 1,
            "enabled": normalized,
            "updated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }
        return state

    WorkspaceStateStore(Path(workspace_dir)).update(update)
    return normalized


def apply_memory_search_preference(assembler: Any, workspace_dir: str | Path) -> bool:
    """Apply the persisted setting to a newly-created context assembler."""

    enabled = is_memory_search_enabled(workspace_dir)
    assembler.saved_memory_injection_enabled = enabled
    return enabled


def apply_memory_injection_preferences(
    assembler: Any, workspace_dir: str | Path
) -> dict[str, bool]:
    """Reapply both independent user preferences to a context assembler."""

    turns_enabled = is_memory_turns_enabled(workspace_dir)
    saved_enabled = is_memory_search_enabled(workspace_dir)
    assembler.turns_injection_enabled = turns_enabled
    assembler.saved_memory_injection_enabled = saved_enabled
    return {"turns": turns_enabled, "saved": saved_enabled}
