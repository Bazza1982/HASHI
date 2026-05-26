from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from orchestrator.superloop_store import SuperloopStore, system_actor


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SuperloopTaskboardService:
    def __init__(self, store: SuperloopStore):
        self.store = store

    def list_tasks(self, loop_id: str) -> list[dict[str, Any]]:
        return self.store.load_loop_json_list(self._taskboard_path(loop_id))

    def add_task(
        self,
        loop_id: str,
        *,
        title: str,
        owner_agent: str,
        owner_instance: str,
        depends_on: list[str] | None = None,
        task_id: str | None = None,
        actor: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        path = self._taskboard_path(loop_id)
        with self.store._lock:
            tasks = self.store.load_loop_json_list(path)
            assigned_task_id = task_id or self.store.generate_record_id("task")
            now = _utc_now()
            task = {
                "task_id": assigned_task_id,
                "title": title,
                "description": title,
                "status": "pending",
                "owner_agent": owner_agent,
                "owner_instance": owner_instance,
                "depends_on": list(depends_on or []),
                "priority": "normal",
                "created_at": now,
                "updated_at": now,
                "artifact_refs": [],
                "notes": [],
            }
            tasks.append(task)
            self.store.save_loop_json_list(path, tasks)
            self.store.refresh_loop_stats(loop_id)
        self.store.append_loop_event(
            loop_id,
            event_type="task.added",
            data={"task_id": assigned_task_id},
            actor=actor or system_actor("superloop_taskboard"),
        )
        return task

    def update_task_status(
        self,
        loop_id: str,
        task_id: str,
        status: str,
        *,
        actor: dict[str, Any] | None = None,
    ) -> bool:
        path = self._taskboard_path(loop_id)
        with self.store._lock:
            tasks = self.store.load_loop_json_list(path)
            updated = False
            for task in tasks:
                if task.get("task_id") != task_id:
                    continue
                task["status"] = status
                task["updated_at"] = _utc_now()
                updated = True
                break
            if not updated:
                return False
            self.store.save_loop_json_list(path, tasks)
            self.store.refresh_loop_stats(loop_id)
        self.store.append_loop_event(
            loop_id,
            event_type="task.status_updated",
            data={"task_id": task_id, "status": status},
            actor=actor or system_actor("superloop_taskboard"),
        )
        return True

    def _taskboard_path(self, loop_id: str) -> Path:
        state = self.store.load_loop_state(loop_id)
        path_rel = state.get("taskboard_path")
        return self.store.resolve_loop_path(loop_id, path_rel, "taskboard.json")
