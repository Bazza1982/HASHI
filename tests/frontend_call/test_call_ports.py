from types import SimpleNamespace
import pytest
from orchestrator.session_store import SessionStore
from orchestrator.frontend_call.ports import HashiPorts
from orchestrator.frontend_call.contract import CallError
from orchestrator.frontend_live_voice.manager import LiveVoiceManager
from orchestrator.frontend_live_voice.protocol import LiveVoiceError


async def test_real_session_store_is_the_only_message_and_result_owner(tmp_path):
    store = SessionStore(tmp_path / "sessions.db", instance_id="TEST")
    session = store.ensure_default_session(owner_id="owner", agent_id="lily")
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
    assert user["display_text"] == "Read this label." and "AC." in user["text"]
    assert "Untrusted camera observation" in captured[0][0]
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
