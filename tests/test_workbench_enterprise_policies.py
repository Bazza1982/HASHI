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
    ):
        self._payload = payload or {}
        self.headers = headers or {}
        self.path = path

    async def json(self):
        return self._payload


def _server(tmp_path: Path) -> WorkbenchApiServer:
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
async def test_enterprise_admin_can_create_and_list_policy_rules(tmp_path):
    server = _server(tmp_path)
    headers = _admin_headers(server)

    create_response = await server.handle_enterprise_policies_create(
        _FakeRequest(
            {
                "rule_id": "pol-deny-backend",
                "action": "backend.switch",
                "resource": "backend:grok-cli",
                "effect": "deny",
                "scope_type": "project",
                "scope_id": "prj-research",
                "conditions": {"agent_id": "nana"},
                "priority": 200,
            },
            headers=headers,
        )
    )
    list_response = await server.handle_enterprise_policies(_FakeRequest(headers=headers))

    created = json.loads(create_response.text)
    listed = json.loads(list_response.text)
    assert create_response.status == 201
    assert created["policy"]["id"] == "pol-deny-backend"
    assert created["policy"]["effect"] == "deny"
    assert created["policy"]["conditions"] == {"agent_id": "nana"}
    assert list_response.status == 200
    assert listed["count"] == 1
    assert listed["policies"][0]["id"] == "pol-deny-backend"


@pytest.mark.asyncio
async def test_enterprise_policy_create_rejects_invalid_effect(tmp_path):
    server = _server(tmp_path)
    headers = _admin_headers(server)

    response = await server.handle_enterprise_policies_create(
        _FakeRequest(
            {"action": "command.execute", "effect": "maybe"},
            headers=headers,
        )
    )

    assert response.status == 400
    payload = json.loads(response.text)
    assert payload["ok"] is False
    assert "unsupported policy decision" in payload["error"]


@pytest.mark.asyncio
async def test_enterprise_admin_can_install_default_connector_policies(tmp_path):
    server = _server(tmp_path)
    headers = _admin_headers(server)

    response = await server.handle_enterprise_policies_install_defaults(_FakeRequest(headers=headers))
    second_response = await server.handle_enterprise_policies_install_defaults(_FakeRequest(headers=headers))
    list_response = await server.handle_enterprise_policies(_FakeRequest(headers=headers))

    payload = json.loads(response.text)
    second_payload = json.loads(second_response.text)
    listed = json.loads(list_response.text)
    assert response.status == 200
    assert payload["ok"] is True
    assert payload["count"] == 6
    assert {policy["id"] for policy in payload["policies"]} == {
        "tpl-connector-github-repo-read-allow",
        "tpl-connector-github-repo-get-allow",
        "tpl-connector-github-issue-create-approval",
        "tpl-connector-github-pr-create-approval",
        "tpl-connector-github-pr-merge-approval",
        "tpl-connector-slack-message-send-approval",
    }
    assert second_payload["count"] == 6
    assert listed["count"] == 6


@pytest.mark.asyncio
async def test_enterprise_policy_api_requires_admin(tmp_path):
    server = _server(tmp_path)
    user = server.identity_service.bootstrap_org_admin(
        org_id="ORG-001",
        org_name="Acme",
        email="admin@example.com",
        display_name="Admin",
        password="secret-password",
    )
    regular = server.identity_service.create_user(
        org_id="ORG-001",
        email="user@example.com",
        display_name="User",
        password="secret-password",
        user_id="usr-user",
    )
    server.identity_service.assign_project_role(
        user_id=regular.id,
        project_id="ORG-001-default",
        role=EnterpriseRole.INDIVIDUAL_USER,
    )
    session = server.identity_service.create_session(user_id=regular.id)
    assert user.id

    response = await server.handle_enterprise_policies(
        _FakeRequest(headers={"Authorization": f"Bearer {session.token}"}, path="/api/enterprise/policies")
    )

    assert response.status == 403
    assert json.loads(response.text)["ok"] is False
