from __future__ import annotations

import json
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
    transcript = tmp_path / "workspaces" / "nana" / "transcript.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text(
        '{"role":"assistant","text":"done sl-test-001 task-001 receipt-nana artifact nana_report.md"}\n',
        encoding="utf-8",
    )
    artifact = tmp_path / "nana_report.md"
    artifact.write_text("# Nana report\n", encoding="utf-8")
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
                "receipt_refs": ["receipt-nana"],
                "artifact_refs": ["nana_report.md"],
                "receipt_sources": [
                    {
                        "agent": "nana",
                        "transcript_path": "workspaces/nana/transcript.jsonl",
                        "line_start": 1,
                        "line_end": 1,
                        "artifact_path": "nana_report.md",
                    }
                ],
            }
        ],
    )

    report = validate_loop(store, "sl-test-001", closeout=True)

    assert report["blocking"] is False
    assert report["summary"]["errors"] == 0


def test_closeout_blocks_hchat_task_with_unverifiable_receipt_ref(tmp_path: Path) -> None:
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
                "receipt_refs": ["receipt-nana"],
                "artifact_refs": ["nana_report.md"],
            }
        ],
    )

    report = validate_loop(store, "sl-test-001", closeout=True)

    assert report["blocking"] is True
    assert any(item["code"] == "hchat_receipt_unverifiable" for item in report["findings"])


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


def test_closeout_accepts_enterprise_evidence_bundle_as_closeout_evidence(tmp_path: Path) -> None:
    store = SuperloopStore(tmp_path / "superloops")
    _create_loop(
        store,
        taskboard=[
            {
                "task_id": "task-001",
                "title": "Complete governed task",
                "status": "completed",
                "owner_agent": "zelda",
                "owner_instance": "HASHI1",
                "depends_on": [],
                "execution_mode": "local_self",
                "required_evidence": ["closeout evidence"],
                "evidence_bundle_ids": ["evb-001"],
            }
        ],
    )

    report = validate_loop(store, "sl-test-001", closeout=True)

    assert report["blocking"] is False
    assert report["summary"]["errors"] == 0


def test_closed_issue_and_wait_statuses_are_contract_terminal_states(tmp_path: Path) -> None:
    store = SuperloopStore(tmp_path / "superloops")
    _create_loop(
        store,
        taskboard=[
            {
                "task_id": "task-001",
                "title": "Complete local task",
                "status": "completed",
                "owner_agent": "zelda",
                "owner_instance": "HASHI1",
                "depends_on": [],
                "execution_mode": "local_self",
                "artifact_refs": ["artifact.md"],
            }
        ],
        issues=[
            {
                "issue_id": "issue-001",
                "title": "Reviewer blocker",
                "status": "closed",
                "severity": "blocker",
                "blocks_closeout": True,
            }
        ],
        waits=[
            {
                "wait_id": "wait-001",
                "kind": "await_hchat_reply",
                "status": "closed",
            }
        ],
    )

    report = validate_loop(store, "sl-test-001", closeout=True)

    assert report["blocking"] is False
    assert report["summary"]["errors"] == 0
    assert not any(item["code"] in {"issue_status_noncontract", "wait_status_noncontract"} for item in report["findings"])


def test_validator_reports_state_stats_drift(tmp_path: Path) -> None:
    store = SuperloopStore(tmp_path / "superloops")
    _create_loop(store, taskboard=[{"task_id": "task-001", "status": "pending"}])
    state = store.load_loop_state("sl-test-001")
    state["stats"] = {"task_total": 99, "task_completed": 99, "issue_open": 0, "wait_open": 0}
    store.save_loop_state("sl-test-001", state)

    report = validate_loop(store, "sl-test-001")

    assert any(item["code"] == "state_stats_drift" for item in report["findings"])


def test_validator_blocks_unclassified_live_attempt_at_closeout(tmp_path: Path) -> None:
    store = SuperloopStore(tmp_path / "superloops")
    _create_loop(store, taskboard=[])
    store.append_loop_event(
        "sl-test-001",
        event_type="live_attempt.started",
        data={"task_id": "task-1", "cell_id": "cell-1", "scenario": "C01", "attempt": 1},
    )

    report = validate_loop(store, "sl-test-001", closeout=True)

    assert report["blocking"] is True
    assert any(item["code"] == "live_attempt_unclassified" for item in report["findings"])


def test_validator_reports_paused_loop_with_active_schema_v2_dispatch(tmp_path: Path) -> None:
    store = SuperloopStore(tmp_path / "superloops")
    _create_loop(store, taskboard=[])
    state = store.load_loop_state("sl-test-001")
    state["status"] = "paused"
    state["active_dispatch_id"] = "dispatch-1"
    store.save_loop_state("sl-test-001", state)
    row = {
        "schema_version": 2,
        "dispatch_instance_id": "dispatch-1",
        "dispatch_id": "dispatch-1",
        "request_id": "req-1",
        "status": "accepted",
        "terminal": False,
    }
    (store.loop_dir("sl-test-001") / "dispatches.jsonl").write_text(
        json.dumps(row) + "\n",
        encoding="utf-8",
    )

    report = validate_loop(store, "sl-test-001")

    assert any(item["code"] == "terminal_loop_with_active_dispatch" for item in report["findings"])
