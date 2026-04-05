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
from typing import TYPE_CHECKING, Any, Callable, Optional

from nagare.logging.events import RunEventLogger

if TYPE_CHECKING:
    from nagare.engine.callable_setup_manager import CallableSetupManager


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
        setup_manager: CallableSetupManager | None = None,
    ) -> None:
        self.run_id = run_id
        self._event_logger = event_logger
        self.runs_root = Path(runs_root)
        self._registry: dict[str, StepCallable] = {}
        self._setup_manager = setup_manager

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

        step_id = task_message.get("payload", {}).get("step_id", "unknown")

        # Auto-setup loop: if callable is missing and setup_manager is wired,
        # ask AI to implement it (up to MAX_RETRIES times) before giving up.
        if agent_id not in self._registry and self._setup_manager is not None:
            return self._execute_with_auto_setup(agent_id, step_id, task_message)

        if agent_id not in self._registry:
            available = list(self._registry.keys())
            error_msg = (
                f"No callable registered for agent_id='{agent_id}'. "
                f"Available: {available}"
            )
            logger.error(error_msg)
            self._emit("callable_not_found", agent_id=agent_id, error=error_msg)
            return {"status": "failed", "error": error_msg}

        return self._invoke(agent_id, step_id, task_message)

    def _execute_with_auto_setup(
        self,
        agent_id: str,
        step_id: str,
        task_message: dict,
    ) -> dict:
        """
        Retry loop that asks the AI to implement the callable, waits for code
        delivery, and re-executes. Escalates to human after MAX_RETRIES.
        """
        from nagare.engine.callable_setup_manager import MAX_RETRIES

        self._emit("callable_not_found", agent_id=agent_id, step_id=step_id)
        mgr = self._setup_manager  # guaranteed non-None by caller

        for attempt in range(1, MAX_RETRIES + 1):
            logger.info(
                "Callable auto-setup attempt %d/%d for agent_id='%s'",
                attempt,
                MAX_RETRIES,
                agent_id,
            )
            self._emit(
                "callable_setup_attempt",
                agent_id=agent_id,
                step_id=step_id,
                attempt=attempt,
                max_retries=MAX_RETRIES,
            )

            event = mgr.request_setup(agent_id, task_message, attempt=attempt)
            delivered = mgr.wait_for_setup(agent_id, event)

            if not delivered:
                self._emit(
                    "callable_setup_timeout",
                    agent_id=agent_id,
                    step_id=step_id,
                    attempt=attempt,
                )
                mgr.increment_retry(agent_id)
                continue

            fn = mgr.pop_pending_callable(agent_id)
            if fn is None:
                # Shouldn't happen — event fired but no fn stored
                mgr.increment_retry(agent_id)
                continue

            # Register (force, bypassing duplicate guard) and try executing
            self._registry[agent_id] = fn
            logger.info("Callable '%s' registered from auto-setup, executing…", agent_id)

            result = self._invoke(agent_id, step_id, task_message)
            if result.get("status") != "failed":
                self._emit(
                    "callable_setup_success",
                    agent_id=agent_id,
                    step_id=step_id,
                    attempt=attempt,
                )
                return result

            # Callable ran but returned failed — remove so next attempt re-delivers
            del self._registry[agent_id]
            mgr.increment_retry(agent_id)
            self._emit(
                "callable_setup_bad_result",
                agent_id=agent_id,
                step_id=step_id,
                attempt=attempt,
                error=result.get("error", ""),
            )

        # All retries exhausted
        logger.error(
            "Callable auto-setup exhausted %d attempts for agent_id='%s', escalating",
            MAX_RETRIES,
            agent_id,
        )
        mgr.escalate_to_human(agent_id, task_message)
        self._emit(
            "callable_setup_exhausted",
            agent_id=agent_id,
            step_id=step_id,
            max_retries=MAX_RETRIES,
        )
        return {
            "status": "failed",
            "error": (
                f"Callable auto-setup failed after {MAX_RETRIES} attempts "
                f"for agent_id='{agent_id}'. Human escalation sent."
            ),
        }

    def _invoke(self, agent_id: str, step_id: str, task_message: dict) -> dict:
        """Execute a callable that is already in self._registry."""
        fn = self._registry[agent_id]

        self._emit("callable_start", agent_id=agent_id, step_id=step_id)
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
            logger.error("Callable %s raised: %s\n%s", agent_id, error_msg, tb)
            self._emit(
                "callable_error",
                agent_id=agent_id,
                step_id=step_id,
                error=error_msg,
                elapsed_s=round(elapsed, 3),
            )
            return {"status": "failed", "error": error_msg, "traceback": tb}

    def _emit(self, event_type: str, **kwargs: Any) -> None:
        if self._event_logger is None:
            return
        self._event_logger.emit(event_type, **kwargs)
