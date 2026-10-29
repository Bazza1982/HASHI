from __future__ import annotations

import json

import pytest

from orchestrator.config_json import read_config_json
from tests.test_workbench_telegram_credential import ADMIN_TOKEN, _Request, _server


@pytest.mark.asyncio
async def test_access_api_is_admin_only_and_disabled_until_runtime_is_ready(tmp_path):
    server = _server(tmp_path, verifier=None)
    unauthenticated = await server.handle_admin_agent_telegram_access(_Request())
    assert unauthenticated.status == 403
    authenticated = _Request(headers={"X-Workbench-Token": ADMIN_TOKEN})
    response = await server.handle_admin_agent_telegram_access(authenticated)
    assert response.status == 501
    assert json.loads(response.text)["error_code"] == "telegram_access_unavailable"
    before = (tmp_path / "agents.json").read_bytes()
    attempted = await server.handle_admin_agent_telegram_access_change(
        _Request(headers={"X-Workbench-Token": ADMIN_TOKEN}, payload={
            "expected_revision": "0" * 64, "scope": "global", "action": "add", "user_id": "200",
        })
    )
    assert attempted.status == 501
    assert (tmp_path / "agents.json").read_bytes() == before


@pytest.mark.asyncio
async def test_access_api_uses_scoped_revision_and_rejects_stale_or_invalid_writes(tmp_path, monkeypatch):
    server = _server(tmp_path, verifier=None)
    monkeypatch.setattr(server, "_telegram_access_management_supported", lambda: True)
    headers = {"X-Workbench-Token": ADMIN_TOKEN}
    status = await server.handle_admin_agent_telegram_access(_Request(headers=headers))
    data = json.loads(status.text)
    assert status.status == 200
    assert status.headers["Cache-Control"] == "no-store"
    assert data["management_supported"] is True
    assert data["agent_id"] == "lily"
    assert data["global_user_ids"] == ["42"]
    assert "provider_api_key" not in status.text

    payload = {"expected_revision": data["revision"], "scope": "agent",
               "action": "add", "user_id": "200"}
    changed = await server.handle_admin_agent_telegram_access_change(_Request(headers=headers, payload=payload))
    assert changed.status == 200
    assert json.loads(changed.text)["agent_user_ids"] == ["200"]

    before = (tmp_path / "agents.json").read_bytes()
    stale = await server.handle_admin_agent_telegram_access_change(_Request(headers=headers, payload=payload))
    assert stale.status == 409
    assert json.loads(stale.text)["error_code"] == "telegram_access_changed"
    invalid = await server.handle_admin_agent_telegram_access_change(
        _Request(headers=headers, payload={**payload, "extra": "reject"})
    )
    assert invalid.status == 400
    assert (tmp_path / "agents.json").read_bytes() == before


@pytest.mark.asyncio
async def test_missing_admin_token_does_not_grant_access_management(tmp_path, monkeypatch):
    server = _server(tmp_path, verifier=None)
    server.admin_token = ""
    monkeypatch.setattr(server, "_telegram_access_management_supported", lambda: True)
    response = await server.handle_admin_agent_telegram_access(_Request())
    assert response.status == 403

@pytest.mark.asyncio
async def test_api_can_configure_first_and_additional_users_without_overwriting_secrets(tmp_path, monkeypatch):
    server = _server(tmp_path, verifier=None, authorized_id=0, shared=True)
    monkeypatch.setattr(server, "_telegram_access_management_supported", lambda: True)
    headers = {"X-Workbench-Token": ADMIN_TOKEN}
    before_secrets = (tmp_path / "secrets.json").read_bytes()

    status = json.loads((await server.handle_admin_agent_telegram_access(_Request(headers=headers))).text)
    assert status["primary_user_id"] is None
    first = json.loads((await server.handle_admin_agent_telegram_access_change(
        _Request(headers=headers, payload={"expected_revision": status["revision"], "scope": "global",
                                           "action": "add", "user_id": "100"})
    )).text)
    assert first["primary_user_id"] == "100"
    second = json.loads((await server.handle_admin_agent_telegram_access_change(
        _Request(headers=headers, payload={"expected_revision": first["revision"], "scope": "global",
                                           "action": "add", "user_id": "200"})
    )).text)
    assert second["global_user_ids"] == ["100", "200"]

    momo = json.loads((await server.handle_admin_agent_telegram_access(_Request(agent_id="momo", headers=headers))).text)
    assert momo["global_user_ids"] == ["100", "200"]
    assert momo["agent_user_ids"] == []
    assert read_config_json(tmp_path / "agents.json")["global"]["owner_scope_id"] == "user:0"
    assert (tmp_path / "secrets.json").read_bytes() == before_secrets
