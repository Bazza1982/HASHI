from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestrator.enterprise import EnterpriseRole
from orchestrator.workbench_api import WorkbenchApiServer


class _FakeRequest:
    def __init__(
        self,
        payload: dict | None = None,
        *,
        headers: dict | None = None,
        path: str = "",
        query: dict | None = None,
    ):
        self._payload = payload or {}
        self.headers = headers or {}
        self.path = path
        self.query = query or {}

    async def json(self):
        return self._payload


def _server(tmp_path: Path, *, profile: str = "enterprise") -> WorkbenchApiServer:
    config_path = tmp_path / "agents.json"
    config_path.write_text(
        json.dumps(
            {
                "global": {"deployment_profile": profile, "organization_id": "ORG-001"},
                "agents": [],
            }
        ),
        encoding="utf-8",
    )
    global_config = SimpleNamespace(
        deployment_profile=profile,
        organization_id="ORG-001",
        bridge_home=tmp_path,
        workbench_port=18800,
        project_root=tmp_path,
    )
    return WorkbenchApiServer(config_path=config_path, global_config=global_config)


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
async def test_enterprise_admin_can_query_audit_ledger(tmp_path):
    server = _server(tmp_path)
    headers = _admin_headers(server)
    server.audit_ledger.append(
        event_type="policy",
        actor_id="usr-1",
        action="command.execute",
        status="denied",
        project_id="prj-finance",
        context={"command_name": "backend"},
    )
    server.audit_ledger.append(
        event_type="channel",
        actor_id="usr-2",
        action="channel_access",
        status="denied",
        project_id="prj-research",
        context={"channel_type": "telegram"},
    )

    response = await server.handle_enterprise_audit(
        _FakeRequest(headers=headers, query={"event_type": "policy", "limit": "10"})
    )

    payload = json.loads(response.text)
    assert response.status == 200
    assert payload["ok"] is True
    assert payload["count"] == 1
    assert payload["events"][0]["event_type"] == "policy"
    assert payload["events"][0]["context"]["command_name"] == "backend"


@pytest.mark.asyncio
async def test_enterprise_admin_can_export_audit_ledger_as_ndjson(tmp_path):
    server = _server(tmp_path)
    headers = _admin_headers(server)
    server.audit_ledger.append(
        event_type="policy",
        actor_id="usr-1",
        action="backend.switch",
        status="approval_required",
        context={"approval_request_id": "appr-1"},
    )

    response = await server.handle_enterprise_audit_export(_FakeRequest(headers=headers))

    rows = [json.loads(line) for line in response.text.splitlines()]
    assert response.status == 200
    assert rows[0]["event_type"] == "policy"
    assert rows[0]["context"]["approval_request_id"] == "appr-1"


@pytest.mark.asyncio
async def test_enterprise_audit_api_rejects_individual_user(tmp_path):
    server = _server(tmp_path)
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

    response = await server.handle_enterprise_audit(
        _FakeRequest(headers={"Authorization": f"Bearer {session.token}"}, path="/api/enterprise/audit")
    )

    assert response.status == 403
    assert json.loads(response.text)["error"] == "admin auth failed"


@pytest.mark.asyncio
async def test_personal_profile_rejects_enterprise_audit_api(tmp_path):
    server = _server(tmp_path, profile="personal")

    response = await server.handle_enterprise_audit(_FakeRequest(path="/api/enterprise/audit"))

    assert response.status == 404
    assert json.loads(response.text)["error"] == "enterprise API requires governed profile"
