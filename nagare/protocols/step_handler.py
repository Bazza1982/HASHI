from __future__ import annotations

from typing import Protocol


class StepHandler(Protocol):
    def execute(
        self,
        agent_id: str,
        task_message: dict,
        agent_md_path: str,
        timeout_seconds: int = 600,
        backend: str = "claude-cli",
        model: str = "",
    ) -> dict:
        """Execute one workflow step and return a structured result."""
