from __future__ import annotations

from orchestrator.enterprise import ConnectorAction, ConnectorHealth, ConnectorResult, EnterpriseAuditLedger, IdentityService
from orchestrator.enterprise.connectors import record_connector_event


class _FakeConnector:
    connector_type = "github"

    def health_check(self):
        return ConnectorHealth(ok=True, status="healthy", message="ready")

    def execute(self, action: ConnectorAction):
        return ConnectorResult(ok=True, status="success", message=f"executed {action.action}", data={"id": 123})


def test_connector_interface_can_execute_and_report_health():
    connector = _FakeConnector()
    action = ConnectorAction(connector_type="github", action="repo.read", resource="repo:hashi")

    assert connector.health_check().status == "healthy"
    assert connector.execute(action).data == {"id": 123}


def test_record_connector_event_writes_canonical_ledger_event_and_redacts_parameters(tmp_path):
    IdentityService.from_path(tmp_path / "enterprise.sqlite").create_organization(org_id="ORG-001", name="Acme")
    ledger = EnterpriseAuditLedger.from_path(tmp_path / "enterprise.sqlite", org_id="ORG-001")
    action = ConnectorAction(
        connector_type="github",
        action="pr.create",
        resource="repo:hashi",
        actor_id="usr-1",
        project_id="prj-research",
        task_id="task-1",
        request_id="req-1",
        correlation_id="corr-1",
        parameters={"title": "Add feature", "token": "secret-token"},
    )
    result = ConnectorResult(ok=True, status="success", message="created", data={"url": "https://example.test/pr/1"})

    event = record_connector_event(ledger, action, result, credential_id="cred-github")

    assert event.event_type == "connector"
    assert event.action == "github.pr.create"
    assert event.status == "success"
    assert event.actor_id == "usr-1"
    assert event.project_id == "prj-research"
    assert event.task_id == "task-1"
    assert event.request_id == "req-1"
    assert event.correlation_id == "corr-1"
    assert event.context["connector_type"] == "github"
    assert event.context["credential_id"] == "cred-github"
    assert event.context["parameters"]["title"] == "Add feature"
    assert event.context["parameters"]["token"] == "[REDACTED]"
