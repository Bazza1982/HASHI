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
    assert "session_id" not in metadata
    assert metadata["owner_id"] == session["owner_id"]
    assert "session_context_generation" not in metadata
    assert metadata["agent_activity_context"]["origin_context_generation"] == 3
    assert metadata["session_channel_key"] == "fixture-client"
    assert metadata["agent_activity_context"]["origin_session_id"] == "fixture-session"
    assert key == f"command-derived:{connector}:fixture-session:3:55:bg"

@pytest.mark.asyncio
@pytest.mark.parametrize("surface,connector", [("workbench", "backend_api"), ("tui", "tui")])
async def test_bg_handoff_reaches_real_pao_activity_admission(tmp_path, monkeypatch, surface, connector):
    from orchestrator.session_store import SessionStore

    store = SessionStore(tmp_path / "sessions.sqlite3", instance_id="BG_FIXTURE")
    origin = store.ensure_default_session(owner_id="user:7", agent_id="fixture")
    runtime = SimpleNamespace(name="fixture", workspace_dir=tmp_path, session_store=store,
        global_config=SimpleNamespace(authorized_id=7, instance_id="BG_FIXTURE", bridge_home=tmp_path),
        _reply_text=AsyncMock())
    monkeypatch.setattr(runtime_session, "current_session_for_update", lambda *_args: origin)
    admissions = []
    async def enqueue(chat_id, prompt, source, summary, **kwargs):
        metadata = dict(kwargs["request_metadata"])
        # Execute the real PAO selection and durable writer used by enqueue_request.
        activity, owner, resolved_surface, channel = runtime_session.resolve_request_session(
            runtime, source=source, chat_id=chat_id, metadata=metadata)
        metadata.update(session_id=activity["session_id"], owner_id=owner,
                        session_surface=resolved_surface, session_channel_key=channel)
        actual = runtime_session.accept_request(
            runtime, request_id="bg-real-admission", chat_id=chat_id, prompt=prompt,
            source=source, request_metadata=metadata, request_content=None,
            idempotency_key=kwargs["idempotency_key"])
        admissions.append((actual[0], actual[1], metadata))
        return actual[1].request_id
    runtime.enqueue_request = enqueue
    update = SimpleNamespace(update_id=99, effective_chat=SimpleNamespace(id=77),
        effective_user=SimpleNamespace(id=7), effective_message=SimpleNamespace(text="/bg exact task"),
        _hashi_session_surface=surface, _hashi_session_channel_key="fixture-client",
        _hashi_session_context_generation=origin["context_generation"])
    context = SimpleNamespace(args=["exact", "task"], frontend_connector_id=connector)
    await bg_command(runtime, update, context)
    activity, accepted, metadata = admissions[0]
    assert activity["session_kind"] == "agent_activity"
    assert accepted.session_id == activity["session_id"] != origin["session_id"]
    assert metadata["agent_activity_context"]["origin_session_id"] == origin["session_id"]
    assert metadata["ingress_transport"] == connector
    assert store.get_run(accepted.run_id, owner_id="user:7")["source"] == "background:prompt"

# HASHI-Fingerprint: hfp_64d99894f93d4ae7ba08d6a2f2981286
