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
        match_info: dict | None = None,
    ):
        self._payload = payload or {}
        self.headers = headers or {}
        self.path = path
        self.query = query or {}
        self.match_info = match_info or {}

    async def json(self):
        return self._payload


def _server(tmp_path: Path, *, profile: str = "enterprise", agents: list[dict] | None = None) -> WorkbenchApiServer:
    config_path = tmp_path / "agents.json"
    config_path.write_text(
        json.dumps(
            {
                "global": {"deployment_profile": profile, "organization_id": "ORG-001"},
                "agents": agents or [],
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


def _audit_events(tmp_path: Path) -> list[dict]:
    path = tmp_path / "state" / "enterprise_audit.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


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
    assert _audit_events(tmp_path)[-1]["action"] == "login"

    me_response = await server.handle_auth_me(_FakeRequest(headers={"Authorization": f"Bearer {token}"}))
    me_payload = json.loads(me_response.text)
    assert me_response.status == 200
    assert me_payload["user"]["email"] == "admin@example.com"

    logout_response = await server.handle_auth_logout(_FakeRequest(headers={"Authorization": f"Bearer {token}"}))
    assert json.loads(logout_response.text)["revoked"] is True
    assert _audit_events(tmp_path)[-1]["action"] == "logout"

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
    event = _audit_events(tmp_path)[-1]
    assert event["action"] == "login"
    assert event["status"] == "failed"
    assert "wrong" not in json.dumps(event)


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


@pytest.mark.asyncio
async def test_enterprise_admin_can_create_and_list_projects(tmp_path):
    server = _server(tmp_path)
    admin = server.identity_service.bootstrap_org_admin(
        org_id="ORG-001",
        org_name="Acme",
        email="admin@example.com",
        display_name="Admin",
        password="secret-password",
        user_id="usr-admin",
    )
    session = server.identity_service.create_session(user_id=admin.id)
    headers = {"Authorization": f"Bearer {session.token}"}

    create_response = await server.handle_enterprise_projects_create(
        _FakeRequest(
            {
                "name": "Research",
                "workspace_root": "/srv/hashi/research",
                "project_id": "prj-research",
            },
            headers=headers,
        )
    )
    list_response = await server.handle_enterprise_projects(_FakeRequest(headers=headers))

    assert create_response.status == 201
    projects = json.loads(list_response.text)["projects"]
    assert [project["id"] for project in projects] == ["ORG-001-default", "prj-research"]
    event = _audit_events(tmp_path)[-1]
    assert event["action"] == "project_create"
    assert event["context"]["project_id"] == "prj-research"


@pytest.mark.asyncio
async def test_enterprise_admin_can_create_and_list_users(tmp_path):
    server = _server(tmp_path)
    admin = server.identity_service.bootstrap_org_admin(
        org_id="ORG-001",
        org_name="Acme",
        email="admin@example.com",
        display_name="Admin",
        password="secret-password",
        user_id="usr-admin",
    )
    project = server.identity_service.create_project(
        org_id="ORG-001",
        name="Research",
        project_id="prj-research",
    )
    session = server.identity_service.create_session(user_id=admin.id)
    headers = {"Authorization": f"Bearer {session.token}"}

    create_response = await server.handle_enterprise_users_create(
        _FakeRequest(
            {
                "email": "user@example.com",
                "display_name": "User",
                "password": "secret-password",
                "user_id": "usr-user",
                "project_id": project.id,
                "role": EnterpriseRole.INDIVIDUAL_USER.value,
            },
            headers=headers,
        )
    )
    list_response = await server.handle_enterprise_users(_FakeRequest(headers=headers))

    assert create_response.status == 201
    users = json.loads(list_response.text)["users"]
    assert [user["email"] for user in users] == ["admin@example.com", "user@example.com"]
    created = json.loads(create_response.text)["user"]
    assert created["memberships"][0]["role"] == EnterpriseRole.INDIVIDUAL_USER.value
    event = _audit_events(tmp_path)[-1]
    assert event["action"] == "user_create"
    assert event["context"]["target_user_id"] == "usr-user"
    assert "secret-password" not in json.dumps(event)


@pytest.mark.asyncio
async def test_enterprise_admin_can_create_list_and_revoke_api_tokens(tmp_path):
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
    session = server.identity_service.create_session(user_id=admin.id)
    headers = {"Authorization": f"Bearer {session.token}"}

    create_response = await server.handle_enterprise_api_tokens_create(
        _FakeRequest({"user_id": user.id, "scopes": ["audit:read", "tasks:write"]}, headers=headers)
    )
    create_payload = json.loads(create_response.text)
    api_token = create_payload["api_token"]

    assert create_response.status == 201
    assert api_token["token"].startswith("hs_api_")
    assert api_token["scopes"] == ["audit:read", "tasks:write"]
    assert server.identity_service.validate_api_token(api_token["token"]).id == api_token["id"]

    list_response = await server.handle_enterprise_api_tokens(_FakeRequest(headers=headers))
    listed = json.loads(list_response.text)["api_tokens"]
    assert [item["id"] for item in listed] == [api_token["id"]]
    assert "token" not in listed[0]
    assert "token_hash" not in json.dumps(listed)

    revoke_response = await server.handle_enterprise_api_token_revoke(
        _FakeRequest(headers=headers, match_info={"token_id": api_token["id"]})
    )
    assert json.loads(revoke_response.text)["revoked"] is True
    assert server.identity_service.validate_api_token(api_token["token"]) is None

    active_after_revoke = await server.handle_enterprise_api_tokens(_FakeRequest(headers=headers))
    assert json.loads(active_after_revoke.text)["count"] == 0
    revoked_list = await server.handle_enterprise_api_tokens(
        _FakeRequest(headers=headers, query={"include_revoked": "true"})
    )
    assert json.loads(revoked_list.text)["count"] == 1

    events = _audit_events(tmp_path)
    assert [event["action"] for event in events[-2:]] == ["api_token_create", "api_token_revoke"]
    assert api_token["token"] not in json.dumps(events)


@pytest.mark.asyncio
async def test_enterprise_api_token_create_rejects_cross_org_user(tmp_path):
    server = _server(tmp_path)
    admin = server.identity_service.bootstrap_org_admin(
        org_id="ORG-001",
        org_name="Acme",
        email="admin@example.com",
        display_name="Admin",
        password="secret-password",
        user_id="usr-admin",
    )
    server.identity_service.create_organization(org_id="ORG-002", name="Other")
    other_user = server.identity_service.create_user(
        org_id="ORG-002",
        email="other@example.com",
        display_name="Other",
        password="secret-password",
        user_id="usr-other",
    )
    session = server.identity_service.create_session(user_id=admin.id)

    response = await server.handle_enterprise_api_tokens_create(
        _FakeRequest(
            {"user_id": other_user.id, "scopes": ["audit:read"]},
            headers={"Authorization": f"Bearer {session.token}"},
        )
    )

    assert response.status == 400
    assert "not in this organization" in json.loads(response.text)["error"]
    event = _audit_events(tmp_path)[-1]
    assert event["action"] == "api_token_create"
    assert event["status"] == "failed"


@pytest.mark.asyncio
async def test_enterprise_admin_api_rejects_individual_user(tmp_path):
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

    response = await server.handle_enterprise_users(
        _FakeRequest(headers={"Authorization": f"Bearer {session.token}"}, path="/api/enterprise/users")
    )

    assert response.status == 403
    assert json.loads(response.text)["error"] == "admin auth failed"
    event = _audit_events(tmp_path)[-1]
    assert event["action"] == "admin_auth"
    assert event["status"] == "denied"
    assert event["context"]["path"] == "/api/enterprise/users"


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


@pytest.mark.asyncio
async def test_personal_profile_lists_all_agents_without_session(tmp_path):
    server = _server(
        tmp_path,
        profile="personal",
        agents=[
            {
                "name": "zelda",
                "display_name": "Zelda",
                "workspace_dir": "workspaces/zelda",
                "type": "flex",
            },
            {
                "name": "nana",
                "display_name": "Nana",
                "workspace_dir": "workspaces/nana",
                "type": "flex",
                "project_id": "prj-research",
            },
        ],
    )

    response = await server.handle_agents(_FakeRequest())

    assert response.status == 200
    payload = json.loads(response.text)
    assert [agent["name"] for agent in payload["agents"]] == ["zelda", "nana"]


@pytest.mark.asyncio
async def test_enterprise_agents_requires_session(tmp_path):
    server = _server(
        tmp_path,
        agents=[
            {
                "name": "nana",
                "display_name": "Nana",
                "workspace_dir": "workspaces/nana",
                "type": "flex",
                "project_id": "prj-research",
            },
        ],
    )

    response = await server.handle_agents(_FakeRequest(path="/api/agents"))

    assert response.status == 401
    assert json.loads(response.text)["error"] == "not authenticated"


@pytest.mark.asyncio
async def test_enterprise_admin_can_list_all_agents(tmp_path):
    server = _server(
        tmp_path,
        agents=[
            {
                "name": "unscoped",
                "display_name": "Unscoped",
                "workspace_dir": "workspaces/unscoped",
                "type": "flex",
            },
            {
                "name": "research",
                "display_name": "Research",
                "workspace_dir": "workspaces/research",
                "type": "flex",
                "project_id": "prj-research",
            },
        ],
    )
    admin = server.identity_service.bootstrap_org_admin(
        org_id="ORG-001",
        org_name="Acme",
        email="admin@example.com",
        display_name="Admin",
        password="secret-password",
        user_id="usr-admin",
    )
    session = server.identity_service.create_session(user_id=admin.id)

    response = await server.handle_agents(_FakeRequest(headers={"Authorization": f"Bearer {session.token}"}))

    assert response.status == 200
    assert [agent["name"] for agent in json.loads(response.text)["agents"]] == ["unscoped", "research"]


@pytest.mark.asyncio
async def test_enterprise_individual_user_sees_only_project_agents(tmp_path):
    server = _server(
        tmp_path,
        agents=[
            {
                "name": "unscoped",
                "display_name": "Unscoped",
                "workspace_dir": "workspaces/unscoped",
                "type": "flex",
            },
            {
                "name": "research",
                "display_name": "Research",
                "workspace_dir": "workspaces/research",
                "type": "flex",
                "project_id": "prj-research",
            },
            {
                "name": "finance",
                "display_name": "Finance",
                "workspace_dir": "workspaces/finance",
                "type": "flex",
                "project_ids": ["prj-finance"],
            },
            {
                "name": "shared",
                "display_name": "Shared",
                "workspace_dir": "workspaces/shared",
                "type": "flex",
                "project_ids": ["prj-research", "prj-ops"],
            },
        ],
    )
    server.identity_service.bootstrap_org_admin(
        org_id="ORG-001",
        org_name="Acme",
        email="admin@example.com",
        display_name="Admin",
        password="secret-password",
    )
    server.identity_service.create_project(org_id="ORG-001", name="Research", project_id="prj-research")
    user = server.identity_service.create_user(
        org_id="ORG-001",
        email="user@example.com",
        display_name="User",
        password="secret-password",
        user_id="usr-user",
    )
    server.identity_service.assign_project_role(
        user_id=user.id,
        project_id="prj-research",
        role=EnterpriseRole.INDIVIDUAL_USER,
    )
    session = server.identity_service.create_session(user_id=user.id)

    response = await server.handle_agents(_FakeRequest(headers={"Authorization": f"Bearer {session.token}"}))

    assert response.status == 200
    assert [agent["name"] for agent in json.loads(response.text)["agents"]] == ["research", "shared"]
