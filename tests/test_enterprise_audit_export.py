from __future__ import annotations

from orchestrator.enterprise import (
    EnterpriseAuditLedger,
    IdentityService,
    format_elastic_bulk_action,
    format_otel_log,
    format_siem_event,
    format_splunk_hec_event,
)


def _ledger(tmp_path) -> EnterpriseAuditLedger:
    db_path = tmp_path / "state" / "enterprise.sqlite"
    identity = IdentityService.from_path(db_path)
    identity.create_organization(org_id="ORG-001", name="Acme")
    return EnterpriseAuditLedger.from_path(db_path, org_id="ORG-001")


def test_formats_audit_event_for_siem_ingestion(tmp_path):
    ledger = _ledger(tmp_path)
    event = ledger.append(
        event_type="policy",
        actor_id="usr-1",
        action="file.write",
        status="denied",
        project_id="prj-1",
        correlation_id="corr-1",
        context={"resource": "file:/tmp/report.md"},
    )

    payload = format_siem_event(event)

    assert payload["@timestamp"] == event.ts
    assert payload["event"]["id"] == event.id
    assert payload["event"]["category"] == ["configuration"]
    assert payload["event"]["outcome"] == "failure"
    assert payload["organization"]["id"] == "ORG-001"
    assert payload["user"]["id"] == "usr-1"
    assert payload["trace"]["id"] == "corr-1"
    assert payload["labels"]["hashi.audit.event_hash"] == event.event_hash
    assert payload["hashi"]["audit"]["context"]["resource"] == "file:/tmp/report.md"


def test_formats_audit_event_for_opentelemetry_log_ingestion(tmp_path):
    ledger = _ledger(tmp_path)
    event = ledger.append(
        event_type="connector",
        actor_id="usr-1",
        action="message.send",
        status="success",
        request_id="req-1",
        context={"connector_type": "slack"},
    )

    payload = format_otel_log(event)

    assert payload["time_unix_nano"] > 0
    assert payload["severity_text"] == "INFO"
    assert payload["severity_number"] == 9
    assert payload["body"] == "connector.message.send success"
    assert payload["attributes"]["hashi.audit.id"] == event.id
    assert payload["attributes"]["hashi.audit.event_hash"] == event.event_hash
    assert payload["attributes"]["hashi.audit.context"]["connector_type"] == "slack"


def test_formats_audit_event_for_splunk_hec_envelope(tmp_path):
    ledger = _ledger(tmp_path)
    event = ledger.append(event_type="policy", action="shell.exec", status="denied", actor_id="usr-1")

    payload = format_splunk_hec_event(event)

    assert payload["time"] > 0
    assert payload["host"] == "hashi"
    assert payload["source"] == "hashi.enterprise.audit"
    assert payload["sourcetype"] == "_json"
    assert payload["event"]["event"]["id"] == event.id
    assert payload["fields"]["hashi_audit_id"] == event.id
    assert payload["fields"]["hashi_event_hash"] == event.event_hash


def test_formats_elastic_bulk_create_action(tmp_path):
    ledger = _ledger(tmp_path)
    event = ledger.append(event_type="auth", action="login", status="success")

    payload = format_elastic_bulk_action(event)

    assert payload == {"create": {"_id": event.id}}
