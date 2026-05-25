from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from orchestrator.superloop_scheduler import advance_superloops_once
from orchestrator.superloop_issues import SuperloopIssuesService
from orchestrator.superloop_store import SuperloopStore
from orchestrator.superloop_taskboard import SuperloopTaskboardService
from orchestrator.superloop_waits import SuperloopWaitsService


def test_superloop_scheduler_satisfies_deadline_wait_and_advances(tmp_path: Path) -> None:
    store = SuperloopStore(tmp_path / "superloops")
    store.create_compiled_loop(
        loop_id="sl-test-001",
        loop_state={
            "loop_id": "sl-test-001",
            "status": "running",
            "current_step": None,
            "taskboard_path": "superloops/loops/sl-test-001/taskboard.json",
            "waits_path": "superloops/loops/sl-test-001/waits.json",
        },
        taskboard=[],
        issues=[],
        waits=[],
        operator_summary="# summary\n",
    )
    taskboard = SuperloopTaskboardService(store)
    waits = SuperloopWaitsService(store)
    taskboard.add_task("sl-test-001", title="run smoke", owner_agent="zelda", owner_instance="HASHI1")
    waits.add_wait(
        "sl-test-001",
        kind="sleep_until",
        details={"until": (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()},
        resume_policy={"on_satisfied": "advance", "on_timeout": "raise_issue"},
    )

    stats = advance_superloops_once(tmp_path / "superloops")
    assert stats["loops_checked"] == 1
    assert stats["waits_satisfied"] == 1
    assert stats["loops_advanced"] == 1

    state = store.load_loop_state("sl-test-001")
    assert state["current_step"].startswith("task-")
    assert state["next_action"]["kind"] == "run_task"

    after_waits = waits.list_waits("sl-test-001")
    assert after_waits[0]["status"] == "satisfied"
    issues = SuperloopIssuesService(store).list_issues("sl-test-001")
    assert len(issues) == 1
    assert "timeout in sl-test-001" in issues[0]["title"]


def test_superloop_scheduler_keeps_future_deadline_pending(tmp_path: Path) -> None:
    store = SuperloopStore(tmp_path / "superloops")
    store.create_compiled_loop(
        loop_id="sl-test-002",
        loop_state={
            "loop_id": "sl-test-002",
            "status": "running",
            "taskboard_path": "superloops/loops/sl-test-002/taskboard.json",
            "waits_path": "superloops/loops/sl-test-002/waits.json",
        },
        taskboard=[],
        issues=[],
        waits=[],
        operator_summary="# summary\n",
    )
    waits = SuperloopWaitsService(store)
    waits.add_wait(
        "sl-test-002",
        kind="sleep_until",
        details={"until": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()},
    )

    stats = advance_superloops_once(tmp_path / "superloops")
    assert stats["loops_checked"] == 1
    assert stats["waits_satisfied"] == 0
    assert stats["loops_advanced"] == 0
    assert waits.has_open_waits("sl-test-002") is True


def test_superloop_scheduler_does_not_start_task_without_wait_or_opt_in(tmp_path: Path) -> None:
    store = SuperloopStore(tmp_path / "superloops")
    store.create_compiled_loop(
        loop_id="sl-test-003",
        loop_state={
            "loop_id": "sl-test-003",
            "status": "running",
            "current_step": None,
            "taskboard_path": "superloops/loops/sl-test-003/taskboard.json",
            "waits_path": "superloops/loops/sl-test-003/waits.json",
        },
        taskboard=[],
        issues=[],
        waits=[],
        operator_summary="# summary\n",
    )
    taskboard = SuperloopTaskboardService(store)
    taskboard.add_task("sl-test-003", title="dispatch worker", owner_agent="zelda", owner_instance="HASHI1")

    stats = advance_superloops_once(tmp_path / "superloops")

    assert stats["loops_checked"] == 1
    assert stats["loops_advanced"] == 0
    state = store.load_loop_state("sl-test-003")
    assert state["current_step"] is None
    assert taskboard.list_tasks("sl-test-003")[0]["status"] == "pending"


def test_superloop_scheduler_starts_task_without_wait_when_explicitly_enabled(tmp_path: Path) -> None:
    store = SuperloopStore(tmp_path / "superloops")
    store.create_compiled_loop(
        loop_id="sl-test-004",
        loop_state={
            "loop_id": "sl-test-004",
            "status": "running",
            "current_step": None,
            "scheduler_auto_advance": True,
            "taskboard_path": "superloops/loops/sl-test-004/taskboard.json",
            "waits_path": "superloops/loops/sl-test-004/waits.json",
        },
        taskboard=[],
        issues=[],
        waits=[],
        operator_summary="# summary\n",
    )
    taskboard = SuperloopTaskboardService(store)
    taskboard.add_task("sl-test-004", title="dispatch worker", owner_agent="zelda", owner_instance="HASHI1")

    stats = advance_superloops_once(tmp_path / "superloops")

    assert stats["loops_checked"] == 1
    assert stats["loops_advanced"] == 1
    state = store.load_loop_state("sl-test-004")
    assert state["current_step"].startswith("task-")
    assert taskboard.list_tasks("sl-test-004")[0]["status"] == "in_progress"
