from __future__ import annotations

import json

from orchestrator.enterprise import EnterpriseAuditLedger, IdentityService
from orchestrator.enterprise.audit_adapters import (
    ingest_browser_action_audit_jsonl,
    ingest_remote_audit_jsonl,
    ingest_slash_command_audit_jsonl,
    ingest_token_audit_jsonl,
    ingest_tool_action_audit_jsonl,
)
from orchestrator.slash_command_audit import build_audit_record


def _init_ledger(tmp_path, org_id: str = "ORG-001") -> EnterpriseAuditLedger:
    db_path = tmp_path / "state" / "enterprise.sqlite"
    identity = IdentityService.from_path(db_path)
    identity.create_organization(org_id=org_id, name="Acme")
    return EnterpriseAuditLedger.from_path(db_path, org_id=org_id)


def test_ingests_slash_command_audit_jsonl_into_ledger(tmp_path):
    ledger = _init_ledger(tmp_path)
    audit_path = tmp_path / "slash_command_audit.jsonl"
    record = build_audit_record(
        agent="nana",
        command_name="backend",
        args=["grok-cli"],
        source_channel="telegram",
        handler_kind="native",
        status="success",
        duration_ms=42,
        actor_id="usr-1",
        chat_id="chat-1",
        side_effects=["backend_switch"],
        ts="2026-06-14T12:00:00+00:00",
    )
    audit_path.write_text(json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8")

    result = ingest_slash_command_audit_jsonl(ledger, audit_path)

    assert result.ingested == 1
    assert result.skipped == 0
    assert result.duplicate == 0
    events = ledger.query(event_type="slash_command")
    assert len(events) == 1
    event = events[0]
    assert event.ts == "2026-06-14T12:00:00+00:00"
    assert event.actor_id == "usr-1"
    assert event.action == "slash.backend"
    assert event.status == "success"
    assert event.context["agent"] == "nana"
    assert event.context["args_redacted"] == ["grok-cli"]
    assert event.context["source_channel"] == "telegram"
    assert event.context["side_effects"] == ["backend_switch"]
    assert event.context["legacy_line"] == 1


def test_slash_audit_ingest_is_idempotent(tmp_path):
    ledger = _init_ledger(tmp_path)
    audit_path = tmp_path / "slash_command_audit.jsonl"
    record = build_audit_record(
        agent="nana",
        command_name="status",
        source_channel="workbench_api",
        handler_kind="registry",
        status="blocked",
        duration_ms=7,
        actor_id="usr-2",
        blocked_reason="command_disabled",
        ts="2026-06-14T12:01:00+00:00",
    )
    audit_path.write_text(json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8")

    first = ingest_slash_command_audit_jsonl(ledger, audit_path)
    second = ingest_slash_command_audit_jsonl(ledger, audit_path)

    assert first.ingested == 1
    assert second.ingested == 0
    assert second.duplicate == 1
    events = ledger.query(event_type="slash_command")
    assert len(events) == 1
    assert events[0].status == "blocked"
    assert events[0].context["blocked_reason"] == "command_disabled"


def test_slash_audit_ingest_skips_malformed_lines(tmp_path):
    ledger = _init_ledger(tmp_path)
    audit_path = tmp_path / "slash_command_audit.jsonl"
    valid = build_audit_record(
        agent="nana",
        command_name="queue",
        source_channel="telegram",
        handler_kind="registry",
        status="failed",
        duration_ms=3,
        error="boom",
    )
    audit_path.write_text(
        "\n".join(
            [
                "{not-json",
                json.dumps(["not", "object"]),
                json.dumps(valid, ensure_ascii=False),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    result = ingest_slash_command_audit_jsonl(ledger, audit_path)

    assert result.ingested == 1
    assert result.skipped == 2
    assert len(result.errors) == 2
    event = ledger.query(event_type="slash_command")[0]
    assert event.action == "slash.queue"
    assert event.status == "failed"
    assert event.context["error"] == "boom"


def test_ingests_token_audit_jsonl_into_ledger(tmp_path):
    ledger = _init_ledger(tmp_path)
    audit_path = tmp_path / "token_audit.jsonl"
    record = {
        "ts": "2026-06-14T13:00:00+00:00",
        "request_id": "req-1",
        "request_fingerprint": "fingerprint-1",
        "agent": "zelda",
        "runtime": "flex",
        "completion_path": "foreground",
        "backend": "codex-cli",
        "model": "gpt-5.5",
        "source": "scheduler",
        "summary": "Nudge Task",
        "success": True,
        "input_tokens": 123,
        "output_tokens": 45,
        "thinking_tokens": 6,
        "tool_call_count": 2,
        "wrapper_used": False,
    }
    audit_path.write_text(json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8")

    result = ingest_token_audit_jsonl(ledger, audit_path)

    assert result.ingested == 1
    event = ledger.query(event_type="model_invocation")[0]
    assert event.ts == "2026-06-14T13:00:00+00:00"
    assert event.action == "model.invoke"
    assert event.status == "success"
    assert event.request_id == "req-1"
    assert event.correlation_id == "fingerprint-1"
    assert event.context["backend"] == "codex-cli"
    assert event.context["model"] == "gpt-5.5"
    assert event.context["input_tokens"] == 123
    assert event.context["tool_call_count"] == 2


def test_token_audit_ingest_is_idempotent_and_skips_bad_lines(tmp_path):
    ledger = _init_ledger(tmp_path)
    audit_path = tmp_path / "token_audit.jsonl"
    record = {
        "ts": "2026-06-14T13:01:00+00:00",
        "request_id": "req-2",
        "backend": "grok-cli",
        "model": "grok-composer-2.5-fast",
        "success": False,
        "error": "empty answer",
    }
    audit_path.write_text(
        "{bad-json\n" + json.dumps(record, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    first = ingest_token_audit_jsonl(ledger, audit_path)
    second = ingest_token_audit_jsonl(ledger, audit_path)

    assert first.ingested == 1
    assert first.skipped == 1
    assert second.ingested == 0
    assert second.skipped == 1
    assert second.duplicate == 1
    event = ledger.query(event_type="model_invocation")[0]
    assert event.status == "failed"
    assert event.request_id == "req-2"
    assert event.context["error"] == "empty answer"


def test_ingests_remote_audit_jsonl_into_ledger(tmp_path):
    ledger = _init_ledger(tmp_path)
    audit_path = tmp_path / "remote_audit.jsonl"
    records = [
        {
            "ts": 1778395658.0,
            "event": "hchat_received",
            "from": "HASHI2",
            "to_agent": "nana",
            "snippet": "[hchat from zelda@HASHI2] please review",
        },
        {
            "ts": 1778395659.0,
            "event": "terminal_exec",
            "client": "remote-cli",
            "command": "ls",
            "allowed": False,
        },
    ]
    audit_path.write_text(
        "\n".join(json.dumps(record, ensure_ascii=False) for record in records) + "\n",
        encoding="utf-8",
    )

    result = ingest_remote_audit_jsonl(ledger, audit_path)

    assert result.ingested == 2
    events = ledger.query(event_type="remote")
    assert [event.action for event in events] == ["remote.hchat_received", "remote.terminal_exec"]
    assert events[0].status == "observed"
    assert events[0].actor_id == "HASHI2"
    assert events[0].ts == "2026-05-10T06:47:38+00:00"
    assert events[0].context["to_agent"] == "nana"
    assert events[1].status == "denied"
    assert events[1].actor_id == "remote-cli"
    assert events[1].context["command"] == "ls"


def test_remote_audit_ingest_is_idempotent_and_skips_bad_lines(tmp_path):
    ledger = _init_ledger(tmp_path)
    audit_path = tmp_path / "remote_audit.jsonl"
    record = {
        "ts": "2026-06-14T14:00:00+00:00",
        "event": "pairing_request",
        "client": "HASHI2",
        "name": "hashi2 peer",
        "auto_approved": False,
    }
    audit_path.write_text(
        json.dumps(["not", "object"]) + "\n" + json.dumps(record, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    first = ingest_remote_audit_jsonl(ledger, audit_path)
    second = ingest_remote_audit_jsonl(ledger, audit_path)

    assert first.ingested == 1
    assert first.skipped == 1
    assert second.ingested == 0
    assert second.skipped == 1
    assert second.duplicate == 1
    event = ledger.query(event_type="remote")[0]
    assert event.action == "remote.pairing_request"
    assert event.status == "pending"
    assert event.actor_id == "HASHI2"


def test_ingests_browser_action_audit_jsonl_into_ledger(tmp_path):
    ledger = _init_ledger(tmp_path)
    audit_path = tmp_path / "browser_action_audit.jsonl"
    record = {
        "ts": 1781256728.0,
        "kind": "browser_action",
        "action": "evaluate",
        "request_id": "req-browser",
        "session_id": "browser-session",
        "args": {
            "url": "https://example.com",
            "agent_name": "sakura",
            "_audit": {"agent_name": "sakura", "call_id": "call-1"},
        },
        "response": {"ok": True, "output": "done"},
        "elapsed_ms": 15,
    }
    audit_path.write_text(json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8")

    result = ingest_browser_action_audit_jsonl(ledger, audit_path)

    assert result.ingested == 1
    event = ledger.query(event_type="tool")[0]
    assert event.action == "browser.evaluate"
    assert event.status == "success"
    assert event.actor_id == "sakura"
    assert event.request_id == "req-browser"
    assert event.correlation_id == "browser-session"
    assert event.ts == "2026-06-12T09:32:08+00:00"
    assert event.context["legacy_source"] == "browser_action_audit"
    assert event.context["response"]["ok"] is True


def test_browser_action_audit_ingest_is_idempotent_and_skips_bad_lines(tmp_path):
    ledger = _init_ledger(tmp_path)
    audit_path = tmp_path / "browser_action_audit.jsonl"
    record = {
        "ts": "2026-06-14T15:00:00+00:00",
        "kind": "browser_action",
        "action": "active_tab",
        "request_id": "req-tab",
        "args": {},
        "response": {"ok": False, "error": "bridge down"},
    }
    audit_path.write_text(
        json.dumps({"not": "browser"}) + "\n" + json.dumps(record, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    first = ingest_browser_action_audit_jsonl(ledger, audit_path)
    second = ingest_browser_action_audit_jsonl(ledger, audit_path)

    assert first.ingested == 2
    assert second.ingested == 0
    assert second.duplicate == 2
    events = ledger.query(event_type="tool")
    by_action = {event.action: event for event in events}
    assert set(by_action) == {"browser.unknown", "browser.active_tab"}
    assert by_action["browser.active_tab"].status == "failed"


def test_ingests_tool_action_audit_jsonl_into_ledger(tmp_path):
    ledger = _init_ledger(tmp_path)
    audit_path = tmp_path / "tool_action_audit.jsonl"
    record = {
        "ts": 1781256800.0,
        "kind": "tool_action",
        "tool_name": "file_write",
        "tool_call_id": "call-file",
        "agent": "nana",
        "workspace_dir": str(tmp_path),
        "safety_mode": "read_write",
        "status": "success",
        "is_error": False,
        "duration_ms": 11,
        "args_redacted": {"path": "report.md", "content": "hello"},
        "output_snippet": "OK: wrote 5 characters",
    }
    audit_path.write_text(json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8")

    result = ingest_tool_action_audit_jsonl(ledger, audit_path)

    assert result.ingested == 1
    event = ledger.query(event_type="tool")[0]
    assert event.action == "tool.file_write"
    assert event.status == "success"
    assert event.actor_id == "nana"
    assert event.correlation_id == "call-file"
    assert event.ts == "2026-06-12T09:33:20+00:00"
    assert event.context["args_redacted"]["path"] == "report.md"
    assert event.context["output_snippet"] == "OK: wrote 5 characters"


def test_tool_action_audit_ingest_is_idempotent_and_skips_bad_lines(tmp_path):
    ledger = _init_ledger(tmp_path)
    audit_path = tmp_path / "tool_action_audit.jsonl"
    record = {
        "ts": "2026-06-14T16:00:00+00:00",
        "kind": "tool_action",
        "tool_name": "bash",
        "tool_call_id": "call-bash",
        "agent": "zelda",
        "status": "failed",
        "is_error": True,
        "args_redacted": {"command": "rm -rf /tmp/example"},
        "output_snippet": "Error: blocked",
    }
    audit_path.write_text(
        "{bad-json\n" + json.dumps(record, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    first = ingest_tool_action_audit_jsonl(ledger, audit_path)
    second = ingest_tool_action_audit_jsonl(ledger, audit_path)

    assert first.ingested == 1
    assert first.skipped == 1
    assert second.ingested == 0
    assert second.skipped == 1
    assert second.duplicate == 1
    event = ledger.query(event_type="tool")[0]
    assert event.action == "tool.bash"
    assert event.status == "failed"
    assert event.actor_id == "zelda"
