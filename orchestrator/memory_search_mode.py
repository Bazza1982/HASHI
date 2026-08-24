from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from orchestrator.workspace_state import WorkspaceStateStore


STATE_KEY = "memory_search"


def is_memory_search_enabled(workspace_dir: str | Path) -> bool:
    """Return the persisted per-turn long-term-memory search preference.

    Absence is deliberately OFF.  Retrieval is optional enrichment, not a
    prerequisite for normal conversational continuity.
    """

    state = WorkspaceStateStore(Path(workspace_dir)).read()
    block = state.get(STATE_KEY)
    if not isinstance(block, Mapping):
        return False
    return bool(block.get("enabled", False))


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


def apply_memory_search_preference(assembler: Any, workspace_dir: str | Path) -> bool:
    """Apply the persisted setting to a newly-created context assembler."""

    enabled = is_memory_search_enabled(workspace_dir)
    assembler.saved_memory_injection_enabled = enabled
    return enabled
