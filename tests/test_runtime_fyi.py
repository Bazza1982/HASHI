from __future__ import annotations

from types import SimpleNamespace

import pytest

from orchestrator import runtime_fyi


def _update():
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=123),
        effective_chat=SimpleNamespace(id=456),
    )


def _context(*args):
    return SimpleNamespace(args=list(args))


def _runtime():
    replies = []
    queued = []
    runtime = SimpleNamespace(
        _is_authorized_user=lambda user_id: True,
        _build_fyi_request_prompt=lambda prompt_text="": f"FYI::{prompt_text}",
    )

    async def _reply_text(update, text, **kwargs):
        replies.append((text, kwargs))

    async def _enqueue_request(chat_id, prompt, source, summary):
        queued.append((chat_id, prompt, source, summary))

    runtime._reply_text = _reply_text
    runtime.enqueue_request = _enqueue_request
    return runtime, replies, queued


@pytest.mark.asyncio
async def test_cmd_fyi_enqueues_refresh_request():
    runtime, replies, queued = _runtime()

    await runtime_fyi.cmd_fyi(runtime, _update(), _context("please", "refresh"))

    assert replies[-1][0] == "Refreshing AGENT FYI..."
    assert queued == [(456, "FYI::please refresh", "fyi", "AGENT FYI refresh")]
