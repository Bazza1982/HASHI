from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestrator import runtime_hchat


def _update():
    return SimpleNamespace(effective_user=SimpleNamespace(id=123))


def _context(*args):
    return SimpleNamespace(args=list(args))


def _runtime():
    replies = []
    enqueued = []
    runtime = SimpleNamespace(
        _is_authorized_user=lambda user_id: True,
        name="lily",
        global_config=SimpleNamespace(config_path=Path("/tmp/config.json")),
        orchestrator=None,
    )

    async def _reply_text(update, text, **kwargs):
        replies.append((text, kwargs))

    async def enqueue_api_text(text, source, deliver_to_telegram):
        enqueued.append((text, source, deliver_to_telegram))

    runtime._reply_text = _reply_text
    runtime.enqueue_api_text = enqueue_api_text
    return runtime, replies, enqueued


@pytest.mark.asyncio
async def test_cmd_hchat_shows_usage_without_enough_args():
    runtime, replies, enqueued = _runtime()

    await runtime_hchat.cmd_hchat(runtime, _update(), _context("lily"))

    assert "Usage: <code>/hchat" in replies[-1][0]
    assert replies[-1][1]["parse_mode"] == "HTML"
    assert enqueued == []


@pytest.mark.asyncio
async def test_cmd_hchat_reports_missing_group_directory():
    runtime, replies, enqueued = _runtime()

    await runtime_hchat.cmd_hchat(runtime, _update(), _context("@staff", "status"))

    assert replies[-1][0] == "❌ Agent directory unavailable for group resolution."
    assert enqueued == []


@pytest.mark.asyncio
async def test_cmd_hchat_enqueues_single_target_task():
    runtime, replies, enqueued = _runtime()

    await runtime_hchat.cmd_hchat(runtime, _update(), _context("rika", "share", "update"))

    assert replies[-1][0] == "💬 Composing Hchat message to <b>rika</b>..."
    assert replies[-1][1]["parse_mode"] == "HTML"
    assert enqueued[-1][1:] == ("bridge:hchat", True)
    assert 'agent "rika"' in enqueued[-1][0]
    assert "Intent: share update" in enqueued[-1][0]
