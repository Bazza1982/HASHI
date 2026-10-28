from __future__ import annotations

import json

import pytest

from tests.test_session_api import _Request, _server


@pytest.mark.asyncio
async def test_session_command_needs_real_credentials_before_forget_dispatch(tmp_path, monkeypatch):
    from orchestrator import command_interaction_bridge, slash_command_audit

    server, _runtime = _server(tmp_path)
    server.admin_token = "test-secret"
    monkeypatch.setattr(server, "_check_admin_auth", lambda request: request.headers.get("Authorization") == "Bearer test-secret")
    monkeypatch.setattr(slash_command_audit, "is_supported_slash_command", lambda _runtime, command: command == "forget")
    seen = []

    async def dispatch(_runtime, payload, metadata):
        seen.append((payload, metadata))
        return {"ok": True, "http_status": 200}

    monkeypatch.setattr(command_interaction_bridge, "dispatch_command_interaction", dispatch)
    session = server.session_store.resolve_primary_session(owner_id="user:7", agent_id="lily", establish=True)
    payload = {
        "command": "forget", "arguments": ["2h"], "client_id": "client-123456789",
        "request_id": "request-123456789", "ui_locale": "en",
        "context_generation": session["context_generation"],
    }
    request = _Request(payload, match_info={"session_id": session["session_id"]})
    denied = await server.handle_v1_session_command_invocation(request)
    assert denied.status == 401
    assert seen == []
    request.headers["Authorization"] = "Bearer test-secret"
    allowed = await server.handle_v1_session_command_invocation(request)
    assert allowed.status == 200, allowed.text
    assert seen[0][1]["_hashi_forget_authenticated"] is True
    assert seen[0][1]["owner_id"] == "user:7"


@pytest.mark.asyncio
async def test_session_api_preview_and_confirmation_run_the_real_command(tmp_path, monkeypatch):
    import re
    from unittest.mock import AsyncMock
    from orchestrator import runtime_session
    from orchestrator.bridge_memory import BridgeMemoryStore
    from tests.test_forget_command import Runtime
    from tests.test_session_store import _complete

    server, _ = _server(tmp_path)
    server.admin_token = "test-secret"
    monkeypatch.setattr(server, "_check_admin_auth", lambda request: request.headers.get("Authorization") == "Bearer test-secret")
    home = tmp_path / "agent"
    home.mkdir()
    (home / "agent.md").write_text(
        "[persona]\nPersona\n[persona_end]\n[sys]\nRules\n[sys_end]\n"
        "[memory]\nwrong idea\n[memory_end]\n", encoding="utf-8",
    )
    runtime = Runtime(home, server.session_store)
    from orchestrator.canonical_audit import CanonicalAuditStore
    runtime.canonical_audit = CanonicalAuditStore(
        tmp_path / "audit-home", instance_id="HASHI1", agent_id="lily",
    )
    runtime.global_config = server.global_config
    server.runtimes = [runtime]
    session = server.session_store.resolve_primary_session(owner_id="user:7", agent_id="lily", establish=True)
    _complete(server.session_store, session_id=session["session_id"], owner_id="user:7", agent_id="lily",
              request_id="old", key="old", text="wrong idea", answer="wrong answer")
    BridgeMemoryStore(home).record_memory("episodic", "test", "wrong idea")
    from types import SimpleNamespace
    reset_backend = AsyncMock(return_value=True)
    runtime.backend_manager = SimpleNamespace(current_backend=SimpleNamespace(
        capabilities=SimpleNamespace(supports_sessions=True), handle_new_session=reset_backend,
    ))
    monkeypatch.setattr(runtime_session, "_discard_session_transients", lambda runtime: None)
    base = {"command": "forget", "client_id": "client-123456789",
            "ui_locale": "en", "context_generation": session["context_generation"]}
    headers = {"Authorization": "Bearer test-secret"}
    preview = await server.handle_v1_session_command_invocation(_Request(
        {**base, "arguments": ["2h"], "request_id": "request-preview-1234"},
        match_info={"session_id": session["session_id"]}, headers=headers,
    ))
    assert preview.status == 200, preview.text
    token = re.search(r"/forget confirm ([A-Za-z0-9_-]+)", preview.text).group(1)
    confirmed = await server.handle_v1_session_command_invocation(_Request(
        {**base, "arguments": ["confirm", token], "request_id": "request-confirm-1234"},
        match_info={"session_id": session["session_id"]}, headers=headers,
    ))
    assert confirmed.status == 200, confirmed.text
    body = json.loads(confirmed.text)
    new_id = body["result"]["new_session_id"]
    assert new_id != session["session_id"]
    reset_backend.assert_awaited_once()
    assert server.session_store.resolve_primary_session(owner_id="user:7", agent_id="lily")["session_id"] == new_id
    assert "wrong idea" not in str(server.session_store.agent_history_page(owner_id="user:7", agent_id="lily"))


@pytest.mark.asyncio
async def test_legacy_agent_command_and_chat_require_credentials_before_forget(tmp_path, monkeypatch):
    server, _runtime = _server(tmp_path)
    server.admin_token = "test-secret"
    monkeypatch.setattr(server, "_check_admin_auth", lambda request: request.headers.get("Authorization") == "Bearer test-secret")

    request = _Request({"command": "/forget 2h"}, match_info={"name": "lily"})
    denied = await server.handle_agent_command(request)
    assert denied.status == 401
    chat = _Request({"agent": "lily", "text": "/forget 2h"})
    chat.content_type = "application/json"
    denied_chat = await server.handle_chat(chat)
    assert denied_chat.status == 401

    seen = []

    async def dispatch(_runtime, text, **kwargs):
        seen.append((text, kwargs))
        return {"ok": True, "messages": []}

    monkeypatch.setattr("orchestrator.workbench_api.try_execute_slash_command_text", dispatch)
    chat.headers["Authorization"] = "Bearer test-secret"
    allowed = await server.handle_chat(chat)
    assert allowed.status == 200, allowed.text
    assert seen[0][1]["session_metadata"]["_hashi_forget_authenticated"] is True
