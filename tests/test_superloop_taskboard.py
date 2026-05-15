from __future__ import annotations

from pathlib import Path

from orchestrator.superloop_store import SuperloopStore
from orchestrator.superloop_taskboard import SuperloopTaskboardService


def test_taskboard_add_and_update(tmp_path: Path) -> None:
    store = SuperloopStore(tmp_path / "superloops")
    store.create_compiled_loop(
        loop_id="sl-test-001",
        loop_state={"loop_id": "sl-test-001", "status": "running", "taskboard_path": "superloops/loops/sl-test-001/taskboard.json"},
        taskboard=[],
        issues=[],
        waits=[],
        operator_summary="# summary\n",
    )
    svc = SuperloopTaskboardService(store)
    task = svc.add_task(
        "sl-test-001",
        title="Do work",
        owner_agent="zelda",
        owner_instance="HASHI1",
    )
    assert task["task_id"].startswith("task-")
    assert svc.update_task_status("sl-test-001", task["task_id"], "completed") is True
    tasks = svc.list_tasks("sl-test-001")
    assert tasks[0]["status"] == "completed"
