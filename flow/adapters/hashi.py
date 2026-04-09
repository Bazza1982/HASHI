"""HASHI-specific adapters around the extracted Nagare core protocols."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable

from nagare.handlers.subprocess_handler import SubprocessStepHandler
from nagare.logging.events import RunEventLogger


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RUNS_ROOT = ROOT / "flow" / "runs"


class HASHIStepHandler:
    """Wrap HASHI worker dispatch with adapter-scoped correlation logging."""

    def __init__(
        self,
        delegate: Any | None = None,
        *,
        run_id: str | None = None,
        repo_root: str | Path = ROOT,
        runs_root: str | Path = DEFAULT_RUNS_ROOT,
        event_logger: RunEventLogger | None = None,
        workflow_id: str | None = None,
        trace_id: str | None = None,
    ) -> None:
        self.delegate = delegate
        self.run_id = run_id
        self.repo_root = Path(repo_root)
        self.runs_root = Path(runs_root)
        self.event_logger = event_logger
        self.workflow_id = workflow_id
        self.trace_id = trace_id

    def bind_runtime_context(
        self,
        *,
        run_id: str,
        workflow_id: str | None,
        trace_id: str,
        event_logger: RunEventLogger,
    ) -> None:
        self.run_id = run_id
        self.workflow_id = workflow_id
        self.trace_id = trace_id
        self.event_logger = event_logger

        if self.delegate is not None:
            if hasattr(self.delegate, "run_id"):
                self.delegate.run_id = run_id
            if hasattr(self.delegate, "repo_root"):
                self.delegate.repo_root = self.repo_root
            if hasattr(self.delegate, "runs_root"):
                self.delegate.runs_root = self.runs_root
            if hasattr(self.delegate, "workers_base"):
                self.delegate.workers_base = self.runs_root / run_id / "workers"
            if hasattr(self.delegate, "event_logger"):
                self.delegate.event_logger = event_logger

    def execute(
        self,
        agent_id: str,
        task_message: dict,
        agent_md_path: str,
        timeout_seconds: int = 600,
        backend: str = "claude-cli",
        model: str = "",
    ) -> dict:
        delegate = self._get_delegate()
        task_id = task_message.get("task_id")
        step_id = task_message.get("payload", {}).get("step_id")
        started_at = time.time()

        self._emit(
            "adapter.step_handler.started",
            message="HASHI step handler dispatch started",
            request_id=task_id,
            step_id=step_id,
            data={
                "agent_id": agent_id,
                "backend": backend,
                "model": model,
                "delegate": type(delegate).__name__,
            },
        )

        try:
            result = delegate.execute(
                agent_id=agent_id,
                task_message=task_message,
                agent_md_path=agent_md_path,
                timeout_seconds=timeout_seconds,
                backend=backend,
                model=model,
            )
        except Exception as exc:
            self._emit(
                "adapter.step_handler.failed",
                message="HASHI step handler dispatch failed",
                request_id=task_id,
                step_id=step_id,
                duration_ms=(time.time() - started_at) * 1000,
                error_code="adapter_exception",
                error_message=str(exc),
                data={"agent_id": agent_id, "delegate": type(delegate).__name__},
            )
            raise

        status = str(result.get("status", "unknown")).lower()
        event_name = "adapter.step_handler.completed" if status in {"completed", "success"} else "adapter.step_handler.failed"
        self._emit(
            event_name,
            message="HASHI step handler dispatch finished",
            request_id=task_id,
            step_id=step_id,
            duration_ms=(time.time() - started_at) * 1000,
            error_code=result.get("error_type"),
            error_message=result.get("error_message") or result.get("error"),
            data={
                "agent_id": agent_id,
                "delegate": type(delegate).__name__,
                "status": result.get("status"),
            },
        )
        return result

    def _get_delegate(self) -> Any:
        if self.delegate is None:
            if not self.run_id:
                raise RuntimeError("HASHIStepHandler requires run_id before first execute().")
            self.delegate = SubprocessStepHandler(
                self.run_id,
                event_logger=self.event_logger,
                repo_root=self.repo_root,
                runs_root=self.runs_root,
            )
        return self.delegate

    def _emit(self, event: str, **kwargs: Any) -> None:
        if not self.event_logger:
            return
        self.event_logger.emit(event, component="hashi.adapter.step_handler", **kwargs)


class HChatNotifier:
    """Wrap HASHI's HChat integration with adapter-scoped logging."""

    def __init__(
        self,
        delegate: Any | None = None,
        send_func: Callable[..., Any] | None = None,
        *,
        event_logger: RunEventLogger | None = None,
    ) -> None:
        self.delegate = delegate
        self.send_func = send_func
        self.event_logger = event_logger

    def bind_runtime_context(self, *, event_logger: RunEventLogger, **_: Any) -> None:
        self.event_logger = event_logger

    def send(
        self,
        *,
        agent_id: str,
        text: str,
        run_id: str | None = None,
        workflow_id: str | None = None,
    ) -> None:
        request_id = f"notify:{agent_id}:{int(time.time() * 1000)}"
        started_at = time.time()
        self._emit(
            "adapter.notifier.started",
            message="HASHI notifier send started",
            request_id=request_id,
            data={"agent_id": agent_id, "workflow_id": workflow_id},
        )
        try:
            self._send(agent_id=agent_id, text=text, run_id=run_id, workflow_id=workflow_id)
        except Exception as exc:
            self._emit(
                "adapter.notifier.failed",
                message="HASHI notifier send failed",
                request_id=request_id,
                duration_ms=(time.time() - started_at) * 1000,
                error_code="notifier_exception",
                error_message=str(exc),
                data={"agent_id": agent_id, "workflow_id": workflow_id},
            )
            raise
        self._emit(
            "adapter.notifier.completed",
            message="HASHI notifier send completed",
            request_id=request_id,
            duration_ms=(time.time() - started_at) * 1000,
            data={"agent_id": agent_id, "workflow_id": workflow_id, "run_id": run_id},
        )

    def _send(
        self,
        *,
        agent_id: str,
        text: str,
        run_id: str | None,
        workflow_id: str | None,
    ) -> Any:
        if self.delegate is not None:
            return self.delegate.send(
                agent_id=agent_id,
                text=text,
                run_id=run_id,
                workflow_id=workflow_id,
            )
        if self.send_func is not None:
            return self.send_func(to_agent=agent_id, from_agent="flow-runner", text=text)

        from tools.hchat_send import send_hchat

        return send_hchat(to_agent=agent_id, from_agent="flow-runner", text=text)

    def _emit(self, event: str, **kwargs: Any) -> None:
        if not self.event_logger:
            return
        self.event_logger.emit(event, component="hashi.adapter.notifier", **kwargs)


class HASHIEvaluator:
    """Wrap HASHI's evaluator so the core engine can treat it as optional."""

    def __init__(
        self,
        evaluator: Any | None = None,
        *,
        event_logger: RunEventLogger | None = None,
    ) -> None:
        self.evaluator = evaluator
        self.event_logger = event_logger

    def bind_runtime_context(self, *, event_logger: RunEventLogger, **_: Any) -> None:
        self.event_logger = event_logger

    def evaluate_run(self, run_id: str) -> dict[str, Any] | None:
        request_id = f"evaluate:{run_id}"
        started_at = time.time()
        self._emit(
            "adapter.evaluator.started",
            message="HASHI evaluator started",
            request_id=request_id,
            data={"run_id": run_id},
        )
        try:
            report = self._get_evaluator().evaluate_run(run_id)
        except Exception as exc:
            self._emit(
                "adapter.evaluator.failed",
                message="HASHI evaluator failed",
                request_id=request_id,
                duration_ms=(time.time() - started_at) * 1000,
                error_code="evaluator_exception",
                error_message=str(exc),
                data={"run_id": run_id},
            )
            raise
        self._emit(
            "adapter.evaluator.completed",
            message="HASHI evaluator completed",
            request_id=request_id,
            duration_ms=(time.time() - started_at) * 1000,
            data={
                "run_id": run_id,
                "report_available": report is not None,
                "overall_score": None if not report else report.get("scores", {}).get("overall"),
            },
        )
        return report

    def _get_evaluator(self) -> Any:
        if self.evaluator is None:
            from flow.agents.evaluator.evaluator import FlowEvaluator

            self.evaluator = FlowEvaluator()
        return self.evaluator

    def _emit(self, event: str, **kwargs: Any) -> None:
        if not self.event_logger:
            return
        self.event_logger.emit(event, component="hashi.adapter.evaluator", **kwargs)


def ensure_hashi_step_handler(
    step_handler: Any | None,
    *,
    run_id: str | None = None,
    repo_root: str | Path = ROOT,
    runs_root: str | Path = DEFAULT_RUNS_ROOT,
) -> HASHIStepHandler:
    if isinstance(step_handler, HASHIStepHandler):
        return step_handler
    return HASHIStepHandler(
        step_handler,
        run_id=run_id,
        repo_root=repo_root,
        runs_root=runs_root,
    )


def ensure_hashi_notifier(notifier: Any | None) -> HChatNotifier:
    if isinstance(notifier, HChatNotifier):
        return notifier
    if notifier is None:
        return HChatNotifier()
    return HChatNotifier(delegate=notifier)


def ensure_hashi_evaluator(evaluator: Any | None) -> HASHIEvaluator:
    if isinstance(evaluator, HASHIEvaluator):
        return evaluator
    return HASHIEvaluator(evaluator)
