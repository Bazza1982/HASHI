from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime
from orchestrator.voice_manager import VoiceManager
from orchestrator.voice_synthesizer import VoiceAsset


def _manager(tmp_path: Path) -> VoiceManager:
    return VoiceManager(
        tmp_path / "workspace",
        tmp_path / "media",
        native_capabilities=[
            {
                "engine": "openrouter-api",
                "model": "openai/gpt-audio-mini",
                "supported_voices": [
                    "alloy",
                    "ash",
                    "ballad",
                    "coral",
                    "echo",
                    "sage",
                    "shimmer",
                    "verse",
                ],
            }
        ],
    )


def test_voice_profiles_resolve_supported_native_voice_and_tts_fallback(tmp_path):
    manager = _manager(tmp_path)
    manager.set_native_target("openrouter-api", "openai/gpt-audio-mini")

    expected = {
        "warm_female": ("shimmer", "en-US-EmmaMultilingualNeural"),
        "clear_female": ("coral", "en-US-AriaNeural"),
        "warm_male": ("verse", "en-US-GuyNeural"),
        "calm_male": ("echo", "en-US-ChristopherNeural"),
    }
    for profile_id, (native_voice, tts_voice) in expected.items():
        manager.set_voice_profile(profile_id)
        state = manager.get_state()
        assert state["voice_profile"] == profile_id
        assert state["native"]["voice"] == native_voice
        assert state["provider"] == "edge"
        assert state["voice_name"] == tts_voice

    assert manager._tts_voice_for_profile("warm_male", "任务完成。") == (
        "zh-CN-YunxiNeural"
    )
    assert manager._tts_voice_for_profile("calm_male", "終わりました。") == (
        "ja-JP-NaokiNeural"
    )


def test_voice_modes_coordinate_native_and_legacy_tts_state(tmp_path):
    manager = _manager(tmp_path)

    manager.set_reply_mode("tts")
    assert manager.get_reply_mode() == "tts"
    assert manager.is_enabled() is True
    assert manager.native_policy["mode"] == "tts"

    manager.set_reply_mode("auto")
    assert manager.get_reply_mode() == "auto"
    assert manager.is_enabled() is False
    assert manager.native_audio_enabled() is True

    manager.set_reply_mode("off")
    assert manager.get_reply_mode() == "off"
    assert manager.is_enabled() is False
    assert manager.native_audio_enabled() is False


def test_voice_menu_is_compact_two_by_two_mode_and_profile_picker(tmp_path):
    manager = _manager(tmp_path)
    manager.set_native_target("openrouter-api", "openai/gpt-audio-mini")
    manager.set_reply_mode("auto")
    manager.set_voice_profile("clear_female")
    runtime = FlexibleAgentRuntime.__new__(FlexibleAgentRuntime)
    runtime.voice_manager = manager

    menu = manager.voice_menu_text()
    keyboard = runtime._voice_keyboard().inline_keyboard

    assert "<b>Current</b> · <b>AUTO</b>" in menu
    assert "<b>Voice</b> · 👩 Clear" in menu
    assert "<b>Reply</b> · Audio + text" in menu
    assert "Native target" not in menu
    assert "Retention" not in menu
    assert "Audio-model tools" not in menu

    assert len(keyboard) == 5
    assert all(len(row) == 2 for row in keyboard)
    assert [[button.text for button in row] for row in keyboard] == [
        ["✓ Auto", "Native"],
        ["TTS", "Off"],
        ["✓ Audio + text", "Audio only"],
        ["👩 Warm", "✓ 👩 Clear"],
        ["👨 Warm", "👨 Calm"],
    ]
    assert [[button.callback_data for button in row] for row in keyboard] == [
        ["voice:mode:auto", "voice:mode:native"],
        ["voice:mode:tts", "voice:mode:off"],
        ["voice:content:both", "voice:content:audio"],
        ["voice:profile:warm_female", "voice:profile:clear_female"],
        ["voice:profile:warm_male", "voice:profile:calm_male"],
    ]


@pytest.mark.asyncio
async def test_voice_profile_callback_updates_both_renderers_without_growing_menu(
    tmp_path,
):
    manager = _manager(tmp_path)
    manager.set_native_target("openrouter-api", "openai/gpt-audio-mini")
    runtime = FlexibleAgentRuntime.__new__(FlexibleAgentRuntime)
    runtime.voice_manager = manager
    runtime._is_authorized_user = lambda _user_id: True

    class Query:
        data = "voice:profile:calm_male"
        from_user = SimpleNamespace(id=7)

        def __init__(self):
            self.edits = []
            self.answers = []

        async def edit_message_text(self, text, **kwargs):
            self.edits.append((text, kwargs))

        async def answer(self, text=None, **kwargs):
            self.answers.append((text, kwargs))

    query = Query()
    await runtime.callback_voice(
        SimpleNamespace(callback_query=query), SimpleNamespace()
    )

    state = manager.get_state()
    assert state["voice_profile"] == "calm_male"
    assert state["native"]["voice"] == "echo"
    assert state["voice_name"] == "en-US-ChristopherNeural"
    assert len(query.edits) == 1
    assert "native echo" not in query.edits[0][0]
    assert len(query.edits[0][1]["reply_markup"].inline_keyboard) == 5
    assert query.answers[0][0].startswith("Voice set to 👨 Calm")


@pytest.mark.asyncio
async def test_semantic_profile_drives_language_aware_tts_synthesis(
    tmp_path, monkeypatch
):
    manager = _manager(tmp_path)
    manager.set_voice_profile("warm_male")
    manager.set_reply_mode("tts")
    calls = []

    class Provider:
        async def synthesize(self, **kwargs):
            calls.append(kwargs)
            output = Path(kwargs["output_dir"]) / "voice.ogg"
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(b"ogg")
            return VoiceAsset(
                provider="edge",
                text=kwargs["text"],
                spoken_text=kwargs["text"],
                wav_path=None,
                ogg_path=output,
            )

    monkeypatch.setattr(
        "orchestrator.voice_manager.build_provider",
        lambda *_args, **_kwargs: Provider(),
    )

    await manager.synthesize_reply("arale", "req-voice", "任务完成。")

    assert calls[0]["voice_name"] == "zh-CN-YunxiNeural"
