from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestrator.enterprise import ConnectorHealth, ConnectorResult, EnterpriseRole, PolicyEvaluator
from orchestrator.workbench_api import WorkbenchApiServer


class _FakeRequest:
    def __init__(
        self,
        *,
        headers: dict | None = None,
        path: str = "",
        query: dict | None = None,
        body: dict | None = None,
    ):
        self.headers = headers or {}
        self.path = path
        self.query = query or {}
        self._body = body or {}

    async def json(self):
        return self._body


class _FakeConnector:
    connector_type = "github"

    def health_check(self):
        return ConnectorHealth(ok=True, status="healthy", message="ready", data={"mode": "test"})

    def execute(self, action):
        raise AssertionError("not used")


class _RecordingConnector:
    connector_type = "github"

    def __init__(self):
        self.actions = []

    def health_check(self):
        return ConnectorHealth(ok=True, status="healthy")

    def execute(self, action):
        self.actions.append(action)
        return ConnectorResult(ok=True, status="success", message="done", data={"resource": action.resource})


def _server(tmp_path: Path, *, connectors: list | None = None) -> WorkbenchApiServer:
    config_path = tmp_path / "agents.json"
    config_path.write_text(
        json.dumps(
            {
                "global": {"deployment_profile": "enterprise", "organization_id": "ORG-001"},
                "agents": [],
            }
        ),
        encoding="utf-8",
    )
    global_config = SimpleNamespace(
        deployment_profile="enterprise",
        organization_id="ORG-001",
        bridge_home=tmp_path,
        workbench_port=18800,
        project_root=tmp_path,
    )
    return WorkbenchApiServer(config_path=config_path, global_config=global_config, connectors=connectors)


def _admin_headers(server: WorkbenchApiServer) -> dict[str, str]:
    admin = server.identity_service.bootstrap_org_admin(
        org_id="ORG-001",
        org_name="Acme",
        email="admin@example.com",
        display_name="Admin",
        password="secret-password",
        user_id="usr-admin",
    )
    session = server.identity_service.create_session(user_id=admin.id)
    return {"Authorization": f"Bearer {session.token}"}


def _create_github_credential(server: WorkbenchApiServer):
    return server.connector_credentials.create_credential(
        org_id="ORG-001",
        connector_type="github",
        display_name="GitHub App",
        secret_ref="vault://github/app",
        scopes=["repo:read"],
        credential_id="cred-github",
    )


@pytest.mark.asyncio
async def test_enterprise_admin_can_read_connector_health(tmp_path):
    server = _server(tmp_path, connectors=[_FakeConnector()])
    headers = _admin_headers(server)

    response = await server.handle_enterprise_connector_health(
        _FakeRequest(headers=headers, path="/api/enterprise/connectors/health")
    )

    payload = json.loads(response.text)
    assert response.status == 200
    assert payload["ok"] is True
    assert payload["healthy"] is True
    assert payload["count"] == 1
    assert payload["connectors"][0] == {
        "connector_type": "github",
        "ok": True,
        "status": "healthy",
        "message": "ready",
        "data": {"mode": "test"},
    }
    events = server.audit_ledger.query(event_type="connector")
    assert len(events) == 1
    assert events[0].action == "github.health_check"


@pytest.mark.asyncio
async def test_enterprise_connector_health_requires_admin(tmp_path):
    server = _server(tmp_path, connectors=[_FakeConnector()])
    server.identity_service.bootstrap_org_admin(
        org_id="ORG-001",
        org_name="Acme",
        email="admin@example.com",
        display_name="Admin",
        password="secret-password",
    )
    user = server.identity_service.create_user(
        org_id="ORG-001",
        email="user@example.com",
        display_name="User",
        password="secret-password",
        user_id="usr-user",
    )
    server.identity_service.assign_project_role(
        user_id=user.id,
        project_id="ORG-001-default",
        role=EnterpriseRole.INDIVIDUAL_USER,
    )
    session = server.identity_service.create_session(user_id=user.id)

    response = await server.handle_enterprise_connector_health(
        _FakeRequest(
            headers={"Authorization": f"Bearer {session.token}"},
            path="/api/enterprise/connectors/health",
        )
    )

    assert response.status == 403
    assert json.loads(response.text)["ok"] is False


@pytest.mark.asyncio
async def test_enterprise_admin_can_execute_connector_through_gate(tmp_path):
    connector = _RecordingConnector()
    server = _server(tmp_path, connectors=[connector])
    headers = _admin_headers(server)
    _create_github_credential(server)

    response = await server.handle_enterprise_connector_execute(
        _FakeRequest(
            headers=headers,
            path="/api/enterprise/connectors/execute",
            body={
                "connector_type": "github",
                "action": "repo.read",
                "resource": "repo:Bazza1982/hashi",
                "credential_id": "cred-github",
                "project_id": "prj-research",
                "parameters": {"token": "redact-me"},
            },
        )
    )

    payload = json.loads(response.text)
    assert response.status == 200
    assert payload["ok"] is True
    assert payload["gate"]["allowed"] is True
    assert payload["result"]["data"] == {"resource": "repo:Bazza1982/hashi"}
    assert connector.actions[0].actor_id == "usr-admin"
    events = server.audit_ledger.query(event_type="connector")
    assert len(events) == 1
    assert events[0].context["parameters"]["token"] == "[REDACTED]"


@pytest.mark.asyncio
async def test_enterprise_connector_execute_honors_policy_deny(tmp_path):
    connector = _FakeConnector()
    server = _server(tmp_path, connectors=[connector])
    headers = _admin_headers(server)
    _create_github_credential(server)
    PolicyEvaluator.from_path(tmp_path / "state" / "enterprise.sqlite", org_id="ORG-001").add_rule(
        action="connector.execute",
        resource="connector:github:repo.read",
        effect="deny",
        rule_id="pol-deny-connector-read",
    )

    response = await server.handle_enterprise_connector_execute(
        _FakeRequest(
            headers=headers,
            path="/api/enterprise/connectors/execute",
            body={
                "connector_type": "github",
                "action": "repo.read",
                "resource": "repo:Bazza1982/hashi",
                "credential_id": "cred-github",
            },
        )
    )

    payload = json.loads(response.text)
    assert response.status == 200
    assert payload["ok"] is False
    assert payload["gate"]["allowed"] is False
    assert payload["gate"]["policy_rule_id"] == "pol-deny-connector-read"
    assert payload["result"]["status"] == "connector_action_denied"
