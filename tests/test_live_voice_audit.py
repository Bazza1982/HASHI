"""Focused diagnostics tests for the Live Phone lifecycle audit."""

from __future__ import annotations

import json
from pathlib import Path

from orchestrator.frontend_live_voice.audit import LiveVoiceAuditLog
from orchestrator.frontend_live_voice.protocol import CallBinding


def _binding() -> CallBinding:
    return CallBinding(
        owner_id="owner",
        instance_id="HASHI1",
        instance_generation="1",
        agent_id="sunny",
        session_id="session-1",
        context_generation=1,
        call_id="call-audit-1",
        call_epoch=1,
        provider_session_id="provider-secret-id",
    )


def test_call_audit_is_append_only_and_excludes_private_provider_binding(tmp_path: Path):
    audit = LiveVoiceAuditLog(tmp_path)
    binding = _binding()

    assert audit.record(binding, "call.created", phase="connecting")
    assert audit.record(binding, "provider.session_closed", reason="connection_lost")

    records = [json.loads(line) for line in audit.path_for(binding).read_text(encoding="utf-8").splitlines()]
    assert [record["event"] for record in records] == ["call.created", "provider.session_closed"]
    assert all(record["schema"] == "hashi.live_voice.audit.v1" for record in records)
    assert all(record["scope"]["call_id"] == binding.call_id for record in records)
    assert "provider-secret-id" not in audit.path_for(binding).read_text(encoding="utf-8")


def test_call_audit_bounds_and_redacts_diagnostic_values(tmp_path: Path):
    audit = LiveVoiceAuditLog(tmp_path)
    binding = _binding()

    assert audit.record(
        binding,
        "sideband.exception",
        exception_type="RuntimeError",
        exception_message=(
            "Bearer sk-test-super-secret at "
            "wss://api.openai.com/v1/live/sessions/live_provider-private-id/attach"
        ),
        ignored={"nested": "not admitted"},
    )

    record = json.loads(audit.path_for(binding).read_text(encoding="utf-8"))
    assert record["detail"]["exception_type"] == "RuntimeError"
    assert "super-secret" not in json.dumps(record)
    assert "live_provider-private-id" not in json.dumps(record)
    assert "ignored" not in record["detail"]


def test_failed_dial_attempt_has_evidence_before_a_call_id_exists(tmp_path: Path):
    audit = LiveVoiceAuditLog(tmp_path)

    assert audit.record_attempt("attempt-1", "dial.provider_create_failed", error_code="provider_rejected")

    record = json.loads(audit.attempt_path("attempt-1").read_text(encoding="utf-8"))
    assert record["scope"] == {"attempt_id": "attempt-1"}
    assert record["detail"]["error_code"] == "provider_rejected"


def test_call_audit_keeps_bounded_provider_retry_evidence_durable(tmp_path: Path):
    audit = LiveVoiceAuditLog(tmp_path)
    binding = _binding()

    assert audit.record(
        binding,
        "provider.event_processing_failed",
        provider_event_type="session.input_transcript.delta",
        provider_event_id="provider-event-1",
        attempt=12,
        retry_delay_s=1.2,
        source_session_id="session-background",
        source_message_id="message-1",
        item_type="message",
    )
    assert audit.record(
        binding,
        "provider.event_persistence_recovered",
        provider_event_id="provider-event-1",
        attempts=13,
    )

    records = [json.loads(line) for line in audit.path_for(binding).read_text(encoding="utf-8").splitlines()]
    assert records[0]["detail"]["attempt"] == 12
    assert records[0]["detail"]["retry_delay_s"] == 1.2
    assert records[0]["detail"]["source_session_id"] == "session-background"
    assert records[0]["detail"]["source_message_id"] == "message-1"
    assert records[0]["detail"]["item_type"] == "message"
    assert records[1]["detail"]["attempts"] == 13
