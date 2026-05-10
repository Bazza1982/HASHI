from __future__ import annotations

from types import SimpleNamespace

import pytest

from orchestrator import runtime_load


def _update():
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=123),
        effective_chat=SimpleNamespace(id=456),
    )


def _context(*args):
    return SimpleNamespace(args=list(args))


def _runtime(backend_busy=False, topic=None):
    replies = []
    requests = []
    primers = []
    loaded = []
    runtime = SimpleNamespace(
        _is_authorized_user=lambda user_id: True,
        _backend_busy=lambda: backend_busy,
        _pending_auto_recall_context=None,
    )

    async def _reply_text(update, text, **kwargs):
        replies.append((text, kwargs))

    async def enqueue_request(chat_id, prompt, source, summary):
        requests.append((chat_id, prompt, source, summary))

    def _arm_session_primer(text):
        primers.append(text)

    runtime._reply_text = _reply_text
    runtime.enqueue_request = enqueue_request
    runtime._arm_session_primer = _arm_session_primer
    runtime.parked_topics = SimpleNamespace(
        get_topic=lambda slot_id: topic,
        mark_loaded=lambda slot_id: loaded.append(slot_id),
    )
    return runtime, replies, requests, primers, loaded


@pytest.mark.asyncio
async def test_cmd_load_rejects_bad_usage():
    runtime, replies, requests, primers, loaded = _runtime()

    await runtime_load.cmd_load(runtime, _update(), _context("bad"))

    assert replies[-1][0] == "Usage: /load <slot>"


@pytest.mark.asyncio
async def test_cmd_load_blocks_when_busy():
    runtime, replies, requests, primers, loaded = _runtime(backend_busy=True)

    await runtime_load.cmd_load(runtime, _update(), _context("3"))

    assert replies[-1][0] == "Load is blocked while a request is running or queued."


@pytest.mark.asyncio
async def test_cmd_load_reports_missing_topic():
    runtime, replies, requests, primers, loaded = _runtime(topic=None)

    await runtime_load.cmd_load(runtime, _update(), _context("3"))

    assert replies[-1][0] == "Parked topic [3] was not found."


@pytest.mark.asyncio
async def test_cmd_load_restores_topic_and_enqueues_resume():
    topic = {
        "title": "Topic A",
        "summary_short": "Short",
        "summary_long": "Long",
        "recent_context": "Recent",
        "last_exchange_text": "Exchange",
    }
    runtime, replies, requests, primers, loaded = _runtime(topic=topic)

    await runtime_load.cmd_load(runtime, _update(), _context("5"))

    assert loaded == [5]
    assert "PARKED TOPIC [5]" in runtime._pending_auto_recall_context
    assert primers[-1] == "Loading parked topic [5] Topic A. Resume it as the active working context."
    assert replies[-1][0] == "Loading parked topic [5] Topic A and restoring continuity..."
    assert requests == [(
        456,
        "SYSTEM: Resume the parked topic that was just restored into context. Continue naturally from the most relevant unfinished point. Do not explain the restore process at length.\n\nResume the topic now.",
        "park-load",
        "Parked topic load [5]",
    )]
