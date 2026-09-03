from __future__ import annotations

import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest
from telegram.error import TimedOut

sys.modules.setdefault("edge_tts", types.ModuleType("edge_tts"))

from orchestrator import runtime_session
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


def test_flexible_runtime_loads_last_assistant_text_from_transcript(tmp_path):
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text(
        "\n".join(
            [
                json.dumps({"role": "assistant", "text": "older reply"}),
                "not-json",
                json.dumps({"role": "user", "text": "latest prompt"}),
                json.dumps({"role": "assistant", "text": "latest reply"}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    runtime = FlexibleAgentRuntime.__new__(FlexibleAgentRuntime)
    runtime.transcript_log_path = transcript

    assert runtime._load_last_text_from_transcript("assistant") == "latest reply"
    assert runtime._load_last_text_from_transcript("user") == "latest prompt"


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
        _load_last_visible_assistant_text=lambda update: "last assistant reply",
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


def test_say_legacy_fallback_excludes_newer_noninteractive_sources(tmp_path):
    core = tmp_path / "core_transcript.jsonl"
    core.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "role": "assistant_core",
                        "visible_text": "telegram reply",
                        "source": "text",
                    }
                ),
                "not-json",
                json.dumps(
                    {
                        "role": "assistant_core",
                        "visible_text": "scheduler output",
                        "source": "scheduler",
                    }
                ),
                json.dumps(
                    {
                        "role": "assistant_core",
                        "visible_text": "api output",
                        "source": "api-smoke",
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    runtime = FlexibleAgentRuntime.__new__(FlexibleAgentRuntime)
    runtime.core_transcript_log_path = core
    runtime.transcript_log_path = tmp_path / "missing-transcript.jsonl"

    assert runtime._load_last_visible_assistant_text() == "telegram reply"


def test_say_prefers_current_route_confirmed_delivery(tmp_path, monkeypatch):
    core = tmp_path / "core_transcript.jsonl"
    core.write_text(
        json.dumps(
            {
                "role": "assistant_core",
                "visible_text": "newer but unconfirmed output",
                "source": "text",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    runtime = FlexibleAgentRuntime.__new__(FlexibleAgentRuntime)
    runtime.core_transcript_log_path = core
    runtime.transcript_log_path = tmp_path / "missing-transcript.jsonl"
    runtime.session_store = object()
    runtime.error_logger = SimpleNamespace(warning=lambda *args: None)
    update = SimpleNamespace()
    monkeypatch.setattr(
        runtime_session,
        "telegram_delivery_state_for_update",
        lambda runtime, current_update: ("confirmed reply", True),
    )

    assert runtime._load_last_visible_assistant_text(update) == "confirmed reply"


def test_say_does_not_fall_back_after_route_delivery_tracking_starts(
    tmp_path, monkeypatch
):
    core = tmp_path / "core_transcript.jsonl"
    core.write_text(
        json.dumps(
            {
                "role": "assistant_core",
                "visible_text": "backend output whose delivery failed",
                "source": "text",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    runtime = FlexibleAgentRuntime.__new__(FlexibleAgentRuntime)
    runtime.core_transcript_log_path = core
    runtime.transcript_log_path = tmp_path / "missing-transcript.jsonl"
    runtime.session_store = object()
    runtime.error_logger = SimpleNamespace(warning=lambda *args: None)
    monkeypatch.setattr(
        runtime_session,
        "telegram_delivery_state_for_update",
        lambda runtime, update: (None, True),
    )

    assert runtime._load_last_visible_assistant_text(SimpleNamespace()) is None


@pytest.mark.asyncio
async def test_send_voice_reply_reports_telegram_timeout_as_unknown(tmp_path):
    ogg_path = tmp_path / "reply.ogg"
    ogg_path.write_bytes(b"ogg")

    class VoiceManager:
        async def synthesize_reply(self, *args, **kwargs):
            return VoiceAsset(
                provider="test",
                text="hello",
                spoken_text="hello",
                wav_path=None,
                ogg_path=ogg_path,
            )

    class Bot:
        async def send_voice(self, **kwargs):
            raise TimedOut("ack timeout")

    warnings = []
    runtime = FlexibleAgentRuntime.__new__(FlexibleAgentRuntime)
    runtime.telegram_connected = True
    runtime.voice_manager = VoiceManager()
    runtime.name = "zelda"
    runtime.app = SimpleNamespace(bot=Bot())
    runtime.telegram_logger = SimpleNamespace(
        warning=warnings.append,
        info=lambda *args: None,
    )
    runtime.error_logger = SimpleNamespace(error=lambda *args: None)
    runtime._mark_error = lambda *args: pytest.fail(
        "ambiguous delivery is not a hard failure"
    )

    result = await runtime._send_voice_reply(123, "hello", "say-timeout", force=True)

    assert result is None
    assert warnings


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
