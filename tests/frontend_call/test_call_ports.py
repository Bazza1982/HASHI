from types import SimpleNamespace
import pytest
from orchestrator.session_store import SessionStore
from orchestrator.frontend_call.ports import HashiPorts
from orchestrator.frontend_call.contract import CallError
from orchestrator.frontend_live_voice.manager import LiveVoiceManager
from orchestrator.frontend_live_voice.protocol import LiveVoiceError


@pytest.mark.anyio
@pytest.mark.parametrize('fresh_conversation', [False, True])
async def test_real_session_store_is_the_only_message_and_result_owner(tmp_path, monkeypatch, fresh_conversation):
    monkeypatch.setattr('orchestrator.message_context._network_secret', lambda root: 'test-secret')
    store = SessionStore(tmp_path / "sessions.db", instance_id="TEST")
    session = store.ensure_default_session(owner_id="owner", agent_id="lily")
    if fresh_conversation:
        session = store.create_session(owner_id="owner", agent_id="lily", title="Simple conversation")
    captured = []

    class Runtime:
        backend_manager = SimpleNamespace(privacy_level=1)

        def _primary_chat_id(self):
            return 7

        async def enqueue_request(self, chat, prompt, source, summary, **kwargs):
            metadata = kwargs["request_metadata"]
            captured.append((prompt, kwargs))
            accepted = store.accept_run(
                session_id=metadata["session_id"],
                owner_id=metadata["owner_id"],
                agent_id="lily",
                request_id="request-1",
                text=metadata["session_message_text"],
                display_text=metadata["session_message_display_text"],
                content=metadata["session_message_content"],
                source=source,
                idempotency_key=kwargs["idempotency_key"],
                expected_context_generation=metadata["session_context_generation"],
            )
            store.mark_request_running(accepted.request_id, worker_id="fixture")
            store.finish_request(
                accepted.request_id,
                success=True,
                assistant_text="The visible label is AC.",
            )
            return accepted.request_id

    api = SimpleNamespace(
        config_path=tmp_path / 'agents.json',
        session_store=store,
        _runtime_map=lambda: {"lily": Runtime()},
        _persistent_session_v1_ready=lambda: True,
        live_voice_manager=SimpleNamespace(has_foreground_call=lambda owner: False),
    )
    ports = HashiPorts(api)
    binding = {
        "agent_id": "lily",
        "session_id": session["session_id"],
        "context_generation": session["context_generation"],
        "call_id": "call-1",
        "client_id": "client-1",
    }
    result = await ports.admit(
        "owner",
        binding,
        "turn-1",
        "Read this label.",
        "The label says AC.",
        "2026-10-03T00:00:00Z",
    )
    assert (
        ports.result("owner", binding, result["run_id"])["text"]
        == "The visible label is AC."
    )
    user = store.get_message(
        result["message_id"], session_id=session["session_id"], owner_id="owner"
    )
    assert user["display_text"] == user["text"] == "Read this label."
    from orchestrator.message_context import verify_connector_evidence, CONNECTOR_EVIDENCE_METADATA_KEY
    facts = verify_connector_evidence(tmp_path, evidence=captured[0][1]['request_metadata'][CONNECTOR_EVIDENCE_METADATA_KEY], prompt=captured[0][0])
    assert facts['call_media']['observation'] == 'The label says AC.'
    with pytest.raises(CallError):
        ports.validate("owner", {**binding, "context_generation": 999})
    with pytest.raises(Exception):
        ports.result("other", binding, result["run_id"])


async def test_phone_start_guard_is_opt_in_and_checks_before_provider_use(tmp_path):
    store = SessionStore(tmp_path / "sessions.db", instance_id="TEST")
    manager = LiveVoiceManager(
        store, SimpleNamespace(instance_id="TEST", bridge_home=tmp_path)
    )
    assert not manager.has_foreground_call("owner")
    manager.external_call_busy = lambda owner: owner == "owner"
    with pytest.raises(LiveVoiceError, match="live_other_call_active"):
        await manager._op_start("owner", {})


@pytest.mark.anyio
async def test_call_opening_uses_typed_tool_free_speech_without_creating_a_user_run(tmp_path, monkeypatch):
    store = SessionStore(tmp_path / "sessions.db", instance_id="TEST")
    session = store.create_session(owner_id="owner", agent_id="lily", title="Call")
    captured = []

    class Runtime:
        backend_manager = SimpleNamespace(privacy_level=1)

        async def phone_action_operation(self, operation, payload):
            captured.append((operation, payload))
            return {"text": "您好，我接到您的电话了。请讲。"}

    api = SimpleNamespace(
        session_store=store,
        _runtime_map=lambda: {"lily": Runtime()},
        _persistent_session_v1_ready=lambda: True,
        live_voice_manager=SimpleNamespace(has_foreground_call=lambda owner: False),
    )
    monkeypatch.setattr(
        "orchestrator.frontend_call.ports.resolve_call_spoken_context",
        lambda *_args: {"instructions": "effective PCM", "recent": [{"role": "user", "content": "earlier"}]},
    )
    binding = {
        "agent_id": "lily",
        "session_id": session["session_id"],
        "context_generation": session["context_generation"],
        "call_id": "call-1",
        "client_id": "client-1",
    }
    text = await HashiPorts(api).opening(
        "owner", binding, {"tts": {}}, {"tts": {"model": "fixture-tts"}}
    )
    assert text == "您好，我接到您的电话了。请讲。"
    assert len(store.messages(session["session_id"], owner_id="owner")) == 0
    operation, payload = captured[0]
    assert operation == "call_speak"
    assert payload["scope"]["type"] == "hashi.call-speech-scope"
    assert payload["state"]["instructions"] == "effective PCM"
    assert "earlier question" in payload["state"]["goal"]
    assert "short reply" not in payload["state"]["goal"]


@pytest.mark.anyio
async def test_worker_accepts_scoped_call_opening_without_a_phone_database_row(tmp_path, monkeypatch):
    from orchestrator.frontend_live_voice import worker_actions

    store = SessionStore(tmp_path / "sessions.db", instance_id="TEST")
    session = store.create_session(owner_id="owner", agent_id="lily", title="Call")
    runtime = SimpleNamespace(name="lily", session_store=store)
    rendered = []

    async def render(_runtime, state, **kwargs):
        rendered.append((state, kwargs))
        return {"text": "您好。"}

    monkeypatch.setattr(worker_actions, "invoke_phone_judgment", render)
    payload = {
        "owner_id": "owner",
        "scope": {
            "type": "hashi.call-speech-scope",
            "version": 1,
            "call_id": "call-1",
            "client_id": "client-1",
            "agent_id": "lily",
            "session_id": session["session_id"],
            "context_generation": session["context_generation"],
        },
        "state": {"kind": "opening", "goal": "Answer the call", "instructions": "PCM"},
    }
    assert await worker_actions.handle_phone_action_operation(runtime, "call_speak", payload) == {"text": "您好。"}
    assert rendered[0][1]["speech"] is True

    payload["scope"]["context_generation"] += 1
    with pytest.raises(LiveVoiceError, match="live_scope_changed"):
        await worker_actions.handle_phone_action_operation(runtime, "call_speak", payload)
