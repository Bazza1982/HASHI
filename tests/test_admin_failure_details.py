"""Admin API errors keep execution facts and safe, traceable causes."""
from __future__ import annotations

import errno
import json
from types import SimpleNamespace

import pytest

from tests.test_workbench_agent_management import _server, _Request, _installed_codex


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["create", "preview", "delete", "status"])
async def test_admin_unexpected_failure_is_safe_and_preserves_unknown_effect_state(tmp_path, monkeypatch, operation):
    server = _server(tmp_path, orchestrator=SimpleNamespace(runtimes=[]))
    monkeypatch.setattr(server, "_check_admin_auth", lambda request: True)
    manager = server._agent_management_manager()
    calls = []
    async def unavailable(action, **kwargs):
        calls.append(action)
        raise PermissionError(errno.EACCES, "private-path token=private-secret")
    monkeypatch.setattr(manager, "dispatch", unavailable)
    if operation == "create":
        response = await server.handle_admin_add_agent(_Request(payload={
            "name": "fixture-created", "backend": "codex-cli", "is_active": False,
        }))
        expected_code, expected_cause = "creation_failed", "agent_create_dispatch_access_denied"
    elif operation == "preview":
        response = await server.handle_admin_agent_deletion_preview(_Request(match_info={"agent_id": "lily"}))
        expected_code, expected_cause = "preview_failed", "agent_preview_read_access_denied"
    elif operation == "delete":
        response = await server.handle_admin_agent_deletion(_Request(
            match_info={"agent_id": "lily"},
            payload={"preview_token": "fixture-preview", "confirmed_agent_id": "lily"},
        ))
        expected_code, expected_cause = "deletion_failed", "agent_delete_dispatch_access_denied"
    else:
        response = await server.handle_admin_agent_deletion_status(_Request(match_info={"operation_id": "del_fixture"}))
        expected_code, expected_cause = "deletion_status_failed", "agent_delete_status_read_access_denied"
    data = json.loads(response.text)
    assert response.status == 500 and data["ok"] is False
    assert data["error_code"] == expected_code
    assert data["cause_code"] == expected_cause and data["diagnostic_ref"].startswith("operr_")
    assert "private-" not in response.text
    assert len(calls) == 1
    if operation in {"preview", "status"}:
        assert data["effect_state"] == "not_requested" and data["state"] == "failed"
    else:
        assert data["effect_state"] == "unconfirmed" and data["state"] == "unknown"
        assert "before retrying" in data["error"].lower()


@pytest.mark.asyncio
async def test_admin_failure_before_dispatch_does_not_claim_the_action_was_submitted(tmp_path, monkeypatch):
    server = _server(tmp_path, orchestrator=SimpleNamespace(runtimes=[]))
    monkeypatch.setattr(server, "_check_admin_auth", lambda request: True)
    def unavailable(*args, **kwargs):
        raise PermissionError(errno.EACCES, "private-prepare")
    monkeypatch.setattr(server, "_agent_management_action", unavailable)
    response = await server.handle_admin_add_agent(_Request(payload={
        "name": "fixture-created", "backend": "codex-cli", "is_active": False,
    }))
    data = json.loads(response.text)
    assert response.status == 500 and data["effect_state"] == "not_started"
    assert data["cause_code"] == "agent_create_prepare_access_denied"
    assert data["diagnostic_ref"].startswith("operr_") and "private-" not in response.text


@pytest.mark.asyncio
async def test_created_agent_is_not_reported_uncreated_when_start_acknowledgment_fails(tmp_path, monkeypatch):
    _installed_codex(monkeypatch)
    starts = []
    async def timed_out(name):
        starts.append(name)
        raise TimeoutError("private-startup-service")
    kernel = SimpleNamespace(runtimes=[], start_agent=timed_out)
    server = _server(tmp_path, orchestrator=kernel)
    response = await server.handle_admin_add_agent(_Request(payload={
        "name": "fixture-created", "backend": "codex-cli", "is_active": True,
    }))
    data = json.loads(response.text)
    assert response.status == 500 and data["state"] == "unknown"
    assert data["cause_code"] == "agent_create_start_timed_out"
    assert data["observed"]["agent_created"] is True
    assert data["observed"]["start_requested"] is True
    assert "already created" in data["error"].lower()
    assert "private-" not in response.text and starts == ["fixture-created"]
    saved = json.loads(server.config_path.read_text(encoding="utf-8"))
    assert any(row["name"] == "fixture-created" for row in saved["agents"])


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["create", "preview", "delete", "status"])
async def test_admin_auth_rejection_still_has_no_diagnostics_or_execution(tmp_path, monkeypatch, operation):
    server = _server(tmp_path, orchestrator=SimpleNamespace(runtimes=[]))
    monkeypatch.setattr(server, "_check_admin_auth", lambda request: False)
    monkeypatch.setattr(server, "_agent_management_manager", lambda: pytest.fail("Auth rejection reached execution"))
    request = _Request(match_info={"agent_id": "lily"}, payload={})
    handler = {
        "create": server.handle_admin_add_agent,
        "preview": server.handle_admin_agent_deletion_preview,
        "delete": server.handle_admin_agent_deletion,
        "status": server.handle_admin_agent_deletion_status,
    }[operation]
    response = await handler(request)
    assert response.status == 403
    data = json.loads(response.text)
    assert data["error_code"] == "admin_auth_failed" and "diagnostic_ref" not in data
