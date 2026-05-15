from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from orchestrator.superloop_store import SuperloopStore


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_json_list(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    with open(path, "r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, list):
        raise ValueError(f"Expected list JSON in {path}")
    return [item for item in payload if isinstance(item, dict)]


def _dump_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with open(tmp_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    tmp_path.replace(path)


class SuperloopTaskboardService:
    def __init__(self, store: SuperloopStore):
        self.store = store

    def list_tasks(self, loop_id: str) -> list[dict[str, Any]]:
        return _load_json_list(self._taskboard_path(loop_id))

    def add_task(
        self,
        loop_id: str,
        *,
        title: str,
        owner_agent: str,
        owner_instance: str,
        depends_on: list[str] | None = None,
        task_id: str | None = None,
    ) -> dict[str, Any]:
        path = self._taskboard_path(loop_id)
        tasks = _load_json_list(path)
        assigned_task_id = task_id or f"task-{len(tasks) + 1:03d}"
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
        _dump_json(path, tasks)
        self.store.append_loop_event(loop_id, event_type="task.added", data={"task_id": assigned_task_id})
        return task

    def update_task_status(self, loop_id: str, task_id: str, status: str) -> bool:
        path = self._taskboard_path(loop_id)
        tasks = _load_json_list(path)
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
        _dump_json(path, tasks)
        self.store.append_loop_event(loop_id, event_type="task.status_updated", data={"task_id": task_id, "status": status})
        return True

    def _taskboard_path(self, loop_id: str) -> Path:
        state = self.store.load_loop_state(loop_id)
        path_rel = state.get("taskboard_path")
        if isinstance(path_rel, str) and path_rel.strip():
            return (self.store.root_dir.parent / path_rel).resolve()
        return self.store.loop_dir(loop_id) / "taskboard.json"
