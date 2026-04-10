"""
HASHI Flow — Task State
任务状态持久化，工作流可从任意检查点恢复
"""

import json
import threading
from datetime import datetime, timezone
from pathlib import Path

from nagare.logging.events import build_runtime_snapshot


def utc_now():
    return datetime.now(timezone.utc).isoformat()


class TaskState:
    """持久化工作流和步骤状态到 state.json（线程安全）"""

    def __init__(self, run_id: str, runs_root: str | Path = "flow/runs"):
        self.run_id = run_id
        self.runs_root = Path(runs_root)
        self.state_path = self.runs_root / run_id / "state.json"
        self._lock = threading.Lock()
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.state_path.exists():
            self._init_state()

    def _init_state(self):
        self._write({
            "run_id": self.run_id,
            "workflow_status": "created",
            "workflow_id": None,
            "workflow_version": None,
            "workflow_path": None,
            "meta": {},                 # optional origin: task_id, phase_id, project_id
            "steps": {},
            "human_interventions": [],
            "error_count": 0,
            "created_at": utc_now(),
            "updated_at": utc_now()
        })

    def set_workflow_metadata(
        self,
        *,
        workflow_id: str | None,
        workflow_version: str | None = None,
        workflow_path: str | None = None,
    ):
        with self._lock:
            state = self._read()
            state["workflow_id"] = workflow_id
            state["workflow_version"] = workflow_version
            state["workflow_path"] = workflow_path
            state["updated_at"] = utc_now()
            self._write(state)

    def set_origin_meta(
        self,
        *,
        task_id: str | None = None,
        phase_id: str | None = None,
        project_id: str | None = None,
    ):
        """Record the AIPM origin (task/phase/project) that triggered this run."""
        with self._lock:
            state = self._read()
            meta = state.setdefault("meta", {})
            if task_id is not None:
                meta["task_id"] = task_id
            if phase_id is not None:
                meta["phase_id"] = phase_id
            if project_id is not None:
                meta["project_id"] = project_id
            state["updated_at"] = utc_now()
            self._write(state)

    def set_workflow_status(self, status: str):
        with self._lock:
            state = self._read()
            state["workflow_status"] = status
            state["updated_at"] = utc_now()
            if status == "running" and "started_at" not in state:
                state["started_at"] = utc_now()
            elif status in ("completed", "failed", "aborted"):
                state["ended_at"] = utc_now()
            self._write(state)

    def set_step_status(self, step_id: str, status: str, **kwargs):
        with self._lock:
            state = self._read()
            if step_id not in state["steps"]:
                state["steps"][step_id] = {"status": "pending"}
            state["steps"][step_id]["status"] = status
            state["steps"][step_id]["updated_at"] = utc_now()
            if status == "running":
                state["steps"][step_id]["started_at"] = utc_now()
            elif status in ("completed", "failed"):
                state["steps"][step_id]["ended_at"] = utc_now()
            state["steps"][step_id].update(kwargs)
            state["updated_at"] = utc_now()
            self._write(state)

    def record_human_intervention(self, reason: str, step_id: str = None):
        with self._lock:
            state = self._read()
            state["human_interventions"].append({
                "ts": utc_now(),
                "reason": reason,
                "step_id": step_id
            })
            state["updated_at"] = utc_now()
            self._write(state)

    def increment_error_count(self):
        with self._lock:
            state = self._read()
            state["error_count"] = state.get("error_count", 0) + 1
            state["updated_at"] = utc_now()
            self._write(state)

    def get_full_status(self) -> dict:
        return self._read()

    def get_runtime_snapshot(self) -> dict:
        return build_runtime_snapshot(self._read())

    def get_step_status(self, step_id: str) -> str:
        state = self._read()
        return state["steps"].get(step_id, {}).get("status", "unknown")

    def _read(self) -> dict:
        with open(self.state_path) as f:
            return json.load(f)

    def _write(self, state: dict):
        # 原子写入：先写临时文件再替换，防止并发读到空文件
        tmp = self.state_path.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        tmp.replace(self.state_path)
