from __future__ import annotations

import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.modules.setdefault("edge_tts", types.ModuleType("edge_tts"))

from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime
from orchestrator.voice_manager import VoiceManager
from orchestrator.voice_synthesizer import VoiceAsset


def test_say_is_allowed_for_default_allowlist_commands():
    runtime = FlexibleAgentRuntime.__new__(FlexibleAgentRuntime)
    runtime.config = SimpleNamespace(type="limited", extra={"limited_policy": {"mode": "allowlist"}})
    runtime._command_policy_mode = "allow_all"
    runtime._disabled_commands = set()
    runtime._enabled_commands = set()

    runtime._init_command_policy()

    assert runtime._is_command_allowed("say") is True


@pytest.mark.asyncio
async def test_cmd_say_forces_voice_even_when_voice_replies_are_off():
    calls = []
    replies = []

    async def send_voice(chat_id, text, request_id, force=False):
        calls.append(
            {
                "chat_id": chat_id,
                "text": text,
                "request_id": request_id,
                "force": force,
            }
        )
        return True

    runtime = SimpleNamespace(
        _is_authorized_user=lambda user_id: True,
        _load_last_text_from_transcript=lambda role: "last assistant reply",
        _send_voice_reply=send_voice,
        _reply_text=lambda update, text: replies.append(text),
    )
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=123),
        effective_chat=SimpleNamespace(id=456),
    )

    await FlexibleAgentRuntime.cmd_say(runtime, update, SimpleNamespace())

    assert replies == []
    assert calls
    assert calls[0]["text"] == "last assistant reply"
    assert calls[0]["force"] is True


@pytest.mark.asyncio
async def test_voice_manager_force_bypasses_disabled_voice_state(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    media = tmp_path / "media"
    workspace.mkdir()
    media.mkdir()
    (workspace / "voice_state.json").write_text(
        json.dumps(
            {
                "enabled": False,
                "provider": "edge",
                "voice_name": "en-US-EmmaNeural",
                "rate": 0,
                "max_chars": 1200,
                "provider_options": {},
            }
        ),
        encoding="utf-8",
    )

    provider_calls = []

    class Provider:
        async def synthesize(self, **kwargs):
            provider_calls.append(kwargs)
            ogg_path = Path(kwargs["output_dir"]) / "say.ogg"
            ogg_path.parent.mkdir(parents=True, exist_ok=True)
            ogg_path.write_bytes(b"ogg")
            return VoiceAsset(
                provider="edge",
                text=kwargs["text"],
                spoken_text=kwargs["text"],
                wav_path=None,
                ogg_path=ogg_path,
            )

    monkeypatch.setattr("orchestrator.voice_manager.build_provider", lambda *args, **kwargs: Provider())

    manager = VoiceManager(workspace, media)

    assert await manager.synthesize_reply("zelda", "req-off", "hello", force=False) is None
    asset = await manager.synthesize_reply("zelda", "req-force", "hello", force=True)

    assert asset is not None
    assert provider_calls
    assert provider_calls[0]["voice_name"] == "en-US-EmmaNeural"
