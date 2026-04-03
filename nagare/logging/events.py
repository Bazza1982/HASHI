from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


EVENT_LEVELS = {
    "run.failed": "ERROR",
    "run.cancelled": "WARNING",
    "step.failed": "ERROR",
    "workflow.load.failed": "ERROR",
    "workflow.validate.failed": "ERROR",
    "workflow.export.blocked": "WARNING",
    "workflow.fidelity.warning": "WARNING",
    "handler.invoke.failed": "ERROR",
}

LEGACY_EVENT_NAMES = {
    "run.started": "workflow_started",
    "run.completed": "workflow_completed",
    "run.failed": "workflow_failed",
    "run.cancelled": "workflow_aborted",
    "step.started": "step_started",
    "step.completed": "step_completed",
    "step.failed": "step_failed",
    "handler.invoke.started": "handler_started",
    "handler.invoke.completed": "handler_completed",
    "handler.invoke.failed": "handler_failed",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class RunEventLogger:
    """Emit stable run-scoped events and preserve legacy evaluator events."""

    def __init__(
        self,
        *,
        run_id: str,
        trace_id: str,
        workflow_id: str | None,
        workflow_path: str | None,
        runs_root: str | Path = "flow/runs",
        component: str = "engine.runner",
    ) -> None:
        self.run_id = run_id
        self.trace_id = trace_id
        self.workflow_id = workflow_id
        self.workflow_path = workflow_path
        self.component = component
        self.runs_root = Path(runs_root)
        self.run_dir = self.runs_root / run_id
        self.events_path = self.run_dir / "events.jsonl"
        self.legacy_events_path = self.run_dir / "evaluation_events.jsonl"
        self.logger = logging.getLogger(f"nagare.events.{run_id}")
        self.run_dir.mkdir(parents=True, exist_ok=True)

    def emit(
        self,
        event: str,
        *,
        message: str,
        component: str | None = None,
        level: str | None = None,
        request_id: str | None = None,
        step_id: str | None = None,
        duration_ms: int | float | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        record = {
            "timestamp": utc_now(),
            "level": level or EVENT_LEVELS.get(event, "INFO"),
            "component": component or self.component,
            "event": event,
            "message": message,
            "run_id": self.run_id,
            "trace_id": self.trace_id,
            "request_id": request_id,
            "workflow_id": self.workflow_id,
            "workflow_path": self.workflow_path,
            "step_id": step_id,
            "duration_ms": None if duration_ms is None else int(duration_ms),
            "error_code": error_code,
            "error_message": error_message,
            "data": data or {},
        }
        with open(self.events_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

        legacy_name = LEGACY_EVENT_NAMES.get(event)
        if legacy_name:
            legacy = {
                "event_type": legacy_name,
                "workflow_id": self.workflow_id,
                "run_id": self.run_id,
                "trace_id": self.trace_id,
                "ts": record["timestamp"],
                "data": dict(record["data"]),
            }
            if step_id and "step_id" not in legacy["data"]:
                legacy["data"]["step_id"] = step_id
            if error_message and "error" not in legacy["data"]:
                legacy["data"]["error"] = error_message
            with open(self.legacy_events_path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(legacy, ensure_ascii=False) + "\n")

        log_line = f"{event} | run={self.run_id}"
        if step_id:
            log_line += f" | step={step_id}"
        if error_message:
            log_line += f" | error={error_message}"
        getattr(self.logger, record["level"].lower(), self.logger.info)(log_line)
        return record


def build_runtime_snapshot(state: dict) -> dict[str, Any]:
    steps = state.get("steps", {})
    current_steps = sorted(
        step_id
        for step_id, step in steps.items()
        if step.get("status") in {"running", "waiting_human"}
    )
    completed_steps = sorted(
        step_id for step_id, step in steps.items() if step.get("status") == "completed"
    )
    failed_steps = sorted(
        step_id for step_id, step in steps.items() if step.get("status") == "failed"
    )
    waiting_human_steps = sorted(
        step_id for step_id, step in steps.items() if step.get("status") == "waiting_human"
    )

    step_status = {}
    for step_id, step in steps.items():
        step_status[step_id] = {
            "status": step.get("status", "unknown").upper(),
            "attempt": step.get("attempt", 1),
            "started_at": step.get("started_at"),
            "ended_at": step.get("ended_at"),
            "artifacts": step.get("artifacts", {}),
            "error": step.get("error"),
        }

    return {
        "run_id": state.get("run_id"),
        "workflow_id": state.get("workflow_id"),
        "workflow_version": state.get("workflow_version"),
        "status": str(state.get("workflow_status", "unknown")).upper(),
        "created_at": state.get("created_at"),
        "updated_at": state.get("updated_at"),
        "current_steps": current_steps,
        "completed_steps": completed_steps,
        "failed_steps": failed_steps,
        "waiting_human_steps": waiting_human_steps,
        "step_status": step_status,
    }
