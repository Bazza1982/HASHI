"""Call API failures preserve safe causes and execution facts."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from aiohttp.test_utils import TestClient, TestServer

from orchestrator.frontend_call.routes import register_call_api
from tests.test_desktop_failure_details import _api


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["route", "start", "end"])
async def test_call_unexpected_error_keeps_its_cause_without_replaying(
    tmp_path, monkeypatch, operation
):
    api = _api(tmp_path)
    service = register_call_api(api)
    invoke = AsyncMock(side_effect=PermissionError("private-call token=private-secret"))
    monkeypatch.setattr(service, "invoke", invoke)
    async with TestClient(TestServer(api.app)) as client:
        response = await client.post(
            "/api/v1/call/operation", json={"operation": operation}
        )
        data = await response.json()
    assert response.status == 500 and data["error_code"] == "call_internal_error"
    assert data["cause_code"] == "call_dispatch_access_denied"
    assert data["effect_state"] == (
        "not_requested" if operation == "route" else "unconfirmed"
    )
    assert data["diagnostic_ref"] in data["message"] and "private-" not in str(data)
    assert invoke.await_count == 1


@pytest.mark.asyncio
async def test_call_configuration_read_failure_does_not_claim_dispatch(
    tmp_path, monkeypatch
):
    api = _api(tmp_path)
    service = register_call_api(api)

    def inaccessible():
        raise PermissionError("private-call-config")

    monkeypatch.setattr(service.config, "read", inaccessible)
    invoke = AsyncMock()
    monkeypatch.setattr(service, "invoke", invoke)
    async with TestClient(TestServer(api.app)) as client:
        response = await client.post(
            "/api/v1/call/operation", json={"operation": "start"}
        )
        data = await response.json()
    assert response.status == 500 and data["effect_state"] == "not_started"
    assert data["cause_code"] == "call_read_access_denied"
    assert invoke.await_count == 0


@pytest.mark.asyncio
async def test_call_response_encoding_failure_retains_unknown_mutation_outcome(
    tmp_path, monkeypatch
):
    api = _api(tmp_path)
    service = register_call_api(api)
    invoke = AsyncMock(return_value={"private": object()})
    monkeypatch.setattr(service, "invoke", invoke)
    async with TestClient(TestServer(api.app)) as client:
        response = await client.post(
            "/api/v1/call/operation", json={"operation": "end"}
        )
        data = await response.json()
    assert response.status == 500 and data["state"] == "unknown"
    assert data["failure"]["phase"] == "presentation"
    assert data["diagnostic_ref"] in data["message"] and invoke.await_count == 1
