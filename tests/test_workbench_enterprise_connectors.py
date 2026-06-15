from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestrator.enterprise import ConnectorHealth, EnterpriseRole
from orchestrator.workbench_api import WorkbenchApiServer


class _FakeRequest:
    def __init__(self, *, headers: dict | None = None, path: str = "", query: dict | None = None):
        self.headers = headers or {}
        self.path = path
        self.query = query or {}


class _FakeConnector:
    connector_type = "github"

    def health_check(self):
        return ConnectorHealth(ok=True, status="healthy", message="ready", data={"mode": "test"})

    def execute(self, action):
        raise AssertionError("not used")


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
