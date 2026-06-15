from __future__ import annotations

import json

from orchestrator.enterprise import AuditEvent, EnterpriseAuditLedger, IdentityService


def _init_org(tmp_path, org_id: str = "ORG-001") -> None:
    identity = IdentityService.from_path(tmp_path / "state" / "enterprise.sqlite")
    identity.create_organization(org_id=org_id, name="Acme")


def test_audit_ledger_appends_and_queries_events(tmp_path):
    _init_org(tmp_path)
    ledger = EnterpriseAuditLedger.from_path(tmp_path / "state" / "enterprise.sqlite", org_id="ORG-001")

    first = ledger.append(
        event_type="policy",
        actor_id="usr-1",
        action="file.write",
        status="denied",
        project_id="prj-finance",
        request_id="req-1",
        correlation_id="corr-1",
        context={"resource": "file:/tmp/report.md"},
    )
    ledger.append(
        event_type="channel",
        actor_id="usr-2",
        action="channel_access",
        status="denied",
        project_id="prj-research",
        context={"channel_type": "telegram"},
    )

    by_type = ledger.query(event_type="policy")
    by_actor = ledger.query(actor_id="usr-1")
    by_project = ledger.query(project_id="prj-finance")

    assert by_type == [first]
    assert by_actor == [first]
    assert by_project == [first]
    assert first.schema_version == 1
    assert first.context["resource"] == "file:/tmp/report.md"


def test_audit_ledger_appends_existing_audit_event(tmp_path):
    _init_org(tmp_path)
    ledger = EnterpriseAuditLedger.from_path(tmp_path / "state" / "enterprise.sqlite", org_id="ORG-001")
    event = AuditEvent(
        event_type="policy",
        actor_id="usr-1",
        action="backend.switch",
        status="approval_required",
        context={"approval_request_id": "appr-1"},
    )

    stored = ledger.append_audit_event(event, task_id="task-1", correlation_id="corr-1")

    assert stored.event_type == "policy"
    assert stored.ts == event.ts
    assert stored.task_id == "task-1"
    assert stored.correlation_id == "corr-1"
    assert stored.context["approval_request_id"] == "appr-1"


def test_audit_ledger_exports_jsonl(tmp_path):
    _init_org(tmp_path)
    ledger = EnterpriseAuditLedger.from_path(tmp_path / "state" / "enterprise.sqlite", org_id="ORG-001")
    ledger.append(
        event_type="policy",
        actor_id="usr-1",
        action="command.execute",
        status="denied",
        context={"command_name": "backend"},
    )
    export_path = tmp_path / "exports" / "audit.jsonl"

    result_path = ledger.export_jsonl(export_path)

    assert result_path == export_path
    rows = [json.loads(line) for line in export_path.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1
    assert rows[0]["schema_version"] == 1
    assert rows[0]["event_type"] == "policy"
    assert rows[0]["context"]["command_name"] == "backend"
