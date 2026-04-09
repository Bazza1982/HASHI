from __future__ import annotations

from typing import Any, Protocol


class Evaluator(Protocol):
    def evaluate_run(self, run_id: str) -> dict[str, Any] | None:
        """Evaluate a completed run and optionally return a report."""


class NullEvaluator:
    def evaluate_run(self, run_id: str) -> dict[str, Any] | None:
        return None
