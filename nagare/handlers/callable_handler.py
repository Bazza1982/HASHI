"""
Nagare callable step handler — execute Python functions directly, no subprocess.

This handler enables Nagare to orchestrate in-process Python callables
(e.g. Veritas adapters, Kasumi pipelines) alongside subprocess-based
agent steps. Register callables by agent_id, and the handler will
invoke them directly when a workflow step targets that agent.
"""

from __future__ import annotations

import logging
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from nagare.logging.events import RunEventLogger


logger = logging.getLogger(__name__)


# Type alias for registered callables.
# Signature: (task_message: dict) -> dict
StepCallable = Callable[[dict], dict]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class CallableStepHandler:
    """
    In-process step handler that invokes registered Python callables.

    Usage:
        handler = CallableStepHandler(run_id="run-001")
        handler.register("pdf_extract", my_pdf_function)
        handler.register("classify", my_classifier)

        result = handler.execute(
            agent_id="pdf_extract",
            task_message={"run_id": "run-001", "payload": {...}},
            agent_md_path="",  # unused for callables
        )

    The callable receives the full task_message dict and must return a dict
    with at minimum {"status": "completed"|"failed", ...}.
    """

    def __init__(
        self,
        run_id: str,
        *,
        event_logger: RunEventLogger | None = None,
        runs_root: str | Path = "flow/runs",
    ) -> None:
        self.run_id = run_id
        self._event_logger = event_logger
        self.runs_root = Path(runs_root)
        self._registry: dict[str, StepCallable] = {}

    def register(self, agent_id: str, fn: StepCallable) -> None:
        """Register a Python callable for a given agent_id."""
        if agent_id in self._registry:
            raise ValueError(f"agent_id '{agent_id}' is already registered")
        self._registry[agent_id] = fn
        logger.debug("Registered callable for agent_id=%s", agent_id)

    def register_many(self, mapping: dict[str, StepCallable]) -> None:
        """Register multiple callables at once."""
        for agent_id, fn in mapping.items():
            self.register(agent_id, fn)

    def execute(
        self,
        agent_id: str,
        task_message: dict,
        agent_md_path: str,
        timeout_seconds: int = 600,
        backend: str = "callable",
        model: str = "",
    ) -> dict:
        """Execute a registered callable. Conforms to StepHandler protocol."""
        del agent_md_path, timeout_seconds, backend, model  # unused

        fn = self._registry.get(agent_id)
        if fn is None:
            available = list(self._registry.keys())
            error_msg = (
                f"No callable registered for agent_id='{agent_id}'. "
                f"Available: {available}"
            )
            logger.error(error_msg)
            self._emit("callable_not_found", agent_id=agent_id, error=error_msg)
            return {"status": "failed", "error": error_msg}

        step_id = task_message.get("payload", {}).get("step_id", "unknown")
        self._emit(
            "callable_start",
            agent_id=agent_id,
            step_id=step_id,
        )

        t0 = time.monotonic()
        try:
            result = fn(task_message)
            elapsed = time.monotonic() - t0

            if not isinstance(result, dict):
                error_msg = (
                    f"Callable for '{agent_id}' returned {type(result).__name__}, "
                    f"expected dict"
                )
                self._emit(
                    "callable_error",
                    agent_id=agent_id,
                    step_id=step_id,
                    error=error_msg,
                    elapsed_s=round(elapsed, 3),
                )
                return {"status": "failed", "error": error_msg}

            status = result.get("status", "completed")
            self._emit(
                "callable_complete",
                agent_id=agent_id,
                step_id=step_id,
                status=status,
                elapsed_s=round(elapsed, 3),
            )
            return result

        except Exception as exc:
            elapsed = time.monotonic() - t0
            error_msg = f"{type(exc).__name__}: {exc}"
            tb = traceback.format_exc()
            logger.error(
                "Callable %s raised: %s\n%s", agent_id, error_msg, tb
            )
            self._emit(
                "callable_error",
                agent_id=agent_id,
                step_id=step_id,
                error=error_msg,
                elapsed_s=round(elapsed, 3),
            )
            return {
                "status": "failed",
                "error": error_msg,
                "traceback": tb,
            }

    def _emit(self, event_type: str, **kwargs: Any) -> None:
        if self._event_logger is None:
            return
        self._event_logger.emit(event_type, **kwargs)
