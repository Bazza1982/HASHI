from __future__ import annotations

from types import SimpleNamespace

import pytest

from orchestrator import runtime_say


def _update():
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=123),
        effective_chat=SimpleNamespace(id=456),
    )


def _context(*args):
    return SimpleNamespace(args=list(args))


def _runtime(text="hello", send_ok=True):
    replies = []
    sends = []
    runtime = SimpleNamespace(
        _is_authorized_user=lambda user_id: True,
        _load_last_text_from_transcript=lambda role: text if role == "assistant" else None,
    )

    async def _reply_text(update, text, **kwargs):
        replies.append((text, kwargs))

    async def _send_voice_reply(chat_id, text, request_id, force=False):
        sends.append((chat_id, text, request_id, force))
        return send_ok

    runtime._reply_text = _reply_text
    runtime._send_voice_reply = _send_voice_reply
    return runtime, replies, sends


@pytest.mark.asyncio
async def test_cmd_say_reports_missing_text():
    runtime, replies, sends = _runtime(text=None)

    await runtime_say.cmd_say(runtime, _update(), _context())

    assert replies[-1][0] == "No recent message to read."
    assert sends == []


@pytest.mark.asyncio
async def test_cmd_say_sends_voice_reply():
    runtime, replies, sends = _runtime(text="hello", send_ok=True)

    await runtime_say.cmd_say(runtime, _update(), _context())

    assert replies == []
    assert sends and sends[-1][0] == 456
    assert sends[-1][1] == "hello"
    assert sends[-1][3] is True


@pytest.mark.asyncio
async def test_cmd_say_reports_send_failure():
    runtime, replies, sends = _runtime(text="hello", send_ok=False)

    await runtime_say.cmd_say(runtime, _update(), _context())

    assert sends
    assert replies[-1][0] == "Voice synthesis failed. Check /voice status for provider settings."
