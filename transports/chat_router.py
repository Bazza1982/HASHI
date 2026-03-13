from __future__ import annotations
"""
Per-chat routing state for the WhatsApp transport.

Tracks which agent(s) each WhatsApp chat is currently routed to.
State is persisted to a JSON file so it survives restarts.
"""

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("WhatsApp")


@dataclass
class ChatRoute:
    mode: str              # "single" | "group" | "broadcast"
    agents: list[str] = field(default_factory=list)


class ChatRouter:
    """Maps WhatsApp chat JIDs to routing targets, with file persistence."""

    def __init__(self, state_path: Path | None = None):
        self._routes: dict[str, ChatRoute] = {}
        self._state_path = state_path
        if state_path is not None:
            self._load()

    # --- mutations (all auto-save) ---

    def set_single(self, chat_id: str, agent: str) -> None:
        self._routes[chat_id] = ChatRoute(mode="single", agents=[agent])
        self._save()

    def set_group(self, chat_id: str, agents: list[str]) -> None:
        self._routes[chat_id] = ChatRoute(mode="group", agents=list(agents))
        self._save()

    def set_broadcast(self, chat_id: str, all_agents: list[str]) -> None:
        self._routes[chat_id] = ChatRoute(mode="broadcast", agents=list(all_agents))
        self._save()

    def clear(self, chat_id: str) -> None:
        self._routes.pop(chat_id, None)
        self._save()

    # --- reads ---

    def get_targets(self, chat_id: str) -> list[str]:
        route = self._routes.get(chat_id)
        return list(route.agents) if route else []

    def get_mode(self, chat_id: str) -> str:
        route = self._routes.get(chat_id)
        return route.mode if route else "none"

    def get_route(self, chat_id: str) -> ChatRoute | None:
        return self._routes.get(chat_id)

    # --- persistence ---

    def _save(self):
        if self._state_path is None:
            return
        try:
            data = {
                chat_id: {"mode": route.mode, "agents": route.agents}
                for chat_id, route in self._routes.items()
            }
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            self._state_path.write_text(
                json.dumps(data, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning("Failed to save WhatsApp routing state: %s", e)

    def _load(self):
        if self._state_path is None or not self._state_path.exists():
            return
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
            for chat_id, info in data.items():
                mode = info.get("mode", "single")
                agents = info.get("agents", [])
                if agents:
                    self._routes[chat_id] = ChatRoute(mode=mode, agents=agents)
            logger.info(
                "Restored WhatsApp routing state: %d chat(s)",
                len(self._routes),
            )
        except Exception as e:
            logger.warning("Failed to load WhatsApp routing state: %s", e)
