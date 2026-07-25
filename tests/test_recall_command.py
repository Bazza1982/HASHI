from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from orchestrator import runtime_control
from orchestrator.runtime_command_binding import BOT_COMMAND_BINDINGS, COMMAND_BINDINGS


def _update() -> SimpleNamespace:
    message = SimpleNamespace(text="/recall", chat=SimpleNamespace(id=42))
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=1),
        effective_chat=SimpleNamespace(id=42),
        effective_message=message,
        message=message,
    )


def test_recall_is_registered_and_describes_non_interrupting_queue_clear():
    assert any(b.name == "recall" and b.method_name == "cmd_recall" for b in COMMAND_BINDINGS)
    binding = next(b for b in BOT_COMMAND_BINDINGS if b.name == "recall")
    assert binding.description == "Clear selected queued requests"


@pytest.mark.asyncio
async def test_recall_clears_waiting_requests_without_interrupting_active_task():
    replies: list[str] = []
    shutdown = AsyncMock()

    async def _reply(_update, text, **_kwargs):
        replies.append(text)

    queue: asyncio.Queue = asyncio.Queue()
    await queue.put(SimpleNamespace(request_id="queued-1"))
    await queue.put(SimpleNamespace(request_id="queued-2"))

    current_meta = {
        "request_id": "active-1",
        "chat_id": 42,
        "prompt": "Continue implementing the active task",
        "source": "text",
        "summary": "Active task",
    }
    runtime = SimpleNamespace(
        name="zelda",
        logger=SimpleNamespace(warning=lambda *a, **k: None),
        queue=queue,
        backend_manager=SimpleNamespace(current_backend=SimpleNamespace(shutdown=shutdown)),
        current_request_meta=current_meta,
        is_generating=True,
        _is_authorized_user=lambda _uid: True,
        _reply_text=_reply,
    )

    await runtime_control.cmd_recall(runtime, _update(), SimpleNamespace(args=[]))

    assert queue.empty()
    await asyncio.wait_for(queue.join(), timeout=0.1)
    shutdown.assert_not_awaited()
    assert runtime.current_request_meta is current_meta
    assert runtime.is_generating is True
    assert getattr(runtime, "_user_interrupt", None) is None
    assert replies == [
        "↩️ Recalled all 2 queued request(s).\n"
        "The current active task was not interrupted and will continue."
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("count", "expected_remaining", "expected_dropped"),
    [
        (1, ["queued-1", "queued-2"], 1),
        (2, ["queued-1"], 2),
        (1_000_000, [], 3),
    ],
)
async def test_recall_count_removes_newest_requests_and_preserves_fifo_order(
    count: int,
    expected_remaining: list[str],
    expected_dropped: int,
):
    replies: list[str] = []
    shutdown = AsyncMock()

    async def _reply(_update, text, **_kwargs):
        replies.append(text)

    queue: asyncio.Queue = asyncio.Queue()
    for request_id in ("queued-1", "queued-2", "queued-3"):
        await queue.put(SimpleNamespace(request_id=request_id))

    current_meta = {"request_id": "active-1", "prompt": "Keep working"}
    runtime = SimpleNamespace(
        name="zelda",
        logger=SimpleNamespace(warning=lambda *a, **k: None),
        queue=queue,
        backend_manager=SimpleNamespace(current_backend=SimpleNamespace(shutdown=shutdown)),
        current_request_meta=current_meta,
        is_generating=True,
        _is_authorized_user=lambda _uid: True,
        _reply_text=_reply,
    )

    await runtime_control.cmd_recall(
        runtime,
        _update(),
        SimpleNamespace(args=[str(count)]),
    )

    remaining = []
    while not queue.empty():
        item = queue.get_nowait()
        remaining.append(item.request_id)
        queue.task_done()
    await asyncio.wait_for(queue.join(), timeout=0.1)

    assert remaining == expected_remaining
    shutdown.assert_not_awaited()
    assert runtime.current_request_meta is current_meta
    assert getattr(runtime, "_user_interrupt", None) is None
    assert f"newest {expected_dropped}" in replies[0]


@pytest.mark.asyncio
async def test_recall_accepts_a_positive_count_beyond_python_int_conversion_limit():
    replies: list[str] = []
    queue: asyncio.Queue = asyncio.Queue()
    for request_id in ("queued-1", "queued-2"):
        await queue.put(SimpleNamespace(request_id=request_id))

    async def _reply(_update, text, **_kwargs):
        replies.append(text)

    runtime = SimpleNamespace(
        name="zelda",
        logger=SimpleNamespace(warning=lambda *a, **k: None),
        queue=queue,
        current_request_meta={"request_id": "active-1"},
        is_generating=True,
        _is_authorized_user=lambda _uid: True,
        _reply_text=_reply,
    )

    await runtime_control.cmd_recall(
        runtime,
        _update(),
        SimpleNamespace(args=["9" * 5000]),
    )

    assert queue.empty()
    await asyncio.wait_for(queue.join(), timeout=0.1)
    assert replies and "newest 2" in replies[0]


@pytest.mark.asyncio
async def test_recall_with_empty_queue_is_noop_for_active_backend():
    replies: list[str] = []
    shutdown = AsyncMock()

    async def _reply(_update, text, **_kwargs):
        replies.append(text)

    runtime = SimpleNamespace(
        name="zelda",
        logger=SimpleNamespace(warning=lambda *a, **k: None),
        queue=asyncio.Queue(),
        backend_manager=SimpleNamespace(current_backend=SimpleNamespace(shutdown=shutdown)),
        current_request_meta=None,
        is_generating=False,
        _is_authorized_user=lambda _uid: True,
        _reply_text=_reply,
    )

    await runtime_control.cmd_recall(runtime, _update(), SimpleNamespace(args=[]))

    shutdown.assert_not_awaited()
    assert getattr(runtime, "_user_interrupt", None) is None
    assert replies and "No queued requests to recall" in replies[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("args", [["0"], ["-1"], ["two"], ["1", "2"]])
async def test_recall_invalid_count_keeps_queue_unchanged(args: list[str]):
    replies: list[str] = []
    shutdown = AsyncMock()
    queue: asyncio.Queue = asyncio.Queue()
    original = [
        SimpleNamespace(request_id="queued-1"),
        SimpleNamespace(request_id="queued-2"),
    ]
    for item in original:
        await queue.put(item)

    async def _reply(_update, text, **_kwargs):
        replies.append(text)

    runtime = SimpleNamespace(
        name="zelda",
        logger=SimpleNamespace(warning=lambda *a, **k: None),
        queue=queue,
        backend_manager=SimpleNamespace(current_backend=SimpleNamespace(shutdown=shutdown)),
        current_request_meta={"request_id": "active-1"},
        is_generating=True,
        _is_authorized_user=lambda _uid: True,
        _reply_text=_reply,
    )

    await runtime_control.cmd_recall(runtime, _update(), SimpleNamespace(args=args))

    assert list(queue._queue) == original
    shutdown.assert_not_awaited()
    assert getattr(runtime, "_user_interrupt", None) is None
    assert replies and "Nothing was recalled" in replies[0]


@pytest.mark.asyncio
async def test_recall_ignores_unauthorized_user():
    reply = AsyncMock()
    queue: asyncio.Queue = asyncio.Queue()
    await queue.put(SimpleNamespace(request_id="queued-1"))
    runtime = SimpleNamespace(
        name="zelda",
        logger=SimpleNamespace(warning=lambda *a, **k: None),
        queue=queue,
        current_request_meta=None,
        is_generating=False,
        _is_authorized_user=lambda _uid: False,
        _reply_text=reply,
    )

    await runtime_control.cmd_recall(runtime, _update(), SimpleNamespace(args=[]))

    assert queue.qsize() == 1
    reply.assert_not_awaited()
