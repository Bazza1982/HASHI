"""Read-only projection failures preserve causes without leaking chat or replaying."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp.test_utils import TestClient, TestServer

from orchestrator import runtime_session, workbench_api
from orchestrator.chat_projection_transport import try_dispatch_chat_projection_transport
from orchestrator.function_worker_protocol import FunctionWorkerRemoteError
from tests.test_chat_projection_transport import _runtime, _wire, _session, _path, _database_snapshot
from tests.test_session_api import _server


@pytest.mark.asyncio
async def test_worker_projection_read_error_has_safe_correlated_detail_and_remains_read_only(tmp_path, caplog):
    runtime = _runtime(tmp_path)
    _path(runtime, _session(runtime)).mkdir()
    before = _database_snapshot(tmp_path)
    result = await try_dispatch_chat_projection_transport(runtime, _wire(), source_channel="workbench_api")
    assert result["error_code"] == "chat_projection_read_unavailable" and result["http_status"] == 503
    assert result["cause_code"].startswith("chat_projection_read_")
    assert result["failure"]["reason"] in {"access_denied", "io_error"}
    assert result["effect_state"] == "not_requested"
    assert result["diagnostic_ref"] in result["message"] and result["diagnostic_ref"] in caplog.text
    assert str(tmp_path) not in str(result) + caplog.text and "__hashi_chat_projection" not in caplog.text
    assert _database_snapshot(tmp_path) == before
    runtime.enqueue_request.assert_not_awaited()
    runtime._send_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_worker_session_lookup_failure_does_not_hide_the_cause_or_fall_through(tmp_path, monkeypatch):
    runtime = _runtime(tmp_path)
    def inaccessible(*args, **kwargs):
        raise PermissionError("private-session token=private-secret")
    monkeypatch.setattr(runtime_session, "current_session", inaccessible)
    result = await try_dispatch_chat_projection_transport(runtime, _wire(), source_channel="workbench_api")
    assert result["error_code"] == "chat_projection_session_unavailable"
    assert result["cause_code"] == "chat_projection_session_access_denied"
    assert result["effect_state"] == "not_requested" and "private-" not in str(result)
    runtime.enqueue_request.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("remote_type,reason", [("PermissionError", "access_denied"),
    ("TimeoutError", "timed_out"), ("ConnectionError", "connection_failed"), ([], "internal_error")])
async def test_worker_rpc_failure_retains_only_a_finite_cause(remote_type, reason):
    proxy = SimpleNamespace(is_function_worker_proxy=True,
        execute_slash_command=AsyncMock(side_effect=FunctionWorkerRemoteError("runtime.slash",
            {"type": remote_type, "message": "private-transcript token=private-secret"})))
    result = await try_dispatch_chat_projection_transport(proxy, _wire(), source_channel="workbench_api")
    assert result["http_status"] == 503 and result["error_code"] == "chat_projection_transport_unavailable"
    assert result["cause_code"] == "chat_projection_request_" + reason
    assert result["effect_state"] == "not_requested" and "private-" not in str(result)
    assert proxy.execute_slash_command.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("error,reason", [(PermissionError("private-transcript"), "access_denied"),
    (ValueError("private-format"), "response_invalid")])
async def test_shared_projection_error_does_not_expose_raw_exception_or_blame_query_input(tmp_path, monkeypatch, error, reason):
    server, _ = _server(tmp_path)
    session = server.session_store.resolve_primary_session(owner_id="user:7", agent_id="lily", establish=True)
    before = server.session_store.messages(session["session_id"])
    def unreadable(*args, **kwargs):
        raise error
    monkeypatch.setattr(workbench_api, "build_chat_projection", unreadable)
    async with TestClient(TestServer(server.app)) as client:
        response = await client.get("/api/chat-projection/lily")
        result = await response.json()
    assert response.status == 503 and result["error_code"] == "chat_projection_unavailable"
    assert result["failure"]["phase"] == "read" and result["failure"]["reason"] == reason
    assert result["effect_state"] == "not_requested"
    assert result["diagnostic_ref"] in result["message"] and "private-" not in str(result)
    assert server.session_store.messages(session["session_id"]) == before


@pytest.mark.asyncio
async def test_shared_projection_invalid_query_keeps_the_known_rejection(tmp_path):
    server, _ = _server(tmp_path)
    server.session_store.resolve_primary_session(owner_id="user:7", agent_id="lily", establish=True)
    async with TestClient(TestServer(server.app)) as client:
        response = await client.get("/api/chat-projection/lily?limit=0")
        result = await response.json()
    assert response.status == 400 and "diagnostic_ref" not in result
