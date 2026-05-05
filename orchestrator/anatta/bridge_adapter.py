from __future__ import annotations

from pathlib import Path
from typing import Any


class BridgeMemoryAdapter:
    def __init__(self, bridge_memory_store: Any):
        self.bridge_memory_store = bridge_memory_store
        self.db_path: Path | None = getattr(bridge_memory_store, "db_path", None)

    def get_recent_turns(self, limit: int) -> list[dict[str, Any]]:
        if not hasattr(self.bridge_memory_store, "get_recent_turns"):
            return []
        try:
            return list(self.bridge_memory_store.get_recent_turns(limit=limit) or [])
        except Exception:
            return []

    def retrieve_memories(self, query: str, limit: int) -> list[dict[str, Any]]:
        if not hasattr(self.bridge_memory_store, "retrieve_memories"):
            return []
        try:
            return list(self.bridge_memory_store.retrieve_memories(query, limit=limit) or [])
        except Exception:
            return []

    def resolve_bridge_row(self, request_id: str | None) -> tuple[str, int] | None:
        # Placeholder: current BridgeMemoryStore does not expose request_id -> row id resolution.
        # When runtime integration lands, we can pass known turn ids directly.
        return None
