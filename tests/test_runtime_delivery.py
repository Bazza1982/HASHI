import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from telegram import constants

from orchestrator import runtime_delivery


class _Logger:
    def __init__(self):
        self.messages = []

    def info(self, message):
        self.messages.append(("info", message))

    def warning(self, message):
        self.messages.append(("warning", message))


class _Bot:
    def __init__(self):
        self.messages = []
        self.actions = []

    async def send_message(self, **kwargs):
        self.messages.append(kwargs)

    async def send_chat_action(self, **kwargs):
        self.actions.append(kwargs)


def _runtime(tmp_path: Path, *, connected: bool = True):
    bot = _Bot()
    return SimpleNamespace(
        app=SimpleNamespace(bot=bot),
        config=SimpleNamespace(active_backend="codex-cli"),
        logger=_Logger(),
        session_dir=tmp_path,
        telegram_connected=connected,
        telegram_logger=_Logger(),
        workspace_dir=tmp_path,
    )


@pytest.mark.asyncio
async def test_send_long_message_skips_when_telegram_disconnected(tmp_path):
    runtime = _runtime(tmp_path, connected=False)

    elapsed, chunks = await runtime_delivery.send_long_message(
        runtime,
        chat_id=123,
        text="hello",
        request_id="req-1",
    )

    assert (elapsed, chunks) == (0.0, 0)
    assert runtime.app.bot.messages == []
    assert "Telegram disconnected" in runtime.logger.messages[0][1]


@pytest.mark.asyncio
async def test_send_long_message_sends_html_by_default(tmp_path):
    runtime = _runtime(tmp_path)

    _elapsed, chunks = await runtime_delivery.send_long_message(
        runtime,
        chat_id=123,
        text="**hello**",
        request_id="req-2",
    )

    assert chunks == 1
    assert runtime.app.bot.messages == [
        {
            "chat_id": 123,
            "text": "<b>hello</b>",
            "parse_mode": constants.ParseMode.HTML,
        }
    ]


@pytest.mark.asyncio
async def test_send_long_message_error_uses_plain_summary(tmp_path):
    runtime = _runtime(tmp_path)

    _elapsed, chunks = await runtime_delivery.send_long_message(
        runtime,
        chat_id=123,
        text="x" * 3000,
        request_id="req-err",
        purpose="error",
    )

    assert chunks == 1
    message = runtime.app.bot.messages[0]
    assert message["chat_id"] == 123
    assert "parse_mode" not in message
    assert "Backend error (codex-cli) | req-err" in message["text"]
    assert "Full log (local):" in message["text"]
    assert "... (truncated) ..." in message["text"]


@pytest.mark.asyncio
async def test_typing_loop_sends_action_until_stopped(tmp_path):
    runtime = _runtime(tmp_path)
    stop_event = asyncio.Event()
    task = asyncio.create_task(runtime_delivery.typing_loop(runtime, 123, stop_event))
    await asyncio.sleep(0)
    stop_event.set()
    await task

    assert runtime.app.bot.actions == [
        {"chat_id": 123, "action": constants.ChatAction.TYPING}
    ]
