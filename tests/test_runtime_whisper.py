from __future__ import annotations

from types import SimpleNamespace

import pytest

from orchestrator import runtime_whisper


def _update():
    return SimpleNamespace(effective_user=SimpleNamespace(id=123))


def _context(*args):
    return SimpleNamespace(args=list(args))


class _Transcriber:
    def __init__(self, model_size="medium"):
        self.model_size = model_size
        self._model = "loaded"


def _runtime():
    replies = []
    runtime = SimpleNamespace(_is_authorized_user=lambda user_id: True)

    async def _reply_text(update, text, **kwargs):
        replies.append((text, kwargs))

    runtime._reply_text = _reply_text
    return runtime, replies


@pytest.mark.asyncio
async def test_cmd_whisper_shows_current_model(monkeypatch):
    runtime, replies = _runtime()
    transcriber = _Transcriber("medium")
    monkeypatch.setattr(runtime_whisper, "get_transcriber", lambda: transcriber)

    await runtime_whisper.cmd_whisper(runtime, _update(), _context())

    assert replies[-1][0] == "Whisper model: <b>medium</b>"
    assert replies[-1][1]["parse_mode"] == "HTML"


@pytest.mark.asyncio
async def test_cmd_whisper_sets_model_size(monkeypatch):
    runtime, replies = _runtime()
    transcriber = _Transcriber("small")
    monkeypatch.setattr(runtime_whisper, "get_transcriber", lambda: transcriber)

    await runtime_whisper.cmd_whisper(runtime, _update(), _context("large"))

    assert transcriber.model_size == "large-v3"
    assert transcriber._model is None
    assert replies[-1][0] == "✅ Whisper model set to: large-v3. It will load on the next voice message."


@pytest.mark.asyncio
async def test_cmd_whisper_rejects_unknown_value(monkeypatch):
    runtime, replies = _runtime()
    transcriber = _Transcriber("small")
    monkeypatch.setattr(runtime_whisper, "get_transcriber", lambda: transcriber)

    await runtime_whisper.cmd_whisper(runtime, _update(), _context("tiny"))

    assert replies[-1][0] == "Usage: /whisper [small|medium|large]"
