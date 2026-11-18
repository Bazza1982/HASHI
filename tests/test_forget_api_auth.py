from __future__ import annotations

import json

import pytest

from tests.test_session_api import _Request, _server


@pytest.mark.asyncio
@pytest.mark.parametrize("multipart", [False, True])
async def test_unauthenticated_unknown_slash_text_remains_chat_not_a_command(tmp_path, monkeypatch, multipart):
    from unittest.mock import AsyncMock
    from orchestrator import slash_command_audit
    from tests.test_session_api import _MultipartRequest

    server, runtime = _server(tmp_path)
    server.admin_token = "test-secret"
    monkeypatch.setattr(slash_command_audit, "is_supported_slash_command",
                        lambda _runtime, command: command == "status")
    dispatcher = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr("orchestrator.workbench_api.try_execute_slash_command_text", dispatcher)
    runtime.enqueue_api_text = AsyncMock(return_value="req-unknown-slash")

    def request(text):
        fields = {"agent": "lily", "text": text}
        if multipart:
            return _MultipartRequest(fields)
        result = _Request(fields)
        result.content_type = "application/json"
        return result

    response = await server.handle_chat(request("/some/project/path is relevant"))
    assert response.status == 200, response.text
    assert runtime.enqueue_api_text.await_args.args[0] == "/some/project/path is relevant"
    dispatcher.assert_not_awaited()

    for text in ("/status", "/telegram off"):
        denied = await server.handle_chat(request(text))
        assert denied.status == 401
    assert runtime.enqueue_api_text.await_count == 1
    dispatcher.assert_not_awaited()


@pytest.mark.asyncio
async def test_normal_frontend_commands_require_the_existing_transport_credential(tmp_path, monkeypatch):
    from orchestrator import command_interaction_bridge, slash_command_audit

    server, _runtime = _server(tmp_path)
    server.admin_token = "test-secret"
    session = server.session_store.resolve_primary_session(
        owner_id="user:7", agent_id="lily", establish=True
    )
    monkeypatch.setattr(slash_command_audit, "is_supported_slash_command", lambda *_: True)
    seen = []

    async def dispatch(_runtime, _payload, metadata):
        seen.append(metadata)
        return {"ok": True, "http_status": 200}

    monkeypatch.setattr(command_interaction_bridge, "dispatch_command_interaction", dispatch)
    request = _Request(
        {"command": "model", "arguments": [], "client_id": "client-123456789",
         "request_id": "request-123456789", "context_generation": session["context_generation"]},
        match_info={"session_id": session["session_id"]},
    )
    rejected = await server.handle_v1_session_command_invocation(request)
    assert rejected.status == 401
    assert seen == []
    request.headers["X-Workbench-Token"] = "test-secret"
    allowed = await server.handle_v1_session_command_invocation(request)
    assert allowed.status == 200, allowed.text
    assert seen[0]["_hashi_owner_id"] == "user:7"

    agent_request = _Request({"command": "/model"}, match_info={"name": "lily"})
    rejected_agent = await server.handle_agent_command(agent_request)
    assert rejected_agent.status == 401
    chat_request = _Request({"agent": "lily", "text": "/model"})
    chat_request.content_type = "application/json"
    rejected_chat = await server.handle_chat(chat_request)
    assert rejected_chat.status == 401


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


@pytest.mark.asyncio
async def test_encoded_forget_command_requires_strict_auth_before_worker_dispatch(tmp_path, monkeypatch):
    from tests.test_command_interaction_transport import _wire

    server, _ = _server(tmp_path)
    server.admin_token = ""
    seen = []

    async def dispatch(_runtime, text, **kwargs):
        seen.append(kwargs)
        return {"ok": True}

    monkeypatch.setattr("orchestrator.workbench_api.execute_local_command", dispatch)
    request = _Request({"agent": "lily", "command": _wire("open", command="/forget 2h")})
    denied = await server.handle_admin_command(request)
    assert denied.status == 401
    assert seen == []

    server.admin_token = "test-secret"
    request.headers["Authorization"] = "Bearer test-secret"
    allowed = await server.handle_admin_command(request)
    assert allowed.status == 200
    assert seen[0]["session_metadata"]["_hashi_forget_authenticated"] is True
    assert seen[0]["session_metadata"]["_hashi_owner_id"] == "user:7"


@pytest.mark.asyncio
async def test_encoded_forget_auth_is_bound_to_the_worker_owner(monkeypatch):
    from orchestrator.admin_local_testing import try_execute_slash_command_text
    from tests.test_command_interaction_transport import _runtime, _wire

    observed = []
    monkeypatch.setattr(
        "orchestrator.runtime_session.current_session",
        lambda _runtime, **_kwargs: {
            "session_id": "safe-session", "context_generation": 1, "owner_id": "user:7",
        },
    )
    monkeypatch.setattr("orchestrator.runtime_session.owner_id", lambda _runtime: "user:7")

    async def dispatch(_runtime, _payload, metadata):
        observed.append(metadata)
        return {"ok": True, "command_ui_version": 2}

    monkeypatch.setattr(
        "orchestrator.command_interaction_bridge.dispatch_command_interaction", dispatch
    )
    text = _wire("open", command="/forget 2h")
    results = []
    for credentials in (
        {},
        {"_hashi_forget_authenticated": True, "_hashi_owner_id": "user:8"},
        {"_hashi_forget_authenticated": True, "_hashi_owner_id": "user:7"},
    ):
        results.append(await try_execute_slash_command_text(
            _runtime(), text, source_channel="workbench_api", session_metadata=credentials,
        ))
    assert observed[0].get("_hashi_forget_authenticated") is not True
    assert results[1]["error_code"] == "command_menu_forbidden"
    assert observed[1]["_hashi_forget_authenticated"] is True


@pytest.mark.asyncio
async def test_authenticated_command_ui_can_forget_and_start_a_new_session(tmp_path, monkeypatch):
    import re
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from orchestrator import runtime_session
    from orchestrator.admin_local_testing import try_execute_slash_command_text
    from orchestrator.canonical_audit import CanonicalAuditStore
    from tests.test_command_interaction_transport import _wire
    from tests.test_forget_command import Runtime
    from tests.test_session_store import _complete

    server, _ = _server(tmp_path)
    server.admin_token = "test-secret"
    home = tmp_path / "agent"
    home.mkdir()
    (home / "agent.md").write_text(
        "[persona]\nPersona\n[persona_end]\n[sys]\nRules\n[sys_end]\n",
        encoding="utf-8",
    )
    worker = Runtime(home, server.session_store)
    worker.global_config = server.global_config
    worker.canonical_audit = CanonicalAuditStore(
        tmp_path / "audit-home", instance_id="HASHI1", agent_id="lily",
    )
    backend = AsyncMock(return_value=True)
    worker.backend_manager = SimpleNamespace(current_backend=SimpleNamespace(
        capabilities=SimpleNamespace(supports_sessions=True), handle_new_session=backend,
    ))
    monkeypatch.setattr(runtime_session, "_discard_session_transients", lambda runtime: None)

    class Proxy:
        name = "lily"
        is_function_worker_proxy = True

        async def execute_slash_command(self, text, **kwargs):
            return await try_execute_slash_command_text(worker, text, **kwargs)

    server.runtimes = [Proxy()]
    session = server.session_store.resolve_primary_session(
        owner_id="user:7", agent_id="lily", establish=True,
    )
    _complete(server.session_store, session_id=session["session_id"], owner_id="user:7",
              agent_id="lily", request_id="old", key="old",
              text="wrong idea", answer="wrong answer")
    headers = {"Authorization": "Bearer test-secret"}

    preview = await server.handle_admin_command(_Request(
        {"agent": "lily", "command": _wire("open", command="/forget 2h",
                                            request_id="request-preview-1234")},
        headers=headers,
    ))
    assert preview.status == 200, preview.text
    token = re.search(r"/forget confirm ([A-Za-z0-9_-]+)", preview.text).group(1)
    confirmation = await server.handle_admin_command(_Request(
        {"agent": "lily", "command": _wire("open", command=f"/forget confirm {token}",
                                            request_id="request-confirm-1234")},
        headers=headers,
    ))
    assert confirmation.status == 200, confirmation.text
    result = json.loads(confirmation.text)
    assert result["result"]["new_session_id"] != session["session_id"]
    backend.assert_awaited_once()
    assert "wrong idea" not in str(
        server.session_store.agent_history_page(owner_id="user:7", agent_id="lily")
    )
