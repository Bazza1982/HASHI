from __future__ import annotations

from types import SimpleNamespace

import pytest

from orchestrator import runtime_voice


def _update():
    return SimpleNamespace(effective_user=SimpleNamespace(id=123))


def _callback_update(data="voice:toggle:on"):
    answers = []
    edits = []

    async def answer(text=None, show_alert=False):
        answers.append((text, show_alert))

    async def edit_message_text(text, **kwargs):
        edits.append((text, kwargs))

    return SimpleNamespace(
        callback_query=SimpleNamespace(
            from_user=SimpleNamespace(id=123),
            data=data,
            answer=answer,
            edit_message_text=edit_message_text,
        )
    ), answers, edits


def _context(*args):
    return SimpleNamespace(args=list(args))


class _VoiceManager:
    def voice_menu_text(self):
        return "voice menu"

    def provider_hints(self):
        return "provider hints"

    def apply_voice_preset(self, alias):
        return f"preset {alias}"

    def get_provider_name(self):
        return "piper"

    def set_provider(self, name):
        return f"provider {name}"

    def set_voice_name(self, name):
        return f"name {name}"

    def set_rate(self, rate):
        return f"rate {rate}"

    def set_enabled(self, enabled):
        return f"enabled {enabled}"


def _runtime():
    replies = []
    runtime = SimpleNamespace(
        voice_manager=_VoiceManager(),
        _is_authorized_user=lambda user_id: True,
        _voice_keyboard=lambda: "kbd",
    )

    async def _reply_text(update, text, **kwargs):
        replies.append((text, kwargs))

    runtime._reply_text = _reply_text
    return runtime, replies


@pytest.mark.asyncio
async def test_cmd_voice_shows_status_menu():
    runtime, replies = _runtime()

    await runtime_voice.cmd_voice(runtime, _update(), _context())

    assert replies[-1][0] == "voice menu"
    assert replies[-1][1]["reply_markup"] == "kbd"


@pytest.mark.asyncio
async def test_cmd_voice_sets_provider():
    runtime, replies = _runtime()

    await runtime_voice.cmd_voice(runtime, _update(), _context("provider", "elevenlabs"))

    assert replies[-1][0] == "provider elevenlabs"


@pytest.mark.asyncio
async def test_cmd_voice_rejects_bad_rate():
    runtime, replies = _runtime()

    await runtime_voice.cmd_voice(runtime, _update(), _context("rate", "abc"))

    assert replies[-1][0] == "Voice rate must be an integer."


@pytest.mark.asyncio
async def test_callback_voice_updates_menu_after_toggle():
    runtime, _replies = _runtime()
    update, answers, edits = _callback_update("voice:toggle:on")

    await runtime_voice.callback_voice(runtime, update, SimpleNamespace())

    assert edits[-1][0] == "voice menu\n\nenabled True"
    assert edits[-1][1]["reply_markup"] == "kbd"
    assert answers[-1] == (None, False)
