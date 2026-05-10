from __future__ import annotations

from types import SimpleNamespace

import pytest

from orchestrator import runtime_safevoice


def _update():
    return SimpleNamespace(effective_user=SimpleNamespace(id=123))


def _callback_update(data):
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


def _runtime(enabled=False):
    replies = []
    state_calls = []
    enqueued = []
    runtime = SimpleNamespace(
        _safevoice_enabled=enabled,
        _pending_voice={"456": {"prompt": "hi", "summary": "sum", "transcript": "heard text"}},
        _is_authorized_user=lambda user_id: True,
    )

    def _set_skill_state(key, value):
        state_calls.append((key, value))

    async def _reply_text(update, text, **kwargs):
        replies.append((text, kwargs))

    async def enqueue_request(chat_id, prompt, source, summary):
        enqueued.append((chat_id, prompt, source, summary))

    runtime._set_skill_state = _set_skill_state
    runtime._reply_text = _reply_text
    runtime.enqueue_request = enqueue_request
    return runtime, replies, state_calls, enqueued


@pytest.mark.asyncio
async def test_cmd_safevoice_shows_status():
    runtime, replies, _state_calls, _enqueued = _runtime(enabled=True)

    await runtime_safevoice.cmd_safevoice(runtime, _update(), _context())

    assert replies[-1][0] == "Safe Voice: ON 🛡️\nUsage: /safevoice on | off"


@pytest.mark.asyncio
async def test_cmd_safevoice_turns_on():
    runtime, replies, state_calls, _enqueued = _runtime(enabled=False)

    await runtime_safevoice.cmd_safevoice(runtime, _update(), _context("on"))

    assert runtime._safevoice_enabled is True
    assert state_calls == [("safevoice", True)]
    assert replies[-1][0] == "🛡️ Safe Voice ON — voice messages will require confirmation before sending to agent."


@pytest.mark.asyncio
async def test_cmd_safevoice_turns_off_and_clears_pending():
    runtime, replies, state_calls, _enqueued = _runtime(enabled=True)

    await runtime_safevoice.cmd_safevoice(runtime, _update(), _context("off"))

    assert runtime._safevoice_enabled is False
    assert runtime._pending_voice == {}
    assert state_calls == [("safevoice", False)]
    assert replies[-1][0] == "Safe Voice OFF — voice messages go directly to agent."


@pytest.mark.asyncio
async def test_callback_safevoice_confirms_and_enqueues():
    runtime, _replies, _state_calls, enqueued = _runtime(enabled=True)
    update, answers, edits = _callback_update("safevoice:yes:456")

    await runtime_safevoice.callback_safevoice(runtime, update, SimpleNamespace())

    assert edits[-1][0] == "✅ Confirmed. Sending to agent:\n\n_heard text_"
    assert edits[-1][1]["parse_mode"] == "Markdown"
    assert answers[-1] == ("Sending...", False)
    assert enqueued == [(456, "hi", "voice_transcript", "sum")]
