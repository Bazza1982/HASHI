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


def test_runner_blocks_new_dispatch_when_current_phase_has_open_blocker(tmp_path: Path) -> None:
    store = SuperloopStore(tmp_path / "superloops")
    store.create_compiled_loop(
        loop_id="sl-test-blocker",
        loop_state={
            "loop_id": "sl-test-blocker",
            "status": "running",
            "current_phase": "stage_1_flash_cheap",
            "taskboard_path": "superloops/loops/sl-test-blocker/taskboard.json",
            "issues_path": "superloops/loops/sl-test-blocker/issues.json",
            "waits_path": "superloops/loops/sl-test-blocker/waits.json",
        },
        taskboard=[{"task_id": "task-001", "title": "A", "status": "pending", "depends_on": []}],
        issues=[
            {
                "issue_id": "issue-stage-1",
                "title": "Live blocker",
                "status": "open",
                "blocks_stage_1": True,
            }
        ],
        waits=[],
        operator_summary="# summary\n",
    )

    result = SuperloopRunner(store).next_action("sl-test-blocker")

    assert result["advanced"] is False
    assert result["reason"] == "open_blocker_issues"
    assert result["issue_ids"] == ["issue-stage-1"]
    assert SuperloopTaskboardService(store).list_tasks("sl-test-blocker")[0]["status"] == "pending"
    state = store.load_loop_state("sl-test-blocker")
    assert state["dispatch_interlock"]["reason"] == "open_blocker_issues"


def test_runner_allows_unrelated_open_issue(tmp_path: Path) -> None:
    store = SuperloopStore(tmp_path / "superloops")
    store.create_compiled_loop(
        loop_id="sl-test-unrelated-issue",
        loop_state={
            "loop_id": "sl-test-unrelated-issue",
            "status": "running",
            "current_phase": "stage_1_flash_cheap",
            "taskboard_path": "superloops/loops/sl-test-unrelated-issue/taskboard.json",
            "issues_path": "superloops/loops/sl-test-unrelated-issue/issues.json",
            "waits_path": "superloops/loops/sl-test-unrelated-issue/waits.json",
        },
        taskboard=[{"task_id": "task-001", "title": "A", "status": "pending", "depends_on": []}],
        issues=[{"issue_id": "issue-stage-2", "status": "open", "blocks_stage_2": True}],
        waits=[],
        operator_summary="# summary\n",
    )

    result = SuperloopRunner(store).next_action("sl-test-unrelated-issue")

    assert result["advanced"] is True
    assert result["task_id"] == "task-001"


def test_runner_blocks_pause_signal_file(tmp_path: Path) -> None:
    store = SuperloopStore(tmp_path / "superloops")
    store.create_compiled_loop(
        loop_id="sl-test-pause-signal",
        loop_state={
            "loop_id": "sl-test-pause-signal",
            "status": "running",
            "taskboard_path": "superloops/loops/sl-test-pause-signal/taskboard.json",
            "issues_path": "superloops/loops/sl-test-pause-signal/issues.json",
            "waits_path": "superloops/loops/sl-test-pause-signal/waits.json",
        },
        taskboard=[{"task_id": "task-001", "title": "A", "status": "pending", "depends_on": []}],
        issues=[],
        waits=[],
        operator_summary="# summary\n",
    )
    (store.loop_dir("sl-test-pause-signal") / "_pause").write_text("operator requested pause\n", encoding="utf-8")

    result = SuperloopRunner(store).next_action("sl-test-pause-signal")

    assert result["advanced"] is False
    assert result["reason"] == "pause_requested"
    assert result["source"] == "file"


def test_runner_blocks_explicitly_invalid_candidate(tmp_path: Path) -> None:
    store = SuperloopStore(tmp_path / "superloops")
    store.create_compiled_loop(
        loop_id="sl-test-invalid-candidate",
        loop_state={
            "loop_id": "sl-test-invalid-candidate",
            "status": "running",
            "candidate": {"hash": "candidate-1", "evidence_valid": False},
            "taskboard_path": "superloops/loops/sl-test-invalid-candidate/taskboard.json",
            "issues_path": "superloops/loops/sl-test-invalid-candidate/issues.json",
            "waits_path": "superloops/loops/sl-test-invalid-candidate/waits.json",
        },
        taskboard=[{"task_id": "task-001", "title": "A", "status": "pending", "depends_on": []}],
        issues=[],
        waits=[],
        operator_summary="# summary\n",
    )

    result = SuperloopRunner(store).next_action("sl-test-invalid-candidate")

    assert result["advanced"] is False
    assert result["reason"] == "candidate_invalid"
    assert result["candidate_reason"] == "evidence_valid=false"


def test_runner_allows_uninitialized_candidate_placeholder(tmp_path: Path) -> None:
    store = SuperloopStore(tmp_path / "superloops")
    store.create_compiled_loop(
        loop_id="sl-test-candidate-placeholder",
        loop_state={
            "loop_id": "sl-test-candidate-placeholder",
            "status": "running",
            "candidate": {"evidence_valid": False, "valid": False},
            "taskboard_path": "superloops/loops/sl-test-candidate-placeholder/taskboard.json",
            "issues_path": "superloops/loops/sl-test-candidate-placeholder/issues.json",
            "waits_path": "superloops/loops/sl-test-candidate-placeholder/waits.json",
        },
        taskboard=[{"task_id": "task-001", "title": "A", "status": "pending", "depends_on": []}],
        issues=[],
        waits=[],
        operator_summary="# summary\n",
    )

    result = SuperloopRunner(store).next_action("sl-test-candidate-placeholder")

    assert result["advanced"] is True
    assert result["task_id"] == "task-001"
