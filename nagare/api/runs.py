from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

from nagare.api.models import (
    ArtifactEntryModel,
    ArtifactIndexModel,
    EventStreamModel,
    RunSnapshotModel,
    envelope,
)
from nagare.engine.artifacts import ArtifactStore
from nagare.engine.state import TaskState
from nagare.logging.events import RunEventLogger, utc_now


class RunNotFoundError(FileNotFoundError):
    """Raised when a run directory is missing required state."""


class RunSnapshotService:
    def __init__(self, runs_root: str | Path = "flow/runs") -> None:
        self.runs_root = Path(runs_root)

    def new_request_id(self) -> str:
        return f"api-{uuid.uuid4()}"

    def get_run_snapshot(self, run_id: str, *, request_id: str | None = None) -> dict[str, Any]:
        started_at = time.perf_counter()
        request_id = request_id or self.new_request_id()
        retrieved_at = utc_now()
        state = self._load_state(run_id)
        full_status = state.get_full_status()
        snapshot = state.get_runtime_snapshot()
        payload = RunSnapshotModel(
            run_id=run_id,
            workflow_id=snapshot.get("workflow_id"),
            workflow_version=snapshot.get("workflow_version"),
            workflow_path=full_status.get("workflow_path"),
            status=snapshot.get("status", "UNKNOWN"),
            created_at=snapshot.get("created_at"),
            updated_at=snapshot.get("updated_at"),
            current_steps=list(snapshot.get("current_steps", [])),
            completed_steps=list(snapshot.get("completed_steps", [])),
            failed_steps=list(snapshot.get("failed_steps", [])),
            waiting_human_steps=list(snapshot.get("waiting_human_steps", [])),
            step_status=dict(snapshot.get("step_status", {})),
            error_count=int(full_status.get("error_count", 0)),
            human_intervention_count=len(full_status.get("human_interventions", [])),
        ).to_dict()
        response = envelope(request_id=request_id, retrieved_at=retrieved_at, payload={"run": payload})
        self._emit_request_log(
            run_id=run_id,
            request_id=request_id,
            endpoint=f"/runs/{run_id}",
            response_count=len(payload["step_status"]),
            duration_ms=(time.perf_counter() - started_at) * 1000,
        )
        return response

    def get_run_events(
        self,
        run_id: str,
        *,
        request_id: str | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        started_at = time.perf_counter()
        request_id = request_id or self.new_request_id()
        retrieved_at = utc_now()
        self._ensure_run_exists(run_id)
        events_path = self.runs_root / run_id / "events.jsonl"
        events = self._read_jsonl(events_path)
        if limit is not None and limit >= 0:
            events = events[-limit:]
        response = envelope(
            request_id=request_id,
            retrieved_at=retrieved_at,
            payload=EventStreamModel(run_id=run_id, events=events).to_dict(),
        )
        self._emit_request_log(
            run_id=run_id,
            request_id=request_id,
            endpoint=f"/runs/{run_id}/events",
            response_count=len(events),
            duration_ms=(time.perf_counter() - started_at) * 1000,
        )
        return response

    def get_run_artifacts(self, run_id: str, *, request_id: str | None = None) -> dict[str, Any]:
        started_at = time.perf_counter()
        request_id = request_id or self.new_request_id()
        retrieved_at = utc_now()
        self._ensure_run_exists(run_id)
        index = ArtifactStore(run_id, runs_root=self.runs_root).list_all()
        artifacts = [
            ArtifactEntryModel(
                key=key,
                path=str(value.get("path", "")),
                original_path=value.get("original_path"),
                step_id=value.get("step_id"),
                size_bytes=int(value.get("size_bytes", 0)),
                registered_at=value.get("registered_at"),
            )
            for key, value in sorted(index.items())
        ]
        response = envelope(
            request_id=request_id,
            retrieved_at=retrieved_at,
            payload=ArtifactIndexModel(run_id=run_id, artifacts=artifacts).to_dict(),
        )
        self._emit_request_log(
            run_id=run_id,
            request_id=request_id,
            endpoint=f"/runs/{run_id}/artifacts",
            response_count=len(artifacts),
            duration_ms=(time.perf_counter() - started_at) * 1000,
        )
        return response

    def _load_state(self, run_id: str) -> TaskState:
        self._ensure_run_exists(run_id)
        return TaskState(run_id, runs_root=self.runs_root)

    def _ensure_run_exists(self, run_id: str) -> None:
        if not (self.runs_root / run_id / "state.json").exists():
            raise RunNotFoundError(f"Run not found: {run_id}")

    def _read_jsonl(self, path: Path) -> list[dict[str, Any]]:
        if not path.exists():
            return []
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def _emit_request_log(
        self,
        *,
        run_id: str,
        request_id: str,
        endpoint: str,
        response_count: int,
        duration_ms: float,
    ) -> None:
        state = self._load_state(run_id).get_full_status()
        logger = RunEventLogger(
            run_id=run_id,
            trace_id=f"api:{request_id}",
            workflow_id=state.get("workflow_id"),
            workflow_path=state.get("workflow_path"),
            runs_root=self.runs_root,
            component="api.server",
        )
        logger.emit(
            "api.request.completed",
            component="api.server",
            message=f"Served {endpoint}",
            request_id=request_id,
            duration_ms=duration_ms,
            data={
                "endpoint": endpoint,
                "status_code": 200,
                "response_count": response_count,
                "snapshot_version": 1,
            },
        )
