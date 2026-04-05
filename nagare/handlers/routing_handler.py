"""
Nagare RoutingStepHandler — routes steps to callable or subprocess handlers
based on the `backend` field declared in the workflow.

backend: callable  → CallableStepHandler (in-process Python function)
everything else    → fallback_handler (SubprocessStepHandler by default)
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from nagare.handlers.callable_handler import CallableStepHandler, StepCallable
from nagare.logging.events import RunEventLogger

if TYPE_CHECKING:
    from nagare.engine.callable_setup_manager import CallableSetupManager


logger = logging.getLogger(__name__)


class RoutingStepHandler:
    """
    Routes step execution to the appropriate backend handler.

    Usage:
        from nagare.handlers import RoutingStepHandler, SubprocessStepHandler

        subprocess_handler = SubprocessStepHandler(run_id=run_id, ...)
        router = RoutingStepHandler(
            run_id=run_id,
            fallback_handler=subprocess_handler,
        )
        router.register_callable("mineru-extractor", my_extractor_fn)

        # Pass router as step_handler to FlowRunner
        runner = FlowRunner(workflow_path, step_handler=router)
    """

    def __init__(
        self,
        run_id: str,
        *,
        fallback_handler: Any,
        event_logger: RunEventLogger | None = None,
        runs_root: str | Path = "flow/runs",
        setup_manager: CallableSetupManager | None = None,
    ) -> None:
        self.run_id = run_id
        self._fallback = fallback_handler
        self._callable_handler = CallableStepHandler(
            run_id=run_id,
            event_logger=event_logger,
            runs_root=runs_root,
            setup_manager=setup_manager,
        )

    def register_callable(self, agent_id: str, fn: StepCallable) -> None:
        """Register a Python callable for a given agent_id."""
        self._callable_handler.register(agent_id, fn)

    def register_callable_force(self, agent_id: str, fn: StepCallable) -> None:
        """Register (or replace) a callable without raising on duplicates."""
        self._callable_handler._registry[agent_id] = fn

    def register_callables(self, mapping: dict[str, StepCallable]) -> None:
        """Register multiple callables at once."""
        self._callable_handler.register_many(mapping)

    def execute(
        self,
        agent_id: str,
        task_message: dict,
        agent_md_path: str,
        timeout_seconds: int = 600,
        backend: str = "claude-cli",
        model: str = "",
    ) -> dict:
        if backend == "callable":
            logger.debug("Routing %s → callable handler", agent_id)
            return self._callable_handler.execute(
                agent_id=agent_id,
                task_message=task_message,
                agent_md_path=agent_md_path,
                timeout_seconds=timeout_seconds,
                backend=backend,
                model=model,
            )

        logger.debug("Routing %s → fallback handler (backend=%s)", agent_id, backend)
        return self._fallback.execute(
            agent_id=agent_id,
            task_message=task_message,
            agent_md_path=agent_md_path,
            timeout_seconds=timeout_seconds,
            backend=backend,
            model=model,
        )
