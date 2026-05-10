from __future__ import annotations

from types import SimpleNamespace

import pytest

from orchestrator import runtime_retry


def _update():
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=123),
        effective_chat=SimpleNamespace(id=456),
    )


def _context(*args):
    return SimpleNamespace(args=list(args))


def _runtime():
    replies = []
    sent = []
    queued = []
    runtime = SimpleNamespace(
        _is_authorized_user=lambda user_id: True,
        last_response=None,
        last_prompt=None,
        _load_last_text_from_transcript=lambda role: None,
    )

    async def _reply_text(update, text, **kwargs):
        replies.append((text, kwargs))

    async def _send_long_message(**kwargs):
        sent.append(kwargs)

    async def _enqueue_request(chat_id, prompt, source, summary):
        queued.append((chat_id, prompt, source, summary))

    runtime._reply_text = _reply_text
    runtime.send_long_message = _send_long_message
    runtime.enqueue_request = _enqueue_request
    return runtime, replies, sent, queued


@pytest.mark.asyncio
async def test_cmd_retry_restores_response_from_transcript():
    runtime, replies, sent, queued = _runtime()
    runtime._load_last_text_from_transcript = lambda role: "saved reply" if role == "assistant" else None

    await runtime_retry.cmd_retry(runtime, _update(), _context("response"))

    assert replies[-1][0] == "Restoring last response from transcript..."
    assert sent == [{"chat_id": 456, "text": "saved reply", "purpose": "retry-response"}]
    assert queued == []


@pytest.mark.asyncio
async def test_cmd_retry_retries_last_prompt_when_requested():
    runtime, replies, sent, queued = _runtime()
    runtime.last_prompt = SimpleNamespace(chat_id=789, prompt="hello")

    await runtime_retry.cmd_retry(runtime, _update(), _context("prompt"))

    assert replies[-1][0] == "Retrying last prompt..."
    assert queued == [(789, "hello", "retry", "Retry request")]
    assert sent == []


@pytest.mark.asyncio
async def test_cmd_retry_shows_choice_markup_for_unknown_mode():
    runtime, replies, sent, queued = _runtime()

    await runtime_retry.cmd_retry(runtime, _update(), _context("weird"))

    assert replies[-1][0] == "Retry — choose action:"
    assert "reply_markup" in replies[-1][1]
    assert sent == []
    assert queued == []
