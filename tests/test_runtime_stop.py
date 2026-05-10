from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from orchestrator import runtime_stop


def _update():
    return SimpleNamespace(effective_user=SimpleNamespace(id=123))


def _context(*args):
    return SimpleNamespace(args=list(args))


class _Backend:
    def __init__(self):
        self.shutdown_calls = 0

    async def shutdown(self):
        self.shutdown_calls += 1


class _Queue:
    def __init__(self, size: int):
        self._items = list(range(size))
        self.task_done_calls = 0

    def qsize(self):
        return len(self._items)

    def empty(self):
        return not self._items

    def get_nowait(self):
        if not self._items:
            raise asyncio.QueueEmpty
        return self._items.pop(0)

    def task_done(self):
        self.task_done_calls += 1


def _runtime(queue_size=2):
    replies = []
    backend = _Backend()
    runtime = SimpleNamespace(
        name="lin_yueru",
        queue=_Queue(queue_size),
        config=SimpleNamespace(active_backend="claude"),
        backend_manager=SimpleNamespace(current_backend=backend),
        logger=SimpleNamespace(warning=lambda *args, **kwargs: None),
        _is_authorized_user=lambda user_id: True,
    )

    async def _reply_text(update, text, **kwargs):
        replies.append((text, kwargs))

    runtime._reply_text = _reply_text
    return runtime, backend, replies


@pytest.mark.asyncio
async def test_cmd_stop_shuts_down_backend_and_clears_queue():
    runtime, backend, replies = _runtime(queue_size=2)

    await runtime_stop.cmd_stop(runtime, _update(), _context())

    assert backend.shutdown_calls == 1
    assert runtime.queue.task_done_calls == 2
    assert replies[-1][0] == "Stopped execution. Cleared 2 queued messages and killed active backend process tree."
