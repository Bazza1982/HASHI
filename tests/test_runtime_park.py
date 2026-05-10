from __future__ import annotations

from types import SimpleNamespace

import pytest

from orchestrator import runtime_park


def _update():
    return SimpleNamespace(effective_user=SimpleNamespace(id=123))


def _context(*args):
    return SimpleNamespace(args=list(args))


def _runtime(backend_busy=False, summary=None, removed=None, created_topic=None):
    replies = []
    create_calls = []
    runtime = SimpleNamespace(
        _is_authorized_user=lambda user_id: True,
        _format_parked_topics_text=lambda: "PARKED LIST",
        _backend_busy=lambda: backend_busy,
        session_id_dt="sess-1",
    )

    async def _reply_text(update, text, **kwargs):
        replies.append((text, kwargs))

    async def _summarize_current_topic_for_parking(title_override=None):
        return summary

    runtime._reply_text = _reply_text
    runtime._summarize_current_topic_for_parking = _summarize_current_topic_for_parking
    runtime.parked_topics = SimpleNamespace(
        delete_topic=lambda slot_id: removed,
        create_topic=lambda **kwargs: create_calls.append(kwargs) or created_topic,
    )
    return runtime, replies, create_calls


@pytest.mark.asyncio
async def test_cmd_park_lists_topics_without_args():
    runtime, replies, create_calls = _runtime()

    await runtime_park.cmd_park(runtime, _update(), _context())

    assert replies[-1][0] == "PARKED LIST"
    assert create_calls == []


@pytest.mark.asyncio
async def test_cmd_park_rejects_bad_delete_usage():
    runtime, replies, _ = _runtime()

    await runtime_park.cmd_park(runtime, _update(), _context("delete"))

    assert replies[-1][0] == "Usage: /park delete <slot>"


@pytest.mark.asyncio
async def test_cmd_park_deletes_topic():
    runtime, replies, _ = _runtime(removed={"title": "Alpha"})

    await runtime_park.cmd_park(runtime, _update(), _context("delete", "3"))

    assert replies[-1][0] == "Deleted parked topic [3] Alpha"


@pytest.mark.asyncio
async def test_cmd_park_blocks_when_busy():
    runtime, replies, _ = _runtime(backend_busy=True)

    await runtime_park.cmd_park(runtime, _update(), _context("chat"))

    assert replies[-1][0] == "Parking is blocked while a request is running or queued."


@pytest.mark.asyncio
async def test_cmd_park_reports_missing_summary():
    runtime, replies, _ = _runtime(summary=None)

    await runtime_park.cmd_park(runtime, _update(), _context("chat"))

    assert replies[0][0] == "Parking the current topic and writing a resume summary..."
    assert replies[-1][0] == "No recent bridge transcript was available to park."


@pytest.mark.asyncio
async def test_cmd_park_creates_topic_and_reports_slot():
    summary = {
        "title": "Topic A",
        "summary_short": "Short",
        "summary_long": "Long",
        "recent_context": "Recent",
        "last_user_text": "User",
        "last_assistant_text": "Assistant",
        "last_exchange_text": "Exchange",
    }
    topic = {"slot_id": 7, "title": "Topic A", "summary_short": "Short"}
    runtime, replies, create_calls = _runtime(summary=summary, created_topic=topic)

    await runtime_park.cmd_park(runtime, _update(), _context("chat", "Custom", "Title"))

    assert create_calls and create_calls[-1]["title_user_override"] == "Custom Title"
    assert replies[-1][0].startswith("Parked as [7] Topic A")
    assert "Use /load 7 to resume" in replies[-1][0]
