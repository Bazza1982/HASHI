"""
Per-chat routing state for the WhatsApp transport.

Tracks which agent(s) each WhatsApp chat is currently routed to.
State is persisted to a JSON file so it survives restarts.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from orchestrator.config_json import (
    ConfigDocument,
    new_config_json,
    read_config_json,
    write_config_json,
)

logger = logging.getLogger("WhatsApp")


class ChatRouterPersistenceError(RuntimeError):
    """A route change was not published and was rolled back in memory."""


@dataclass
class ChatRoute:
    mode: str              # "single" | "group" | "broadcast"
    agents: list[str] = field(default_factory=list)


class ChatRouter:
    """Maps WhatsApp chat JIDs to routing targets, with file persistence."""

    def __init__(self, state_path: Path | None = None):
        self._routes: dict[str, ChatRoute] = {}
        self._state_path = state_path
        self._document: ConfigDocument | None = None
        if state_path is not None:
            self._load()

    # --- mutations (all auto-save) ---

    def set_single(self, chat_id: str, agent: str) -> None:
        self._set(chat_id, ChatRoute(mode="single", agents=[agent]))

    def set_group(self, chat_id: str, agents: list[str]) -> None:
        self._set(chat_id, ChatRoute(mode="group", agents=list(agents)))

    def set_broadcast(self, chat_id: str, all_agents: list[str]) -> None:
        self._set(chat_id, ChatRoute(mode="broadcast", agents=list(all_agents)))

    def clear(self, chat_id: str) -> None:
        previous = self._routes.pop(chat_id, None)
        try:
            self._save(chat_id, None)
        except Exception:
            if previous is not None:
                self._routes[chat_id] = previous
            raise

    def _set(self, chat_id: str, route: ChatRoute) -> None:
        previous = self._routes.get(chat_id)
        self._routes[chat_id] = route
        try:
            self._save(chat_id, route)
        except Exception:
            if previous is None:
                self._routes.pop(chat_id, None)
            else:
                self._routes[chat_id] = previous
            raise

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

    def _save(self, chat_id: str, route: ChatRoute | None):
        if self._state_path is None:
            return
        missing = object()
        previous = (
            self._document.get(chat_id, missing)
            if self._document is not None
            else missing
        )
        try:
            if self._document is None:
                raise ValueError(
                    "routing state is unreadable; refusing replacement"
                )
            if route is None:
                self._document.pop(chat_id, None)
            else:
                self._document[chat_id] = {
                    "mode": route.mode,
                    "agents": list(route.agents),
                }
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            write_config_json(self._state_path, self._document)
        except Exception as e:
            if self._document is not None:
                if previous is missing:
                    self._document.pop(chat_id, None)
                else:
                    self._document[chat_id] = previous
            logger.warning("Failed to save WhatsApp routing state: %s", e)
            raise ChatRouterPersistenceError(
                "WhatsApp routing state was not saved"
            ) from e

    def _load(self):
        if self._state_path is None:
            return
        if not self._state_path.exists():
            self._document = new_config_json(self._state_path)
            return
        try:
            data = read_config_json(self._state_path)
            self._document = data
            for chat_id, info in data.items():
                if not isinstance(info, dict):
                    continue
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
