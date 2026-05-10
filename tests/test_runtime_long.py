from __future__ import annotations

from types import SimpleNamespace

import pytest

from orchestrator import runtime_long


class _DummyTask:
    def __init__(self, done: bool = False):
        self._done = done
        self.cancelled = False

    def done(self):
        return self._done

    def cancel(self):
        self.cancelled = True


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
        _long_buffer=[],
        _long_buffer_active=False,
        _long_buffer_chat_id=None,
        _long_buffer_timeout_task=None,
        name="sunny",
    )

    async def _reply_text(update, text, **kwargs):
        replies.append((text, kwargs))

    async def enqueue_request(chat_id, prompt, source, summary):
        queued.append((chat_id, prompt, source, summary))

    async def _long_buffer_timeout():
        return None

    runtime._reply_text = _reply_text
    runtime.enqueue_request = enqueue_request
    runtime._long_buffer_timeout = _long_buffer_timeout
    return runtime, replies, queued


@pytest.mark.asyncio
async def test_cmd_long_starts_buffering(monkeypatch):
    runtime, replies, queued = _runtime()
    created = []

    def _create_task(coro):
        created.append(coro)
        coro.close()
        return _DummyTask()

    monkeypatch.setattr(runtime_long.asyncio, "create_task", _create_task)

    await runtime_long.cmd_long(runtime, _update(), _context("hello"))

    assert runtime._long_buffer_active is True
    assert runtime._long_buffer == ["hello"]
    assert runtime._long_buffer_chat_id == 456
    assert replies[-1][0] == "📝 /long mode started. Paste your text, then send /end to submit."
    assert created


@pytest.mark.asyncio
async def test_cmd_long_rejects_nested_session():
    runtime, replies, queued = _runtime()
    runtime._long_buffer_active = True

    await runtime_long.cmd_long(runtime, _update(), _context())

    assert replies[-1][0] == "⏳ Already in /long mode. Send /end to finish."


@pytest.mark.asyncio
async def test_cmd_end_reports_missing_session():
    runtime, replies, queued = _runtime()

    await runtime_long.cmd_end(runtime, _update(), _context())

    assert replies[-1][0] == "No /long session active."


@pytest.mark.asyncio
async def test_cmd_end_reports_empty_buffer():
    runtime, replies, queued = _runtime()
    runtime._long_buffer_active = True
    runtime._long_buffer_timeout_task = _DummyTask()

    await runtime_long.cmd_end(runtime, _update(), _context())

    assert replies[-1][0] == "⚠️ /long buffer was empty, nothing to submit."


@pytest.mark.asyncio
async def test_cmd_end_submits_buffered_text():
    runtime, replies, queued = _runtime()
    runtime._long_buffer_active = True
    runtime._long_buffer = ["line 1", "line 2"]
    runtime._long_buffer_timeout_task = _DummyTask()

    await runtime_long.cmd_end(runtime, _update(), _context())

    assert replies[-1][0] == "✅ Collected 2 lines. Submitting..."
    assert queued == [(456, "line 1\nline 2", "text", "line 1 line 2")]
