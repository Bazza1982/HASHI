from __future__ import annotations

import asyncio
from datetime import datetime
from types import SimpleNamespace

import pytest

from orchestrator.admin_local_testing import execute_local_command, supported_commands
from orchestrator.commands.queue import queue_callback
from orchestrator.runtime_common import QueuedRequest


class _FakeRuntime:
    def __init__(self):
        self.name = "zelda"
        self.global_config = SimpleNamespace(authorized_id=1)
        self.queue = asyncio.Queue()
        self.is_generating = False
        self.current_request_meta = None
        self.last_prompt = None
        self.last_response = None
        self.sent = []

    def _is_authorized_user(self, user_id):
        return user_id == 1

    async def send_long_message(self, chat_id, text, **kwargs):
        self.sent.append({"chat_id": chat_id, "text": text, "kwargs": kwargs})
        return SimpleNamespace(ok=True)


def _request(request_id: str, prompt: str = "Do the thing") -> QueuedRequest:
    return QueuedRequest(
        request_id=request_id,
        chat_id=123,
        prompt=prompt,
        source="text",
        summary=f"summary {request_id}",
        created_at=datetime.now().isoformat(),
    )


@pytest.mark.asyncio
async def test_queue_command_is_registry_command():
    runtime = _FakeRuntime()

    assert "queue" in supported_commands(runtime)


@pytest.mark.asyncio
async def test_queue_list_shows_empty_queue():
    runtime = _FakeRuntime()

    result = await execute_local_command(runtime, "/queue", chat_id=123)

    assert result["ok"] is True
    assert "Queue is empty." in result["messages"][0]["text"]


@pytest.mark.asyncio
async def test_queue_list_shows_pending_items():
    runtime = _FakeRuntime()
    await runtime.queue.put(_request("req-0001"))

    result = await execute_local_command(runtime, "/queue list", chat_id=123)

    text = result["messages"][0]["text"]
    assert "pending: 1" in text
    assert "req-0001" in text
    assert "summary req-0001" in text
    assert "reply_markup" in result["messages"][0]["meta"]
    assert "queue:cancel:req-0001" in result["messages"][0]["meta"]["reply_markup"]


@pytest.mark.asyncio
async def test_queue_show_displays_prompt():
    runtime = _FakeRuntime()
    await runtime.queue.put(_request("req-0002", prompt="Full prompt body"))

    result = await execute_local_command(runtime, "/queue show req-0002", chat_id=123)

    text = result["messages"][0]["text"]
    assert "Queue item" in text
    assert "Full prompt body" in text


@pytest.mark.asyncio
async def test_queue_cancel_removes_pending_item():
    runtime = _FakeRuntime()
    await runtime.queue.put(_request("req-0003"))
    await runtime.queue.put(_request("req-0004"))

    result = await execute_local_command(runtime, "/queue cancel req-0003", chat_id=123)

    assert result["ok"] is True
    assert "Cancelled 1 pending item" in result["messages"][0]["text"]
    remaining = list(runtime.queue._queue)
    assert [item.request_id for item in remaining] == ["req-0004"]
    assert runtime.queue.qsize() == 1


@pytest.mark.asyncio
async def test_queue_cancel_accepts_visible_list_number():
    runtime = _FakeRuntime()
    await runtime.queue.put(_request("req-0003"))
    await runtime.queue.put(_request("req-0004"))

    result = await execute_local_command(runtime, "/queue cancel 1", chat_id=123)

    assert result["ok"] is True
    assert "req-0003" in result["messages"][0]["text"]
    remaining = list(runtime.queue._queue)
    assert [item.request_id for item in remaining] == ["req-0004"]


@pytest.mark.asyncio
async def test_queue_clear_removes_all_pending_without_running():
    runtime = _FakeRuntime()
    runtime.is_generating = True
    runtime.current_request_meta = {
        "request_id": "req-running",
        "source": "text",
        "summary": "currently running",
    }
    await runtime.queue.put(_request("req-0005"))
    await runtime.queue.put(_request("req-0006"))

    result = await execute_local_command(runtime, "/queue clear", chat_id=123)

    assert "Cleared 2 pending item" in result["messages"][0]["text"]
    assert runtime.queue.qsize() == 0
    assert runtime.is_generating is True


@pytest.mark.asyncio
async def test_queue_history_uses_runtime_caches():
    runtime = _FakeRuntime()
    runtime.last_prompt = _request("req-last")
    runtime.last_response = {"request_id": "req-last", "text": "Last response text"}

    result = await execute_local_command(runtime, "/queue history", chat_id=123)

    text = result["messages"][0]["text"]
    assert "req-last" in text
    assert "Last response text" in text


class _FakeQuery:
    def __init__(self, data: str, user_id: int = 1):
        self.data = data
        self.from_user = SimpleNamespace(id=user_id)
        self.answers = []
        self.edits = []

    async def answer(self, *args, **kwargs):
        self.answers.append((args, kwargs))

    async def edit_message_text(self, **kwargs):
        self.edits.append(kwargs)


@pytest.mark.asyncio
async def test_queue_cancel_callback_removes_pending_item_and_refreshes_view():
    runtime = _FakeRuntime()
    await runtime.queue.put(_request("req-0007"))
    await runtime.queue.put(_request("req-0008"))
    query = _FakeQuery("queue:cancel:req-0007")
    update = SimpleNamespace(callback_query=query)

    await queue_callback(runtime, update, SimpleNamespace())

    remaining = list(runtime.queue._queue)
    assert [item.request_id for item in remaining] == ["req-0008"]
    assert query.answers
    assert "Cancelled pending item" in query.edits[-1]["text"]
    assert "req-0008" in query.edits[-1]["text"]


@pytest.mark.asyncio
async def test_queue_clear_callback_requires_confirmation():
    runtime = _FakeRuntime()
    await runtime.queue.put(_request("req-0009"))
    query = _FakeQuery("queue:clear:ask")

    await queue_callback(runtime, SimpleNamespace(callback_query=query), SimpleNamespace())

    assert runtime.queue.qsize() == 1
    assert "Clear all 1 pending" in query.edits[-1]["text"]
    assert "queue:clear:yes" in repr(query.edits[-1]["reply_markup"])
