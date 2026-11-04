from __future__ import annotations
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestrator.phone_manager import PhoneManager
from orchestrator.frontend_live_voice.provider import select_provider
from orchestrator.frontend_live_voice.simulmt_provider import SimulMTProvider
from orchestrator.frontend_live_voice.manager import LiveVoiceManager
from orchestrator.frontend_live_voice.protocol import LiveVoiceError


def test_interpreter_projection_uses_direction_without_agent_context(tmp_path):
    manager = PhoneManager(tmp_path)
    manager.set_provider("hy-simulmt")
    manager.set_model("hy-simulmt-zh-en")
    result = manager.resolve_live_session(display_name="Private persona",
        pcm_payload={"secret": "PRIVATE_CONTEXT"}, recent_history=[{"text": "PRIVATE_HISTORY"}])
    assert result["public"]["mode"] == "interpretation"
    assert result["public"]["language"] == "en"
    assert result["public"]["persona_projected"] is False
    assert result["input"] == []
    assert "PRIVATE_" not in json.dumps(result)
    assert "Chinese speech into English" in result["instructions"]
    assert manager.get_state()["language"] == "en"
    assert {key for key, _ in manager.language_options()} == {"en", "zh-CN"}
    manager.set_language("zh-CN")
    assert manager.get_state()["model"] == "hy-simulmt-en-zh"
    assert manager.resolve_live_session(display_name="Interpreter")["public"]["language"] == "zh-CN"
    assert manager.style_options() == (("natural", "Natural"),)



def test_translation_does_not_claim_conversational_capabilities():
    adapter = SimulMTProvider()
    assert select_provider({"hy-simulmt": adapter}, "hy-simulmt") is adapter
    assert adapter.capabilities.proactive_opening is False
    assert adapter.capabilities.task_results is False
    assert adapter.capabilities.context_updates is False
    assert adapter.media_descriptor() == {
        "transport": "webrtc", "protocol": "openai-live-v1", "data_channel": "oai-events", "version": 1}
    fake = SimpleNamespace(_closing=False, _provider_for=lambda _: adapter)
    LiveVoiceManager._schedule_opening(fake, object())


def test_translation_sideband_accepts_subtitles_and_rejects_delegation():
    adapter = SimulMTProvider()
    event = adapter.normalize_event(json.dumps({"type": "session.output_transcript.delta",
        "event_id": "evt-1", "delta": "你好", "start_ms": 0, "end_ms": 1600}))
    assert event["type"] == "conversation.assistant.delta"
    assert event["delta"] == "你好"
    with pytest.raises(LiveVoiceError, match="live_interpretation_delegation_forbidden"):
        adapter.normalize_event(json.dumps({"type": "session.delegation.created",
            "delegation": {"id": "task-1", "target": "client"}}))
    with pytest.raises(LiveVoiceError):
        adapter.opening("say hello", "event-1")
