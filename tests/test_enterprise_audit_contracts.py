from __future__ import annotations

import json

from orchestrator.enterprise import EnterpriseAuditLedger, IdentityService


REQUIRED_LEDGER_KEYS = {
    "id",
    "org_id",
    "ts",
    "schema_version",
    "event_type",
    "actor_id",
    "action",
    "status",
    "project_id",
    "task_id",
    "request_id",
    "correlation_id",
    "parent_event_id",
    "context",
}

CANONICAL_EVENT_TYPES = {
    "policy": ("file.write", "denied"),
    "channel": ("channel.access", "denied"),
    "slash_command": ("slash.backend", "success"),
    "model_invocation": ("model.invoke", "success"),
    "remote": ("remote.hchat_received", "observed"),
    "tool": ("tool.file_write", "success"),
}


def _init_ledger(tmp_path, org_id: str = "ORG-001") -> EnterpriseAuditLedger:
    db_path = tmp_path / "state" / "enterprise.sqlite"
    identity = IdentityService.from_path(db_path)
    identity.create_organization(org_id=org_id, name="Acme")
    return EnterpriseAuditLedger.from_path(db_path, org_id=org_id)


def test_ledger_event_dict_has_stable_required_keys(tmp_path):
    ledger = _init_ledger(tmp_path)

    event = ledger.append(
        event_type="policy",
        actor_id="usr-1",
        action="backend.switch",
        status="approval_required",
        project_id="prj-1",
        task_id="task-1",
        request_id="req-1",
        correlation_id="corr-1",
        parent_event_id="parent-1",
        context={"resource": "backend:grok-cli"},
    )

    payload = event.to_dict()
    assert set(payload) == REQUIRED_LEDGER_KEYS
    assert payload["schema_version"] == 1
    assert payload["context"] == {"resource": "backend:grok-cli"}


def test_canonical_enterprise_event_types_are_queryable_and_exportable(tmp_path):
    ledger = _init_ledger(tmp_path)

    for event_type, (action, status) in CANONICAL_EVENT_TYPES.items():
        ledger.append(
            event_type=event_type,
            actor_id="usr-1",
            action=action,
            status=status,
            request_id=f"req-{event_type}",
            context={"contract_event_type": event_type},
        )

    for event_type in CANONICAL_EVENT_TYPES:
        events = ledger.query(event_type=event_type)
        assert len(events) == 1
        assert events[0].event_type == event_type
        assert set(events[0].to_dict()) == REQUIRED_LEDGER_KEYS

    export_path = tmp_path / "exports" / "audit.jsonl"
    ledger.export_jsonl(export_path, limit=20)
    rows = [json.loads(line) for line in export_path.read_text(encoding="utf-8").splitlines()]
    assert {row["event_type"] for row in rows} == set(CANONICAL_EVENT_TYPES)
    assert all(set(row) == REQUIRED_LEDGER_KEYS for row in rows)


def test_ledger_context_is_json_safe(tmp_path):
    ledger = _init_ledger(tmp_path)

    event = ledger.append(
        event_type="tool",
        action="tool.file_write",
        status="success",
        context={
            "path": tmp_path / "report.md",
            "tuple": ("a", 1),
            "nested": {"items": {1, 2}},
        },
    )

    payload = event.to_dict()
    json.dumps(payload, ensure_ascii=False)
    assert payload["context"]["path"].startswith("PosixPath(")
    assert payload["context"]["tuple"] == ["a", 1]
    assert sorted(payload["context"]["nested"]["items"]) == [1, 2]
