from __future__ import annotations

from pathlib import Path

from orchestrator.superloop_runner import SuperloopRunner
from orchestrator.superloop_store import SuperloopStore
from orchestrator.superloop_validator import format_validation_report, validate_loop


def _create_loop(store: SuperloopStore, *, taskboard: list[dict], issues: list[dict] | None = None, waits: list[dict] | None = None) -> None:
    store.create_compiled_loop(
        loop_id="sl-test-001",
        loop_state={
            "loop_id": "sl-test-001",
            "status": "running",
            "taskboard_path": "superloops/loops/sl-test-001/taskboard.json",
            "issues_path": "superloops/loops/sl-test-001/issues.json",
            "waits_path": "superloops/loops/sl-test-001/waits.json",
        },
        taskboard=taskboard,
        issues=issues or [],
        waits=waits or [],
        operator_summary="# summary\n",
    )


def test_validator_advisory_reports_hchat_evidence_without_blocking(tmp_path: Path) -> None:
    store = SuperloopStore(tmp_path / "superloops")
    _create_loop(
        store,
        taskboard=[
            {
                "task_id": "task-001",
                "title": "Ask Nana",
                "status": "completed",
                "owner_agent": "nana",
                "owner_instance": "HASHI1",
                "depends_on": [],
                "execution_mode": "hchat_agent",
            }
        ],
    )

    report = validate_loop(store, "sl-test-001")

    assert report["blocking"] is False
    assert report["summary"]["warnings"] >= 1
    assert any(item["code"] == "hchat_task_missing_receipt" for item in report["findings"])
    assert "Superloop validation" in format_validation_report(report)


def test_validator_closeout_blocks_truth_claim_without_hchat_receipt(tmp_path: Path) -> None:
    store = SuperloopStore(tmp_path / "superloops")
    _create_loop(
        store,
        taskboard=[
            {
                "task_id": "task-001",
                "title": "Ask Nana",
                "status": "completed",
                "owner_agent": "nana",
                "owner_instance": "HASHI1",
                "depends_on": [],
                "execution_mode": "hchat_agent",
            }
        ],
    )

    report = validate_loop(store, "sl-test-001", closeout=True)

    assert report["blocking"] is True
    assert report["summary"]["errors"] >= 1
    assert any(item["code"] == "hchat_task_missing_receipt" for item in report["findings"])


def test_runner_does_not_auto_complete_when_closeout_validation_fails(tmp_path: Path) -> None:
    store = SuperloopStore(tmp_path / "superloops")
    _create_loop(
        store,
        taskboard=[
            {
                "task_id": "task-001",
                "title": "Ask Nana",
                "status": "completed",
                "owner_agent": "nana",
                "owner_instance": "HASHI1",
                "depends_on": [],
                "execution_mode": "hchat_agent",
            }
        ],
    )

    result = SuperloopRunner(store).next_action("sl-test-001")

    assert result["ok"] is False
    assert result["reason"] == "closeout_blocked"
    state = store.load_loop_state("sl-test-001")
    assert state["status"] == "blocked"
    assert state["next_action"]["kind"] == "repair_closeout_evidence"


def test_closeout_accepts_hchat_task_with_dispatch_and_receipt(tmp_path: Path) -> None:
    store = SuperloopStore(tmp_path / "superloops")
    _create_loop(
        store,
        taskboard=[
            {
                "task_id": "task-001",
                "title": "Ask Nana",
                "status": "completed",
                "owner_agent": "nana",
                "owner_instance": "HASHI1",
                "depends_on": [],
                "execution_mode": "hchat_agent",
                "dispatch_refs": ["dispatch_nana.md"],
                "receipt_refs": ["nana_report.md"],
            }
        ],
    )

    report = validate_loop(store, "sl-test-001", closeout=True)

    assert report["blocking"] is False
    assert report["summary"]["errors"] == 0


def test_completed_dispatch_task_accepts_dispatch_refs_as_required_evidence(tmp_path: Path) -> None:
    store = SuperloopStore(tmp_path / "superloops")
    _create_loop(
        store,
        taskboard=[
            {
                "task_id": "task-001",
                "title": "Dispatch worker",
                "status": "completed",
                "owner_agent": "zelda",
                "owner_instance": "HASHI1",
                "depends_on": [],
                "execution_mode": "local_self",
                "required_evidence": ["dispatch_refs"],
                "dispatch_refs": ["dispatch_mimi.md"],
            }
        ],
        waits=[
            {
                "wait_id": "wait-001",
                "kind": "await_hchat_reply",
                "status": "completed",
            }
        ],
    )

    report = validate_loop(store, "sl-test-001", closeout=True)

    assert report["blocking"] is False
    assert report["summary"]["errors"] == 0
    assert not any(item["code"] == "wait_status_noncontract" for item in report["findings"])
