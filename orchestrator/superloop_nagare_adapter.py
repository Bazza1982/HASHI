from __future__ import annotations

from pathlib import Path
from typing import Any

from orchestrator.superloop_store import SuperloopStore, _json_dump


class SuperloopNagareAdapter:
    def __init__(self, store: SuperloopStore):
        self.store = store

    def add_child_workflow(
        self,
        loop_id: str,
        *,
        workflow_path: str,
        child_id: str | None = None,
    ) -> dict[str, Any]:
        state = self.store.load_loop_state(loop_id)
        children = list(state.get("child_runs") or [])
        resolved_child_id = child_id or f"child-{len(children) + 1:03d}"
        entry = {
            "child_id": resolved_child_id,
            "kind": "nagare",
            "workflow_path": workflow_path,
            "run_id": None,
            "status": "planned",
        }
        children.append(entry)
        state["child_runs"] = children
        _json_dump(self.store.loop_dir(loop_id) / "state.json", state)
        self.store.append_loop_event(
            loop_id,
            event_type="child_run.planned",
            data={"child_id": resolved_child_id, "workflow_path": workflow_path},
        )
        return entry

    def mark_child_completed(self, loop_id: str, child_id: str, run_id: str) -> bool:
        state = self.store.load_loop_state(loop_id)
        children = list(state.get("child_runs") or [])
        updated = False
        for item in children:
            if item.get("child_id") != child_id:
                continue
            item["status"] = "completed"
            item["run_id"] = run_id
            updated = True
            break
        if not updated:
            return False
        state["child_runs"] = children
        _json_dump(self.store.loop_dir(loop_id) / "state.json", state)
        self.store.append_loop_event(
            loop_id,
            event_type="child_run.completed",
            data={"child_id": child_id, "run_id": run_id},
        )
        return True
