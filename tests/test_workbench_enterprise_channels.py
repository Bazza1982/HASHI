from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestrator.enterprise import EnterpriseRole
from orchestrator.workbench_api import WorkbenchApiServer


class _FakeRequest:
    def __init__(self, payload: dict | None = None, *, headers: dict | None = None, path: str = ""):
        self._payload = payload or {}
        self.headers = headers or {}
        self.path = path

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


def _audit_events(tmp_path: Path) -> list[dict]:
    path = tmp_path / "state" / "enterprise_audit.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


@pytest.mark.asyncio
async def test_enterprise_admin_can_register_and_list_channels(tmp_path):
    server = _server(tmp_path)
    headers = _admin_headers(server)

    create_response = await server.handle_enterprise_channels_register(
        _FakeRequest(
            {
                "type": "teams",
                "display_name": "Microsoft Teams",
                "enabled": False,
                "risk_tier": "high",
                "config": {"tenant": "acme", "bot_token": "secret-token"},
            },
            headers=headers,
        )
    )
    list_response = await server.handle_enterprise_channels(_FakeRequest(headers=headers))

    assert create_response.status == 201
    channel = json.loads(create_response.text)["channel"]
    assert channel["type"] == "teams"
    assert channel["enabled"] is False
    assert "config" not in channel
    channels = json.loads(list_response.text)["channels"]
    assert [item["type"] for item in channels] == ["teams"]
    event = _audit_events(tmp_path)[-1]
    assert event["action"] == "channel_register"
    assert event["context"]["channel_type"] == "teams"
    assert "secret-token" not in json.dumps(event)


@pytest.mark.asyncio
async def test_enterprise_admin_can_bind_channel_to_project(tmp_path):
    server = _server(tmp_path)
    headers = _admin_headers(server)
    await server.handle_enterprise_channels_register(
        _FakeRequest({"type": "hchat", "enabled": True}, headers=headers)
    )

    bind_response = await server.handle_enterprise_channels_bind(
        _FakeRequest(
            {
                "type": "hchat",
                "scope_type": "project",
                "scope_id": "prj-research",
                "permission": "both",
            },
            headers=headers,
        )
    )
    list_response = await server.handle_enterprise_channels(_FakeRequest(headers=headers))

    assert bind_response.status == 201
    binding = json.loads(bind_response.text)["binding"]
    assert binding["scope_type"] == "project"
    assert binding["scope_id"] == "prj-research"
    listed = json.loads(list_response.text)["channels"][0]
    assert listed["bindings"][0]["scope_id"] == "prj-research"
    assert _audit_events(tmp_path)[-1]["action"] == "channel_bind"


@pytest.mark.asyncio
async def test_enterprise_channel_admin_api_rejects_individual_user(tmp_path):
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

    response = await server.handle_enterprise_channels_register(
        _FakeRequest(
            {"type": "slack", "enabled": True},
            headers={"Authorization": f"Bearer {session.token}"},
            path="/api/enterprise/channels",
        )
    )

    assert response.status == 403
    assert json.loads(response.text)["error"] == "admin auth failed"
    event = _audit_events(tmp_path)[-1]
    assert event["action"] == "admin_auth"
    assert event["status"] == "denied"


@pytest.mark.asyncio
async def test_personal_profile_rejects_enterprise_channels_api(tmp_path):
    server = _server(tmp_path, profile="personal")

    response = await server.handle_enterprise_channels(_FakeRequest(path="/api/enterprise/channels"))

    assert response.status == 404
    assert json.loads(response.text)["error"] == "enterprise API requires governed profile"
