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
        *,
        headers: dict | None = None,
        path: str = "",
        query: dict | None = None,
    ):
        self.headers = headers or {}
        self.path = path
        self.query = query or {}


def _server(tmp_path: Path, *, agents: list[dict]) -> WorkbenchApiServer:
    config_path = tmp_path / "agents.json"
    config_path.write_text(
        json.dumps(
            {
                "global": {"deployment_profile": "enterprise", "organization_id": "ORG-001"},
                "agents": agents,
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "agent_capabilities.json").write_text(
        json.dumps(
            {
                "agents": [
                    {
                        "name": "research",
                        "can_talk_to": ["shared"],
                        "can_receive_from": ["shared"],
                        "allowed_incoming_intents": ["ask"],
                        "granted_scopes": ["conversation"],
                        "tags": ["analysis"],
                    }
                ]
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
async def test_enterprise_admin_can_list_agent_capabilities(tmp_path):
    server = _server(
        tmp_path,
        agents=[
            {
                "name": "research",
                "display_name": "Research",
                "type": "flex",
                "project_id": "prj-research",
                "active_backend": "grok-cli",
                "allowed_backends": [{"engine": "grok-cli", "tools": {"allowed": ["file_read"]}}],
            },
            {"name": "finance", "project_id": "prj-finance"},
        ],
    )
    headers = _admin_headers(server)

    response = await server.handle_enterprise_agent_capabilities(_FakeRequest(headers=headers))

    payload = json.loads(response.text)
    assert response.status == 200
    assert payload["ok"] is True
    assert payload["count"] == 2
    assert [item["name"] for item in payload["agent_capabilities"]] == ["finance", "research"]
    research = payload["agent_capabilities"][1]
    assert research["active_backend"] == "grok-cli"
    assert research["allowed_tools"] == ["file_read"]
    assert research["bridge"]["granted_scopes"] == ["conversation"]


@pytest.mark.asyncio
async def test_enterprise_admin_can_filter_agent_capabilities_by_project(tmp_path):
    server = _server(
        tmp_path,
        agents=[
            {"name": "research", "project_id": "prj-research"},
            {"name": "shared", "project_ids": ["prj-research", "prj-ops"]},
            {"name": "finance", "project_id": "prj-finance"},
        ],
    )
    headers = _admin_headers(server)

    response = await server.handle_enterprise_agent_capabilities(
        _FakeRequest(headers=headers, query={"project_id": "prj-research"})
    )

    payload = json.loads(response.text)
    assert response.status == 200
    assert payload["project_id"] == "prj-research"
    assert [item["name"] for item in payload["agent_capabilities"]] == ["research", "shared"]


@pytest.mark.asyncio
async def test_enterprise_agent_capabilities_require_admin(tmp_path):
    server = _server(tmp_path, agents=[{"name": "research", "project_id": "prj-research"}])
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

    response = await server.handle_enterprise_agent_capabilities(
        _FakeRequest(headers={"Authorization": f"Bearer {session.token}"}, path="/api/enterprise/agent-capabilities")
    )

    assert response.status == 403
    assert json.loads(response.text)["ok"] is False
