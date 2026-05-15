from __future__ import annotations

from pathlib import Path

from orchestrator.superloop_runner import SuperloopRunner
from orchestrator.superloop_store import SuperloopStore
from orchestrator.superloop_taskboard import SuperloopTaskboardService
from orchestrator.superloop_waits import SuperloopWaitsService


def test_runner_blocks_when_wait_pending(tmp_path: Path) -> None:
    store = SuperloopStore(tmp_path / "superloops")
    store.create_compiled_loop(
        loop_id="sl-test-001",
        loop_state={
            "loop_id": "sl-test-001",
            "status": "running",
            "taskboard_path": "superloops/loops/sl-test-001/taskboard.json",
            "waits_path": "superloops/loops/sl-test-001/waits.json",
        },
        taskboard=[
            {
                "task_id": "task-001",
                "title": "A",
                "status": "pending",
                "depends_on": [],
                "owner_agent": "zelda",
                "owner_instance": "HASHI1",
            }
        ],
        issues=[],
        waits=[],
        operator_summary="# summary\n",
    )
    waits = SuperloopWaitsService(store)
    waits.add_wait("sl-test-001", kind="await_hchat_reply")
    runner = SuperloopRunner(store)
    result = runner.next_action("sl-test-001")
    assert result["advanced"] is False
    assert result["reason"] == "open_waits"


def test_runner_advances_next_pending_task(tmp_path: Path) -> None:
    store = SuperloopStore(tmp_path / "superloops")
    store.create_compiled_loop(
        loop_id="sl-test-002",
        loop_state={
            "loop_id": "sl-test-002",
            "status": "running",
            "taskboard_path": "superloops/loops/sl-test-002/taskboard.json",
            "waits_path": "superloops/loops/sl-test-002/waits.json",
        },
        taskboard=[
            {
                "task_id": "task-001",
                "title": "A",
                "status": "pending",
                "depends_on": [],
                "owner_agent": "zelda",
                "owner_instance": "HASHI1",
            }
        ],
        issues=[],
        waits=[],
        operator_summary="# summary\n",
    )
    runner = SuperloopRunner(store)
    result = runner.next_action("sl-test-002")
    assert result["advanced"] is True
    assert result["task_id"] == "task-001"
    taskboard = SuperloopTaskboardService(store).list_tasks("sl-test-002")
    assert taskboard[0]["status"] == "in_progress"


def test_runner_does_not_skip_in_progress_task(tmp_path: Path) -> None:
    store = SuperloopStore(tmp_path / "superloops")
    store.create_compiled_loop(
        loop_id="sl-test-003",
        loop_state={
            "loop_id": "sl-test-003",
            "status": "running",
            "current_step": "task-001",
            "next_action": {"kind": "run_task", "task_id": "task-001"},
            "taskboard_path": "superloops/loops/sl-test-003/taskboard.json",
            "waits_path": "superloops/loops/sl-test-003/waits.json",
        },
        taskboard=[
            {
                "task_id": "task-001",
                "title": "A",
                "status": "in_progress",
                "depends_on": [],
                "owner_agent": "zelda",
                "owner_instance": "HASHI1",
            },
            {
                "task_id": "task-002",
                "title": "B",
                "status": "pending",
                "depends_on": [],
                "owner_agent": "zelda",
                "owner_instance": "HASHI1",
            },
        ],
        issues=[],
        waits=[],
        operator_summary="# summary\n",
    )
    runner = SuperloopRunner(store)
    result = runner.next_action("sl-test-003")
    assert result["advanced"] is False
    assert result["reason"] == "task_in_progress"
    assert result["task_id"] == "task-001"
    taskboard = SuperloopTaskboardService(store).list_tasks("sl-test-003")
    assert [task["status"] for task in taskboard] == ["in_progress", "pending"]
