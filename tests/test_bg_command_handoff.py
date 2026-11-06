"""Background command admission keeps the real frontend Session and Connector."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from orchestrator import runtime_session
from orchestrator.commands.bg import bg_command
from orchestrator.frontend_connector_registry import canonical_connector_id


@pytest.mark.asyncio
@pytest.mark.parametrize("surface,connector", [("workbench", "backend_api"), ("tui", "tui")])
async def test_bg_command_keeps_authenticated_frontend_session(tmp_path, monkeypatch, surface, connector):
    session = {"owner_id": "fixture-owner", "session_id": "fixture-session",
               "context_generation": 3, "next_message_ordinal": 6}
    monkeypatch.setattr(runtime_session, "current_session_for_update",
                        lambda runtime, update: session)
    calls = []

    async def enqueue(chat_id, prompt, source, summary, **kwargs):
        metadata = kwargs["request_metadata"]
        actual = canonical_connector_id(
            source, ingress_transport=metadata.get("ingress_transport"),
            surface=metadata.get("session_surface"))
        calls.append((chat_id, prompt, source, metadata, kwargs.get("idempotency_key"), actual))
        return "fixture-background-request"

    runtime = SimpleNamespace(workspace_dir=tmp_path, session_store=object(),
                              enqueue_request=enqueue, _reply_text=AsyncMock())
    update = SimpleNamespace(update_id=55, effective_chat=SimpleNamespace(id=700000007),
        effective_user=SimpleNamespace(id=7),
        effective_message=SimpleNamespace(text="/bg keep this exact task"),
        _hashi_session_surface=surface, _hashi_session_channel_key="fixture-client",
        _hashi_session_context_generation=3)
    context = SimpleNamespace(args=["keep", "this", "exact", "task"],
                              frontend_connector_id=connector)
    await bg_command(runtime, update, context)
    assert len(calls) == 1
    chat_id, prompt, source, metadata, key, actual = calls[0]
    assert chat_id == 0
    assert actual == connector
    assert prompt.endswith("keep this exact task")
    assert metadata["session_id"] == session["session_id"]
    assert metadata["owner_id"] == session["owner_id"]
    assert metadata["session_context_generation"] == 3
    assert metadata["session_channel_key"] == "fixture-client"
    assert metadata["agent_activity_context"]["origin_session_id"] == "fixture-session"
    assert key == f"command-derived:{connector}:fixture-session:3:55:bg"
# HASHI-Fingerprint: hfp_64d99894f93d4ae7ba08d6a2f2981286
