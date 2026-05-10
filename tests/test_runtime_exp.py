from __future__ import annotations

from types import SimpleNamespace

import pytest

from orchestrator.commands import exp as runtime_exp


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
    )

    async def _reply_text(update, text, **kwargs):
        replies.append((text, kwargs))

    async def enqueue_request(chat_id, prompt, source, summary):
        queued.append((chat_id, prompt, source, summary))

    runtime._reply_text = _reply_text
    runtime.enqueue_request = enqueue_request
    return runtime, replies, queued


@pytest.mark.asyncio
async def test_cmd_exp_shows_usage_without_task(monkeypatch):
    runtime, replies, queued = _runtime()
    monkeypatch.setattr(runtime_exp, "get_exp_usage_text", lambda: "USAGE")

    await runtime_exp.cmd_exp(runtime, _update(), _context())

    assert replies[-1][0] == "USAGE"
    assert queued == []


@pytest.mark.asyncio
async def test_cmd_exp_enqueues_prompt(monkeypatch):
    runtime, replies, queued = _runtime()
    monkeypatch.setattr(runtime_exp, "build_exp_task_prompt", lambda task: f"PROMPT:{task}")

    await runtime_exp.cmd_exp(runtime, _update(), _context("make", "slides"))

    assert replies[-1][0] == "Running with EXP guidebook..."
    assert queued == [(456, "PROMPT:make slides", "exp", "EXP-guided task")]
