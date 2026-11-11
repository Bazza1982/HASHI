from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from orchestrator.phone_manager import PhoneManager
from orchestrator.translate_manager import TranslateManager
from orchestrator.frontend_live_voice.routes import register_live_voice_routes
from orchestrator.frontend_live_voice.manager import LiveVoiceManager
from orchestrator.frontend_live_voice.protocol import CallBinding, LiveVoiceError
from orchestrator.frontend_live_voice.simulmt_provider import SimulMTProvider
from orchestrator.session_store import SessionStore


def test_translate_selection_is_independent_and_contains_no_agent_context(tmp_path):
    phone = PhoneManager(tmp_path)
    phone.set_language("ja")
    original = phone.state_path.read_bytes()
    translation = TranslateManager(tmp_path)
    translation.set_direction("zh-en")
    result = translation.resolve_live_session(
        display_name="PRIVATE_PERSONA",
        pcm_payload={"secret": "PRIVATE_CONTEXT"},
        recent_history=[{"text": "PRIVATE_HISTORY"}],
    )
    assert result["public"]["entrypoint"] == "translate"
    assert result["public"]["direction"] == "zh-en"
    assert result["public"]["persona_projected"] is False
    assert result["input"] == []
    assert "PRIVATE_" not in json.dumps(result)
    assert result["model"] == "hy-simulmt-zh-en"
    assert TranslateManager(tmp_path).get_state()["language"] == "en"
    assert phone.state_path.read_bytes() == original
    assert phone.get_state()["language"] == "ja"
    translation.set_direction("en-zh")
    assert translation.get_state()["model"] == "hy-simulmt-en-zh"
    assert phone.state_path != translation.state_path
    frozen = result["selection"]
    restored = translation.resolve_live_session(frozen_selection=frozen)
    assert restored["model"] == "hy-simulmt-zh-en"
    assert restored["public"]["direction"] == "zh-en"
    assert "hy-simulmt" not in dict(phone.provider_options())
    with pytest.raises(ValueError, match="direction"):
        translation.set_direction("auto")


def test_translate_routes_derive_the_entrypoint_and_reject_client_override():
    async def scenario():
        observed = []

        class Service:
            available = True

            async def invoke(self, operation, authority, payload):
                observed.append((operation, authority, dict(payload)))
                return {"ok": True}

        async def authorize(request, operation, payload):
            return {"owner_id": "test-owner"}

        app = web.Application()
        service = Service()
        register_live_voice_routes(app, service, authorize, qualified=True)
        register_live_voice_routes(app, service, authorize, qualified=True,
                                  root="/api/v1/translate", entrypoint="translate")
        async with TestClient(TestServer(app)) as client:
            response = await client.post("/api/v1/translate/context", json={})
            assert response.status == 200
            assert observed[-1][2]["entrypoint"] == "translate"
            response = await client.post("/api/v1/live-voice/context", json={})
            assert response.status == 200
            assert observed[-1][2]["entrypoint"] == "phone"
            count = len(observed)
            response = await client.post("/api/v1/translate/context",
                                         json={"entrypoint": "phone"})
            assert response.status == 400
            assert (await response.json())["error_code"] == "live_entrypoint_conflict"
            assert len(observed) == count

    asyncio.run(scenario())


def translation_call(tmp_path):
    store = SessionStore(tmp_path / "state" / "sessions.sqlite3", instance_id="HASHI6")
    session = store.create_session(owner_id="test-owner", agent_id="test-agent", title="Translation")
    translator = TranslateManager(tmp_path / "agent")
    resolved = translator.resolve_live_session()
    adapter = SimulMTProvider(directory=object(), node_id="test-node", service_id="simulmt")
    manager = LiveVoiceManager(
        store, SimpleNamespace(instance_id="HASHI6", instance_generation="1", live_voice_v1=True),
        provider_registry={"hy-simulmt": adapter},
        resolve_phone_session=lambda _agent, **_scope: resolved,
    )
    binding = CallBinding(owner_id="test-owner", instance_id="HASHI6", instance_generation="1",
                          agent_id="test-agent", session_id=session["session_id"],
                          context_generation=1, call_id="test-translation", call_epoch=1,
                          provider_session_id="provider-translation")
    with store._lock, store._connection() as connection:
        connection.execute(
            """INSERT INTO live_calls(call_id, owner_id, session_id, agent_id, instance_id,
                   instance_generation, context_generation, call_epoch, provider_session_id,
                   phase, controller_lease, lease_expiry, started_at, max_ends_at, phone_config_json)
               VALUES (?, ?, ?, ?, ?, ?, 1, 1, ?, 'active', 'test-lease', ?, ?, ?, ?)""",
            (binding.call_id, binding.owner_id, binding.session_id, binding.agent_id,
             binding.instance_id, binding.instance_generation, binding.provider_session_id,
             "2099-01-01T00:00:00Z", "2026-01-01T00:00:00Z", "2099-01-01T00:00:00Z",
             json.dumps({"public": resolved["public"], "selection": resolved["selection"]})),
        )
    return store, manager, binding


def test_translation_keeps_chat_background_and_transcripts_separate(tmp_path):
    store, manager, binding = translation_call(tmp_path)
    store.append_presentation_message(
        session_id=binding.session_id, owner_id=binding.owner_id, agent_id=binding.agent_id,
        role="assistant", text="PRIVATE_BACKGROUND_RESULT", source="test",
        idempotency_key="background-message", history_eligible=True,
    )
    with store._lock, store._connection() as connection:
        store._append_event(connection, session_id=binding.session_id, run_id=None,
                            kind="run.failed", summary="PRIVATE_BACKGROUND_ERROR")
    assert store.pending_live_foreground_items(binding.call_id) == []
    manager._persist_call_record(binding)
    with store._lock, store._connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM messages WHERE source='live-phone'").fetchone()[0] == 0
    assert manager.has_foreground_call(binding.owner_id) is True


def test_translation_context_and_call_control_are_bound_to_the_entrypoint(tmp_path):
    _store, manager, binding = translation_call(tmp_path)
    assert manager.available_for("translate") is True
    assert manager.available_for("phone") is False
    scope = {"agent_id": binding.agent_id, "session_id": binding.session_id,
             "context_generation": binding.context_generation, "entrypoint": "translate"}
    result = asyncio.run(manager.invoke("context", {"owner_id": binding.owner_id}, scope))
    assert result["capability"]["available"] is True
    assert result["capability"]["phone"]["entrypoint"] == "translate"
    assert result["capability"]["phone"]["persona_projected"] is False
    with pytest.raises(LiveVoiceError, match="live_entrypoint_conflict"):
        asyncio.run(manager.invoke("snapshot", {"owner_id": binding.owner_id},
                                   {**binding.public_scope(), "entrypoint": "phone"}))
    result = asyncio.run(manager.invoke("snapshot", {"owner_id": binding.owner_id},
                                        {**binding.public_scope(), "entrypoint": "translate"}))
    assert result["ok"] is True


def test_translate_api_resolver_does_not_load_pcm(tmp_path, monkeypatch):
    from orchestrator.workbench_api import WorkbenchApiServer
    from orchestrator import bridge_memory

    def forbidden(*args, **kwargs):
        raise AssertionError("Translation must not assemble PCM")

    monkeypatch.setattr(bridge_memory, "BridgeContextAssembler", forbidden)
    workspace = tmp_path / "agent"
    runtime = SimpleNamespace(workspace_dir=workspace,
                              translate_manager=TranslateManager(workspace),
                              get_display_name=lambda: "Private Agent")
    api = WorkbenchApiServer.__new__(WorkbenchApiServer)
    api._runtime_map = lambda: {"test-agent": runtime}
    api._require_live_voice_session_scope = lambda **kwargs: None
    resolved = api._resolve_live_voice_phone_session(
        "test-agent", owner_id="test-owner", session_id="test-session",
        context_generation=1, entrypoint="translate",
    )
    assert resolved["input"] == []
    assert resolved["public"]["persona_projected"] is False


def test_translate_menu_and_command_save_direction_in_both_locales(tmp_path):
    from unittest.mock import AsyncMock
    from orchestrator import ui_language
    from orchestrator.translate_settings import command, render

    runtime = SimpleNamespace(translate_manager=TranslateManager(tmp_path),
                              _is_authorized_user=lambda user: user == 7,
                              _reply_text=AsyncMock())
    update = SimpleNamespace(effective_user=SimpleNamespace(id=7))
    with ui_language.language_scope(SimpleNamespace(), locale="zh-CN"):
        asyncio.run(command(runtime, update, SimpleNamespace(args=["direction", "zh-en"])))
        rendered = runtime._reply_text.await_args.args[1]
        assert "同声传译" in rendered and "中文 → 英语" in rendered
        text, keyboard = render(runtime)
        assert "translate." not in text
        assert "✓ 中文 → 英语" in [button.text for row in keyboard.inline_keyboard for button in row]
    with ui_language.language_scope(SimpleNamespace(), locale="en"):
        text, _keyboard = render(runtime)
        assert "Chinese → English" in text
        assert "translate." not in text
