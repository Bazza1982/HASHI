"""Desktop failures retain stage, cause and execution facts."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from orchestrator.desktop_api import register_desktop_api


def _api(tmp_path):
    return SimpleNamespace(
        app=web.Application(),
        config_path=tmp_path / "agents.json",
        global_config=SimpleNamespace(desktop_enabled=True),
        admin_token="fixture-token",
        live_voice_manager=SimpleNamespace(),
        _is_governed_profile=lambda: False,
        _check_admin_auth=lambda request: True,
        _v1_owner_id=lambda request: "owner-fixture",
    )


class _DesktopBroker:
    def __init__(self):
        self.target = {"capability_id": "fixture-capability"}
        self.calls = []
        self.error = None

    def desktop_targets(self):
        if self.error is not None:
            raise self.error
        return [self.target]

    async def invoke_desktop(self, *args, operation, **kwargs):
        self.calls.append(operation)
        if operation == "desktop_input":
            raise self.error
        return {"displays": []}


@pytest.mark.asyncio
async def test_desktop_read_failure_has_safe_correlated_reason(tmp_path):
    api = _api(tmp_path)
    broker = _DesktopBroker()
    broker.error = PermissionError("private-path token=private-secret")
    api.orchestrator = SimpleNamespace(capability_broker=broker)
    register_desktop_api(api)
    async with TestClient(TestServer(api.app)) as client:
        response = await client.post(
            "/api/v1/desktop/operation",
            json={"operation": "targets", "client_id": "fixture"},
        )
        data = await response.json()
    assert response.status == 503 and data["error_code"] == "desktop_unavailable"
    assert data["cause_code"] == "desktop_read_access_denied"
    assert (
        data["effect_state"] == "not_requested"
        and data["diagnostic_ref"] in data["message"]
    )
    assert "private-" not in str(data) and broker.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error", [PermissionError("private-input"), ValueError("private-input")]
)
async def test_desktop_input_exception_after_dispatch_is_unknown_and_never_repeated(
    tmp_path, error
):
    api = _api(tmp_path)
    broker = _DesktopBroker()
    api.orchestrator = SimpleNamespace(capability_broker=broker)
    register_desktop_api(api)
    async with TestClient(TestServer(api.app)) as client:
        opened = await (
            await client.post(
                "/api/v1/desktop/operation",
                json={
                    "operation": "open",
                    "client_id": "fixture",
                    "target": broker.target,
                },
            )
        ).json()
        broker.error = error
        response = await client.post(
            "/api/v1/desktop/operation",
            json={
                "operation": "input",
                "client_id": "fixture",
                "session_id": opened["session_id"],
                "lease_id": "fixture-lease",
                "event": {"seq": 1, "kind": "key_up", "key": "Enter"},
            },
        )
        data = await response.json()
    assert response.status == 503 and data["state"] == "unknown"
    assert data["accepted"] is None and data["retry_safe"] is False
    assert data["failure"]["phase"] == "dispatch" and "private-" not in str(data)
    assert broker.calls.count("desktop_input") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("authenticated", [True, False])
async def test_desktop_validation_and_auth_rejection_keep_the_original_contract(
    tmp_path, authenticated
):
    api = _api(tmp_path)
    api._check_admin_auth = lambda request: authenticated
    broker = _DesktopBroker()
    api.orchestrator = SimpleNamespace(capability_broker=broker)
    register_desktop_api(api)
    async with TestClient(TestServer(api.app)) as client:
        response = await client.post(
            "/api/v1/desktop/operation",
            data="invalid-json",
            headers={"Content-Type": "application/json"},
        )
        data = await response.json()
    assert response.status == (400 if authenticated else 403)
    assert data["error_code"] == (
        "desktop_invalid_request" if authenticated else "desktop_auth_required"
    )
    assert "diagnostic_ref" not in data and broker.calls == []
