from __future__ import annotations

from typing import Protocol


class Notifier(Protocol):
    def send(
        self,
        *,
        agent_id: str,
        text: str,
        run_id: str | None = None,
        workflow_id: str | None = None,
    ) -> None:
        """Send a notification outside the core engine."""


class NullNotifier:
    def send(
        self,
        *,
        agent_id: str,
        text: str,
        run_id: str | None = None,
        workflow_id: str | None = None,
    ) -> None:
        return None
