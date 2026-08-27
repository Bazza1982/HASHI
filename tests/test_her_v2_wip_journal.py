from __future__ import annotations

import json

from orchestrator.her_v2.audit import DurableAuditLog
from orchestrator.her_v2.wip_journal import CONTEXT_HEADER, WIPJournal


def test_interrupted_wip_is_injected_and_appends_across_turns(tmp_path):
    path = tmp_path / "wip.jsonl"
    journal = WIPJournal(path)

    assert journal.begin_turn(request_id="req-1", prompt="first task") == ""
    journal.append_audit(
        {
            "event": "commentary_publish_result",
            "payload": {"text": "Checked the source workbook."},
        }
    )

    context = journal.begin_turn(request_id="req-2", prompt="what happened?")

    assert CONTEXT_HEADER in context
    assert "first task" in context
    assert "Checked the source workbook." in context
    assert "Do not continue it by default" in context
    assert [
        row["request_id"] for row in journal.records() if row["kind"] == "turn_received"
    ] == [
        "req-1",
        "req-2",
    ]


def test_completed_turn_clears_all_accumulated_wip(tmp_path):
    path = tmp_path / "wip.jsonl"
    journal = WIPJournal(path)
    journal.begin_turn(request_id="req-1", prompt="unfinished")
    journal.begin_turn(request_id="req-2", prompt="later turn")

    journal.clear_completed()

    assert journal.records() == []
    assert path.read_text(encoding="utf-8") == ""


def test_activity_summary_reports_only_count_and_size(tmp_path):
    path = tmp_path / "wip.jsonl"
    journal = WIPJournal(path)

    assert journal.activity_summary() == {"record_count": 0, "size_bytes": 0}
    journal.begin_turn(request_id="req-1", prompt="private unfinished work")

    summary = journal.activity_summary()
    assert summary["record_count"] == 1
    assert summary["size_bytes"] > 0
    assert set(summary) == {"record_count", "size_bytes"}


def test_crash_torn_tail_does_not_hide_prior_durable_wip(tmp_path):
    path = tmp_path / "wip.jsonl"
    journal = WIPJournal(path)
    journal.begin_turn(request_id="req-1", prompt="durable task")
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"format":"her-v2-wip-journal-v1","broken"')

    context = journal.begin_turn(request_id="req-2", prompt="next turn")

    assert "durable task" in context
    assert [
        row["request_id"] for row in journal.records() if row["kind"] == "turn_received"
    ] == [
        "req-1",
        "req-2",
    ]


def test_audit_observer_receives_only_after_durable_audit_write(tmp_path):
    observed = []
    audit = DurableAuditLog(
        tmp_path / "audit.jsonl",
        tmp_path / "fallback.jsonl",
        observer=lambda record: observed.append(dict(record)),
    )

    audit.append(
        event_id="event-1",
        turn_id="turn-1",
        request_ref="hashi-request:req-1",
        stage="execution",
        role="primary",
        event="stage_completed",
        payload={"output": "draft result"},
    )

    persisted = json.loads((tmp_path / "audit.jsonl").read_text(encoding="utf-8"))
    assert observed == [persisted]


def test_broken_wip_observer_does_not_change_canonical_audit(tmp_path):
    def broken(_record):
        raise OSError("journal unavailable")

    audit = DurableAuditLog(
        tmp_path / "audit.jsonl",
        tmp_path / "fallback.jsonl",
        observer=broken,
    )

    reference = audit.append(
        event_id="event-1",
        turn_id="turn-1",
        request_ref="hashi-request:req-1",
        stage="execution",
        role="primary",
        event="stage_completed",
    )

    assert reference.endswith(":event-1")
    assert (tmp_path / "audit.jsonl").is_file()
