from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestrator import runtime_delivery


class _Logger:
    def __init__(self):
        self.messages = []

    def info(self, message):
        self.messages.append(message)

    def warning(self, message):
        self.messages.append(message)


class _Message:
    def __init__(self):
        self.calls = 0

    async def reply_text(self, text, **kwargs):
        self.calls += 1
        return {"text": text, "kwargs": kwargs}


class _Bot:
    def __init__(self):
        self.sent = []
        self.actions = []

    async def send_message(self, **kwargs):
        self.sent.append(kwargs)
        return kwargs

    async def send_chat_action(self, **kwargs):
        self.actions.append(kwargs)


def _runtime(tmp_path: Path, *, telegram_connected: bool = True):
    runtime = SimpleNamespace()
    runtime.telegram_connected = telegram_connected
    runtime.logger = _Logger()
    runtime.telegram_logger = _Logger()
    runtime.app = SimpleNamespace(bot=_Bot())
    runtime.config = SimpleNamespace(active_backend="codex-cli")
    runtime.workspace_dir = tmp_path
    runtime.session_dir = tmp_path / "session"
    runtime.session_dir.mkdir()
    return runtime


@pytest.mark.asyncio
async def test_reply_text_and_send_text_delegate_to_telegram_objects(tmp_path: Path):
    runtime = _runtime(tmp_path)
    update = SimpleNamespace(message=_Message())
    reply = await runtime_delivery.reply_text(runtime, update, "hello", parse_mode="HTML")
    sent = await runtime_delivery.send_text(runtime, 123, "world")
    assert reply["text"] == "hello"
    assert sent["text"] == "world"


@pytest.mark.asyncio
async def test_send_long_message_skips_when_telegram_disconnected(tmp_path: Path):
    runtime = _runtime(tmp_path, telegram_connected=False)
    elapsed, chunks = await runtime_delivery.send_long_message(runtime, chat_id=1, text="hello")
    assert elapsed == 0.0
    assert chunks == 0


@pytest.mark.asyncio
async def test_send_long_message_handles_error_and_normal_paths(tmp_path: Path):
    runtime = _runtime(tmp_path)
    elapsed, chunks = await runtime_delivery.send_long_message(
        runtime,
        chat_id=1,
        text="plain response",
        request_id="req-1",
    )
    assert chunks == 1
    assert runtime.app.bot.sent[-1]["parse_mode"] == "HTML"

    runtime.app.bot.sent.clear()
    _, err_chunks = await runtime_delivery.send_long_message(
        runtime,
        chat_id=1,
        text="boom" * 1000,
        request_id="req-2",
        purpose="error",
    )
    assert err_chunks == 1
    assert "Backend error" in runtime.app.bot.sent[-1]["text"]


@pytest.mark.asyncio
async def test_typing_loop_sends_actions_until_stopped(tmp_path: Path):
    runtime = _runtime(tmp_path)
    stop_event = asyncio.Event()
    task = asyncio.create_task(runtime_delivery.typing_loop(runtime, 9, stop_event))
    await asyncio.sleep(0.05)
    stop_event.set()
    await task
    assert runtime.app.bot.actions[0]["chat_id"] == 9
