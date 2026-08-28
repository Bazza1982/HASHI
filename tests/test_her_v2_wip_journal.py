from __future__ import annotations

import json

from orchestrator.her_v2.audit import DurableAuditLog
from orchestrator.her_v2.wip_journal import (
    CONTEXT_HEADER,
    MAX_CONTEXT_CHARS,
    MAX_FILE_BYTES,
    MAX_RECORDS,
    WIPJournal,
)


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
    assert "current user request remains authoritative" in context
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


def test_activity_summary_reports_content_free_recovery_identity(tmp_path):
    path = tmp_path / "wip.jsonl"
    journal = WIPJournal(path)

    empty = journal.activity_summary()
    assert empty["record_count"] == 0
    assert empty["size_bytes"] == 0
    assert empty["generation_id"] == ""
    journal.begin_turn(request_id="req-1", prompt="private unfinished work")

    summary = journal.activity_summary()
    assert summary["record_count"] == 1
    assert summary["size_bytes"] > 0
    assert summary["generation_id"].startswith("sha256:")
    assert summary["first_request_id"] == "req-1"
    assert summary["last_request_id"] == "req-1"


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


def test_legacy_prompt_is_hashed_but_not_copied_into_bounded_recovery(tmp_path):
    path = tmp_path / "wip.jsonl"
    secret_prompt = "legacy raw prompt must not survive migration"
    path.write_text(
        json.dumps(
            {
                "format": "her-v2-wip-journal-v1",
                "kind": "turn_received",
                "request_id": "req-legacy",
                "prompt": secret_prompt,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    journal = WIPJournal(path)

    records = journal.records()
    rendered = journal.render_context(records)

    assert records[0]["prompt_chars"] == len(secret_prompt)
    assert records[0]["prompt_sha256"].startswith("sha256:")
    assert secret_prompt not in rendered
    assert "raw prompt content was omitted" in rendered


def test_full_request_audit_is_never_copied_back_or_recursively_expanded(tmp_path):
    path = tmp_path / "wip.jsonl"
    journal = WIPJournal(path)
    oversized_request = "x" * 1_000_000

    for index in range(12):
        prior = journal.begin_turn(
            request_id=f"req-{index}",
            prompt=oversized_request,
            request_summary=f"attempt {index}",
        )
        journal.append_audit(
            {
                "event": "request_received",
                "request_ref": f"hashi-request:req-{index}",
                "payload": {"request": oversized_request},
            }
        )
        journal.append_audit(
            {
                "event": "stage_attempt_failed",
                "stage": "direct",
                "request_ref": f"hashi-request:req-{index}",
                "payload": {
                    "error_code": "PROVIDER_BAD_REQUEST",
                    "http_status": 413,
                    "human_description": "request too large",
                },
            }
        )
        assert len(prior) <= MAX_CONTEXT_CHARS

    assert path.stat().st_size <= MAX_FILE_BYTES
    assert all(row.get("event") != "request_received" for row in journal.records())
    assert oversized_request not in path.read_text(encoding="utf-8")


def test_bounded_rewrite_retains_first_unfinished_request_boundary(tmp_path):
    journal = WIPJournal(tmp_path / "wip.jsonl")
    journal.begin_turn(request_id="req-first", prompt="first unfinished task")

    for index in range(300):
        journal.append_audit(
            {
                "event": "tool_completed",
                "event_id": f"evt-{index}",
                "payload": {"output": f"bounded result {index}"},
            }
        )

    snapshot = journal.snapshot()
    assert snapshot.record_count <= MAX_RECORDS
    assert snapshot.first_request_id == "req-first"
    assert "first unfinished task" in journal.render_context(snapshot.records)


def test_clear_if_unchanged_is_compare_and_swap_safe(tmp_path):
    journal = WIPJournal(tmp_path / "wip.jsonl")
    journal.begin_turn(request_id="req-1", prompt="first")
    snapshot = journal.snapshot()
    journal.append_audit(
        {
            "event": "stage_attempt_failed",
            "payload": {"error_code": "PROVIDER_TIMEOUT"},
        }
    )

    assert journal.clear_if_unchanged(snapshot.file_sha256) is False
    assert journal.snapshot().active is True
    latest = journal.snapshot()
    assert journal.clear_if_unchanged(latest.file_sha256) is True
    assert journal.snapshot().active is False


def test_recovery_capsule_preserves_failure_without_raw_provider_payload(tmp_path):
    journal = WIPJournal(tmp_path / "wip.jsonl")
    journal.begin_turn(
        request_id="req-1",
        prompt="large composed prompt",
        request_summary="repair the workbook",
    )
    journal.append_audit(
        {
            "event": "stage_attempt_failed",
            "stage": "execution",
            "provider": "test-provider",
            "model": "test-model",
            "payload": {
                "error_code": "PROVIDER_TIMEOUT",
                "human_description": "provider timed out",
                "request": "private raw provider request",
            },
        }
    )

    snapshot = journal.snapshot()
    capsule = journal.recovery_capsule(
        snapshot.records,
        source_sha256=snapshot.file_sha256,
    )
    repeated = journal.recovery_capsule(
        snapshot.records,
        source_sha256=snapshot.file_sha256,
    )
    encoded = json.dumps(capsule, ensure_ascii=False)

    assert "PROVIDER_TIMEOUT" in encoded
    assert "provider timed out" in encoded
    assert "private raw provider request" not in encoded
    assert capsule["authority"] == "quoted_recovery_context"
    assert capsule == repeated


def test_recovery_capsule_does_not_label_normal_lifecycle_transition_as_failure(
    tmp_path,
):
    journal = WIPJournal(tmp_path / "wip.jsonl")
    journal.begin_turn(request_id="req-1", prompt="unfinished")
    journal.append_audit(
        {
            "event": "transition",
            "stage": "lifecycle",
            "payload": {"from": "RECEIVED", "to": "EXECUTING"},
        }
    )
    journal.append_audit(
        {
            "event": "transition",
            "stage": "lifecycle",
            "payload": {
                "from": "EXECUTING",
                "to": "ERROR",
                "terminal_reason": "provider failed",
            },
        }
    )

    capsule = journal.recovery_capsule(journal.snapshot().records)

    assert len(capsule["failures"]) == 1
    assert capsule["failures"][0]["facts"]["to"] == "ERROR"


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
