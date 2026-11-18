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



def _canonical_hchat_closeout_case(tmp_path: Path) -> SuperloopStore:
    from orchestrator.superloop_dispatch import SuperloopDispatchLedger
    from orchestrator.superloop_receipts import SuperloopReceiptService
    from orchestrator.superloop_taskboard import SuperloopTaskboardService

    store = SuperloopStore(tmp_path / "superloops")
    _create_loop(store, taskboard=[{
        "task_id": "task-001",
        "title": "Review the delivered work",
        "status": "in_progress",
        "owner_agent": "worker",
        "owner_instance": "HASHI3",
        "depends_on": [],
        "required_evidence": ["dispatch_refs", "receipt_refs"],
        "execution_mode": "hchat_agent",
    }])
    state = store.load_loop_state("sl-test-001")
    state.update(receipt_continuation_enabled=True,
                 controller={"agent": "manager", "instance": "HASHI2"})
    store.save_loop_state("sl-test-001", state)
    ledger = SuperloopDispatchLedger(store)
    ledger.record_started("sl-test-001", dispatch_instance_id="dispatch-1",
                          task_id="task-001", request_id="work-request-1",
                          metadata={"owner_agent": "worker", "owner_instance": "HASHI3"})
    service = SuperloopReceiptService(store, local_instance="HASHI2")
    service.process([{
        "state": "reply_delivered_locally",
        "message_id": "reply-1",
        "in_reply_to": "dispatch-1",
        "conversation_id": "conversation-1",
        "from_agent": "worker",
        "from_instance": "HASHI3",
        "to_agent": "manager",
        "to_instance": "HASHI2",
        "session_id": "controller-session-1",
        "request_id": "reply-request-1",
    }], lambda request: "review-request-1", lambda *_: None)
    reviews_path = store.loop_dir("sl-test-001") / "receipt_reviews.json"
    reviews = store.load_loop_json_list(reviews_path)
    assert len(reviews) == 1 and reviews[0]["status"] == "queued"
    review = store.loop_dir("sl-test-001") / "review.md"
    review.write_text("Verified task-001 from worker; review complete.\n", encoding="utf-8")
    reviews[0].update(review_verified=True, review_evidence_ref="review.md")
    store.save_loop_json_list(reviews_path, reviews)
    ledger.record_terminal("sl-test-001", dispatch_instance_id="dispatch-1",
                           task_id="task-001", request_id="work-request-1",
                           outcome="collected")
    assert SuperloopTaskboardService(store).update_task_status(
        "sl-test-001", "task-001", "completed"
    )
    service.reconcile(
        lambda request: None,
        lambda agent, request: {
            "ok": True, "agent_id": agent, "request_id": request,
            "session_id": "controller-session-1", "terminal": True,
            "state": "completed",
        },
    )
    reviews = store.load_loop_json_list(reviews_path)
    assert reviews[0]["followthrough_state"] == "reviewed"
    return store


def test_closeout_accepts_pao_reviewed_receipt_without_transcript(tmp_path: Path) -> None:
    store = _canonical_hchat_closeout_case(tmp_path)

    result = SuperloopRunner(store).next_action("sl-test-001")

    assert result["reason"] == "completed"
    assert store.load_loop_state("sl-test-001")["status"] == "completed"


def test_closeout_rejects_unproven_or_stale_pao_receipt(tmp_path: Path) -> None:
    from orchestrator.superloop_dispatch import SuperloopDispatchLedger

    for case in ("unreviewed", "missing-review", "wrong-task", "missing-event",
                 "later-failed-dispatch", "required-delivery", "required-artifact",
                 "reopened-task", "recreated-task", "corrupt-journal", "wrong-agent-source",
                 "reassigned-owner", "missing-sender-proof", "wrong-destination"):
        case_path = tmp_path / case
        case_path.mkdir()
        store = _canonical_hchat_closeout_case(case_path)
        root = store.loop_dir("sl-test-001")
        reviews_path = root / "receipt_reviews.json"
        reviews = store.load_loop_json_list(reviews_path)
        if case == "unreviewed":
            reviews[0]["followthrough_state"] = "awaiting_execution"
            store.save_loop_json_list(reviews_path, reviews)
        elif case == "missing-review":
            (root / "review.md").unlink()
        elif case == "wrong-task":
            reviews[0]["task_id"] = "other-task"
            store.save_loop_json_list(reviews_path, reviews)
        elif case == "missing-event":
            events = [json.loads(line) for line in (root / "events.jsonl").read_text(encoding="utf-8").splitlines()]
            (root / "events.jsonl").write_text(
                "".join(json.dumps(event) + "\n" for event in events if event.get("kind") != "receipt.review_queued"),
                encoding="utf-8",
            )
        elif case == "later-failed-dispatch":
            ledger = SuperloopDispatchLedger(store)
            ledger.record_started("sl-test-001", dispatch_instance_id="dispatch-2",
                                  task_id="task-001", request_id="work-request-2")
            ledger.record_terminal("sl-test-001", dispatch_instance_id="dispatch-2",
                                   task_id="task-001", request_id="work-request-2",
                                   outcome="failed", reason="not completed")
        elif case == "required-delivery":
            taskboard_path = root / "taskboard.json"
            tasks = store.load_loop_json_list(taskboard_path)
            tasks[0]["delivery_required"] = True
            store.save_loop_json_list(taskboard_path, tasks)
        elif case == "required-artifact":
            taskboard_path = root / "taskboard.json"
            tasks = store.load_loop_json_list(taskboard_path)
            tasks[0]["required_evidence"].append("artifact_refs")
            store.save_loop_json_list(taskboard_path, tasks)
        elif case in {"reopened-task", "recreated-task"}:
            store.append_loop_event(
                "sl-test-001",
                event_type="task.status_updated" if case == "reopened-task" else "task.added",
                data={"task_id": "task-001", "status": "in_progress"},
            )
        elif case == "corrupt-journal":
            with (root / "events.jsonl").open("a", encoding="utf-8") as handle:
                handle.write("{not-json}\n")
        elif case == "reassigned-owner":
            taskboard_path = root / "taskboard.json"
            tasks = store.load_loop_json_list(taskboard_path)
            tasks[0]["owner_agent"] = "different-worker"
            store.save_loop_json_list(taskboard_path, tasks)
        elif case == "wrong-destination":
            reviews[0]["receipt_to_agent"] = "different-controller"
            store.save_loop_json_list(reviews_path, reviews)
        elif case == "missing-sender-proof":
            for key in ("receipt_from_agent", "receipt_from_instance"):
                reviews[0].pop(key, None)
            store.save_loop_json_list(reviews_path, reviews)
            ledger_path = root / "dispatches.jsonl"
            rows = [json.loads(line) for line in ledger_path.read_text(encoding="utf-8").splitlines()]
            for row in rows:
                row.pop("owner_agent", None)
                row.pop("owner_instance", None)
            ledger_path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        else:
            taskboard_path = root / "taskboard.json"
            tasks = store.load_loop_json_list(taskboard_path)
            tasks[0]["receipt_sources"] = [{
                "agent": "wrong-worker", "transcript_path": "old-missing-transcript.jsonl",
            }]
            store.save_loop_json_list(taskboard_path, tasks)

        report = validate_loop(store, "sl-test-001", closeout=True)
        assert report["blocking"] is True, case



def test_closeout_uses_canonical_receipt_when_transcript_copy_is_missing(tmp_path: Path) -> None:
    store = _canonical_hchat_closeout_case(tmp_path)
    taskboard_path = store.loop_dir("sl-test-001") / "taskboard.json"
    tasks = store.load_loop_json_list(taskboard_path)
    tasks[0]["receipt_sources"] = [{
        "agent": "worker", "transcript_path": "old-missing-transcript.jsonl",
    }]
    store.save_loop_json_list(taskboard_path, tasks)

    report = validate_loop(store, "sl-test-001", closeout=True)

    assert report["blocking"] is False
    assert any(item["code"] == "hchat_receipt_canonical_reviewed" for item in report["findings"])
    assert any(item["code"] == "hchat_receipt_transcript_missing"
               and item["severity"] == "legacy" for item in report["findings"])



def test_closeout_rechecks_journal_when_task_reopens_during_receipt_review(
    tmp_path: Path, monkeypatch,
) -> None:
    store = _canonical_hchat_closeout_case(tmp_path)
    read_text = Path.read_text
    changed = False

    def read_and_reopen(path: Path, *args, **kwargs):
        nonlocal changed
        if path.name == "receipt_reviews.json" and not changed:
            changed = True
            store.append_loop_event(
                "sl-test-001", event_type="task.status_updated",
                data={"task_id": "task-001", "status": "in_progress"},
            )
        return read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_and_reopen)
    report = validate_loop(store, "sl-test-001", closeout=True)

    assert changed is True
    assert report["blocking"] is True



def test_closeout_reconciles_legacy_sender_from_dispatch_record(tmp_path: Path) -> None:
    store = _canonical_hchat_closeout_case(tmp_path)
    reviews_path = store.loop_dir("sl-test-001") / "receipt_reviews.json"
    reviews = store.load_loop_json_list(reviews_path)
    for key in ("receipt_from_agent", "receipt_from_instance"):
        reviews[0].pop(key, None)
    store.save_loop_json_list(reviews_path, reviews)

    report = validate_loop(store, "sl-test-001", closeout=True)

    assert report["blocking"] is False



def test_closeout_uses_verified_new_sender_without_dispatch_metadata(tmp_path: Path) -> None:
    store = _canonical_hchat_closeout_case(tmp_path)
    root = store.loop_dir("sl-test-001")
    reviews = store.load_loop_json_list(root / "receipt_reviews.json")
    assert reviews[0]["receipt_from_agent"] == "worker"
    assert reviews[0]["receipt_from_instance"] == "HASHI3"
    ledger_path = root / "dispatches.jsonl"
    rows = [json.loads(line) for line in ledger_path.read_text(encoding="utf-8").splitlines()]
    for row in rows:
        row.pop("owner_agent", None)
        row.pop("owner_instance", None)
    ledger_path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")

    report = validate_loop(store, "sl-test-001", closeout=True)

    assert report["blocking"] is False
