from __future__ import annotations

from typing import Any

from orchestrator.superloop_store import SuperloopStore
from orchestrator.superloop_taskboard import SuperloopTaskboardService
from orchestrator.superloop_waits import SuperloopWaitsService


class SuperloopRunner:
    def __init__(
        self,
        store: SuperloopStore,
        *,
        taskboard_service: SuperloopTaskboardService | None = None,
        waits_service: SuperloopWaitsService | None = None,
    ):
        self.store = store
        self.taskboard = taskboard_service or SuperloopTaskboardService(store)
        self.waits = waits_service or SuperloopWaitsService(store)

    def next_action(self, loop_id: str) -> dict[str, Any]:
        with self.store._lock:
            state = self.store.load_loop_state(loop_id)
            status = str(state.get("status") or "")

            if status == "paused":
                return {"ok": True, "loop_id": loop_id, "advanced": False, "reason": "paused"}

            if self.waits.has_open_waits(loop_id):
                return {
                    "ok": True,
                    "loop_id": loop_id,
                    "advanced": False,
                    "reason": "open_waits",
                    "pending_wait_ids": self.waits.pending_wait_ids(loop_id),
                }

            tasks = self.taskboard.list_tasks(loop_id)
            completed = {task["task_id"] for task in tasks if task.get("status") == "completed"}
            next_task = None
            for task in tasks:
                if task.get("status") != "pending":
                    continue
                deps = list(task.get("depends_on") or [])
                if all(dep in completed for dep in deps):
                    next_task = task
                    break

            if next_task is None:
                state["status"] = "completed"
                state["next_action"] = {"kind": "none", "reason": "all_tasks_completed"}
                self._save_loop_state(loop_id, state)
                self.store.append_loop_event(loop_id, event_type="loop.completed", data={"reason": "all_tasks_completed"})
                return {"ok": True, "loop_id": loop_id, "advanced": False, "reason": "completed"}

            task_id = str(next_task["task_id"])
            self.taskboard.update_task_status(loop_id, task_id, "in_progress")
            state["current_step"] = task_id
            state["next_action"] = {"kind": "run_task", "task_id": task_id}
            self._save_loop_state(loop_id, state)
            self.store.append_loop_event(loop_id, event_type="task.started", data={"task_id": task_id})
            return {"ok": True, "loop_id": loop_id, "advanced": True, "task_id": task_id}

    def _save_loop_state(self, loop_id: str, state: dict[str, Any]) -> None:
        self.store.save_loop_state(loop_id, state)
