from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestrator.enterprise import EnterpriseRole
from orchestrator.workbench_api import WorkbenchApiServer


class _FakeRequest:
    def __init__(self, payload: dict | None = None, *, headers: dict | None = None):
        self._payload = payload or {}
        self.headers = headers or {}

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


@pytest.mark.asyncio
async def test_enterprise_login_me_and_logout_flow(tmp_path):
    server = _server(tmp_path)
    server.identity_service.bootstrap_org_admin(
        org_id="ORG-001",
        org_name="Acme",
        email="admin@example.com",
        display_name="Admin",
        password="secret-password",
        user_id="usr-admin",
    )

    login_response = await server.handle_auth_login(
        _FakeRequest({"email": "admin@example.com", "password": "secret-password"})
    )
    login_payload = json.loads(login_response.text)
    token = login_payload["session"]["token"]

    assert login_response.status == 200
    assert login_payload["ok"] is True
    assert login_payload["user"]["memberships"][0]["role"] == EnterpriseRole.ORG_ADMIN.value

    me_response = await server.handle_auth_me(_FakeRequest(headers={"Authorization": f"Bearer {token}"}))
    me_payload = json.loads(me_response.text)
    assert me_response.status == 200
    assert me_payload["user"]["email"] == "admin@example.com"

    logout_response = await server.handle_auth_logout(_FakeRequest(headers={"Authorization": f"Bearer {token}"}))
    assert json.loads(logout_response.text)["revoked"] is True

    me_after_logout = await server.handle_auth_me(_FakeRequest(headers={"Authorization": f"Bearer {token}"}))
    assert me_after_logout.status == 401


@pytest.mark.asyncio
async def test_enterprise_login_rejects_bad_credentials(tmp_path):
    server = _server(tmp_path)
    server.identity_service.bootstrap_org_admin(
        org_id="ORG-001",
        org_name="Acme",
        email="admin@example.com",
        display_name="Admin",
        password="secret-password",
    )

    response = await server.handle_auth_login(
        _FakeRequest({"email": "admin@example.com", "password": "wrong"})
    )

    assert response.status == 401
    assert json.loads(response.text)["ok"] is False


def test_enterprise_admin_auth_requires_admin_project_role(tmp_path):
    server = _server(tmp_path)
    admin = server.identity_service.bootstrap_org_admin(
        org_id="ORG-001",
        org_name="Acme",
        email="admin@example.com",
        display_name="Admin",
        password="secret-password",
        user_id="usr-admin",
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
    admin_session = server.identity_service.create_session(user_id=admin.id)
    user_session = server.identity_service.create_session(user_id=user.id)

    assert server._check_admin_auth(_FakeRequest(headers={"Authorization": f"Bearer {admin_session.token}"}))
    assert not server._check_admin_auth(_FakeRequest(headers={"Authorization": f"Bearer {user_session.token}"}))
    assert not server._check_admin_auth(_FakeRequest(headers={}))


def test_personal_profile_keeps_legacy_workbench_token(tmp_path):
    config_path = tmp_path / "agents.json"
    config_path.write_text(json.dumps({"global": {}, "agents": []}), encoding="utf-8")
    global_config = SimpleNamespace(
        deployment_profile="personal",
        bridge_home=tmp_path,
        workbench_port=18800,
        project_root=tmp_path,
    )
    server = WorkbenchApiServer(
        config_path=config_path,
        global_config=global_config,
        secrets={"workbench_admin_token": "legacy-secret"},
    )

    assert server._check_admin_auth(_FakeRequest(headers={"X-Workbench-Token": "legacy-secret"}))
    assert not server._check_admin_auth(_FakeRequest(headers={"X-Workbench-Token": "wrong"}))

