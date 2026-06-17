from __future__ import annotations

import json

import pytest

from orchestrator.enterprise import (
    AuditLiveExportEndpoint,
    AuditLiveExporter,
    EnterpriseAuditLedger,
    FileAuditLiveExportCheckpoint,
    IdentityService,
)


def _ledger(tmp_path) -> EnterpriseAuditLedger:
    db_path = tmp_path / "state" / "enterprise.sqlite"
    identity = IdentityService.from_path(db_path)
    identity.create_organization(org_id="ORG-001", name="Acme")
    return EnterpriseAuditLedger.from_path(db_path, org_id="ORG-001")


def test_live_export_pushes_siem_ndjson_and_returns_checkpoint(tmp_path):
    ledger = _ledger(tmp_path)
    first = ledger.append(event_type="policy", action="file.write", status="denied", actor_id="usr-1")
    second = ledger.append(event_type="connector", action="message.send", status="success", actor_id="usr-2")
    calls = []

    def transport(url, body, headers, timeout):
        calls.append((url, body, headers, timeout))
        return 202, "accepted"

    result = AuditLiveExporter(ledger, transport=transport).export_since(
        AuditLiveExportEndpoint(
            url="https://siem.example.com/ingest",
            format="siem",
            headers={"Authorization": "Bearer test"},
            batch_size=10,
        )
    )

    assert result.sent == 2
    assert result.last_chain_index == second.chain_index
    url, body, headers, timeout = calls[0]
    rows = [json.loads(line) for line in body.decode("utf-8").splitlines()]
    assert url == "https://siem.example.com/ingest"
    assert headers["content-type"] == "application/x-ndjson"
    assert headers["authorization"] == "Bearer test"
    assert timeout == 10.0
    assert rows[0]["event"]["id"] == first.id
    assert rows[1]["event"]["id"] == second.id


def test_live_export_skips_events_at_or_before_checkpoint(tmp_path):
    ledger = _ledger(tmp_path)
    first = ledger.append(event_type="policy", action="old", status="success")
    second = ledger.append(event_type="policy", action="new", status="success")
    calls = []

    def transport(url, body, headers, timeout):
        calls.append(json.loads(body.decode("utf-8").splitlines()[0]))
        return 200, "ok"

    result = AuditLiveExporter(ledger, transport=transport).export_since(
        AuditLiveExportEndpoint(url="https://siem.example.com/ingest", format="ledger"),
        checkpoint_chain_index=first.chain_index,
    )

    assert result.sent == 1
    assert result.last_chain_index == second.chain_index
    assert calls[0]["id"] == second.id


def test_live_export_builds_otlp_json_payload(tmp_path):
    ledger = _ledger(tmp_path)
    event = ledger.append(event_type="auth", action="login", status="success", actor_id="usr-1")
    calls = []

    def transport(url, body, headers, timeout):
        calls.append((body, headers))
        return 200, "ok"

    result = AuditLiveExporter(ledger, transport=transport).export_since(
        AuditLiveExportEndpoint(url="https://otel.example.com/v1/logs", format="otel")
    )

    payload = json.loads(calls[0][0].decode("utf-8"))
    log_record = payload["resourceLogs"][0]["scopeLogs"][0]["logRecords"][0]
    assert result.sent == 1
    assert calls[0][1]["content-type"] == "application/json"
    assert log_record["attributes"]["hashi.audit.id"] == event.id
    assert log_record["body"] == "auth.login success"


def test_live_export_noops_when_checkpoint_is_current(tmp_path):
    ledger = _ledger(tmp_path)
    event = ledger.append(event_type="auth", action="login", status="success")

    def transport(_url, _body, _headers, _timeout):
        raise AssertionError("transport should not be called")

    result = AuditLiveExporter(ledger, transport=transport).export_since(
        AuditLiveExportEndpoint(url="https://siem.example.com/ingest"),
        checkpoint_chain_index=event.chain_index,
    )

    assert result.sent == 0
    assert result.last_chain_index == event.chain_index
    assert result.status_code is None


def test_live_export_fails_closed_without_transport_or_on_bad_status(tmp_path):
    ledger = _ledger(tmp_path)
    ledger.append(event_type="auth", action="login", status="success")

    with pytest.raises(ValueError, match="transport is required"):
        AuditLiveExporter(ledger).export_since(AuditLiveExportEndpoint(url="https://siem.example.com/ingest"))

    def transport(_url, _body, _headers, _timeout):
        return 500, "secret failure body that should be short"

    with pytest.raises(ValueError, match="HTTP 500"):
        AuditLiveExporter(ledger, transport=transport).export_since(
            AuditLiveExportEndpoint(url="https://siem.example.com/ingest")
        )


def test_live_export_with_checkpoint_persists_after_success(tmp_path):
    ledger = _ledger(tmp_path)
    first = ledger.append(event_type="auth", action="login", status="success")
    second = ledger.append(event_type="policy", action="allow", status="success")
    checkpoint = FileAuditLiveExportCheckpoint(tmp_path / "state" / "audit-live-export.json")
    calls = []

    def transport(url, body, headers, timeout):
        calls.append(body)
        return 200, "ok"

    cycle = AuditLiveExporter(ledger, transport=transport).export_with_checkpoint(
        AuditLiveExportEndpoint(url="https://siem.example.com/ingest", batch_size=10),
        checkpoint,
        max_attempts=1,
    )
    noop = AuditLiveExporter(ledger, transport=transport).export_with_checkpoint(
        AuditLiveExportEndpoint(url="https://siem.example.com/ingest", batch_size=10),
        checkpoint,
        max_attempts=1,
    )

    assert cycle.result.sent == 2
    assert cycle.result.last_chain_index == second.chain_index
    assert cycle.attempts == 1
    assert checkpoint.load() == second.chain_index
    assert noop.result.sent == 0
    assert len(calls) == 1
    assert str(first.id) in calls[0].decode("utf-8")


def test_live_export_with_checkpoint_retries_without_advancing_on_failure(tmp_path):
    ledger = _ledger(tmp_path)
    event = ledger.append(event_type="auth", action="login", status="success")
    checkpoint = FileAuditLiveExportCheckpoint(tmp_path / "state" / "audit-live-export.json")
    attempts = []
    sleeps = []

    def transport(url, body, headers, timeout):
        attempts.append(body)
        if len(attempts) == 1:
            return 503, "temporary"
        return 200, "ok"

    cycle = AuditLiveExporter(ledger, transport=transport).export_with_checkpoint(
        AuditLiveExportEndpoint(url="https://siem.example.com/ingest"),
        checkpoint,
        max_attempts=2,
        backoff_seconds=0.25,
        sleeper=sleeps.append,
    )

    assert cycle.result.sent == 1
    assert cycle.attempts == 2
    assert checkpoint.load() == event.chain_index
    assert sleeps == [0.25]
    assert len(attempts) == 2


def test_live_export_checkpoint_rejects_corrupt_state(tmp_path):
    checkpoint = FileAuditLiveExportCheckpoint(tmp_path / "state" / "audit-live-export.json")
    checkpoint.path.parent.mkdir(parents=True)
    checkpoint.path.write_text("not json", encoding="utf-8")

    with pytest.raises(ValueError, match="checkpoint is invalid"):
        checkpoint.load()
