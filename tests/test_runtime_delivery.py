import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from telegram import constants
from telegram.error import RetryAfter

from orchestrator import runtime_delivery


class _Logger:
    def __init__(self):
        self.messages = []

    def info(self, message):
        self.messages.append(("info", message))

    def warning(self, message):
        self.messages.append(("warning", message))


class _Bot:
    def __init__(self, *, error=None):
        self.messages = []
        self.actions = []
        self.error = error

    async def send_message(self, **kwargs):
        if self.error is not None:
            raise self.error
        self.messages.append(kwargs)

    async def send_chat_action(self, **kwargs):
        self.actions.append(kwargs)


def _runtime(tmp_path: Path, *, connected: bool = True, bot_error=None):
    bot = _Bot(error=bot_error)
    return SimpleNamespace(
        app=SimpleNamespace(bot=bot),
        config=SimpleNamespace(active_backend="codex-cli", telegram_token_key="test-agent"),
        global_config=SimpleNamespace(project_root=tmp_path),
        logger=_Logger(),
        name="test-agent",
        session_dir=tmp_path,
        telegram_connected=connected,
        telegram_logger=_Logger(),
        workspace_dir=tmp_path,
        _notify_enabled=False,
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
            "disable_notification": True,
        }
    ]


@pytest.mark.asyncio
async def test_send_long_message_preserves_pre_rendered_html(tmp_path):
    runtime = _runtime(tmp_path)

    _elapsed, chunks = await runtime_delivery.send_long_message(
        runtime,
        chat_id=123,
        text="⚠️ <b>UNFINISHED WORK</b>\n<code>/compact</code>",
        request_id="req-html-card",
        parse_mode="HTML",
    )

    assert chunks == 1
    assert runtime.app.bot.messages == [
        {
            "chat_id": 123,
            "text": "⚠️ <b>UNFINISHED WORK</b>\n<code>/compact</code>",
            "parse_mode": constants.ParseMode.HTML,
            "disable_notification": True,
        }
    ]


@pytest.mark.asyncio
async def test_send_long_message_elapsed_uses_monotonic_clock(tmp_path, monkeypatch):
    runtime = _runtime(tmp_path)
    ticks = iter((100.0, 100.25))
    monkeypatch.setattr(runtime_delivery, "monotonic", lambda: next(ticks))

    elapsed, chunks = await runtime_delivery.send_long_message(
        runtime,
        chat_id=123,
        text="hello",
        request_id="req-monotonic",
    )

    assert chunks == 1
    assert elapsed == pytest.approx(0.25)


@pytest.mark.asyncio
async def test_send_long_message_respects_notify_on(tmp_path):
    runtime = _runtime(tmp_path)
    runtime._notify_enabled = True

    _elapsed, chunks = await runtime_delivery.send_long_message(
        runtime,
        chat_id=123,
        text="hello",
        request_id="req-2b",
    )

    assert chunks == 1
    assert runtime.app.bot.messages[0]["disable_notification"] is False


@pytest.mark.asyncio
async def test_legacy_notification_signature_cannot_block_final_delivery(
    tmp_path, monkeypatch
):
    runtime = _runtime(tmp_path)
    monkeypatch.setattr(
        runtime_delivery.telegram_notifications,
        "disable_notification",
        lambda _runtime: True,
    )

    _elapsed, chunks = await runtime_delivery.send_long_message(
        runtime,
        chat_id=123,
        text="final answer",
        request_id="req-legacy-notify",
        purpose="response",
    )

    assert chunks == 1
    assert runtime.app.bot.messages[0]["text"] == "final answer"
    assert runtime.app.bot.messages[0]["disable_notification"] is True
    assert any("compatibility fallback" in text for _level, text in runtime.telegram_logger.messages)


@pytest.mark.asyncio
async def test_notification_policy_exception_cannot_block_final_delivery(
    tmp_path, monkeypatch
):
    runtime = _runtime(tmp_path)

    def broken_policy(*_args, **_kwargs):
        raise RuntimeError("policy broke")

    monkeypatch.setattr(
        runtime_delivery.telegram_notifications,
        "disable_notification",
        broken_policy,
    )

    _elapsed, chunks = await runtime_delivery.send_long_message(
        runtime,
        chat_id=123,
        text="still delivered",
        request_id="req-broken-notify",
        purpose="response",
    )

    assert chunks == 1
    assert runtime.app.bot.messages[0]["text"] == "still delivered"
    assert runtime.app.bot.messages[0]["disable_notification"] is False
    assert any("sending audibly" in text for _level, text in runtime.telegram_logger.messages)


@pytest.mark.asyncio
async def test_quiet_mode_silences_interim_but_not_final_or_error(tmp_path):
    runtime = _runtime(tmp_path)
    runtime._notify_mode = "quiet"

    await runtime_delivery.send_long_message(
        runtime, chat_id=123, text="working", request_id="req-q1", purpose="task_commentary"
    )
    await runtime_delivery.send_long_message(
        runtime,
        chat_id=123,
        text="starting",
        request_id="req-q1b",
        purpose="task_acknowledgement",
    )
    await runtime_delivery.send_long_message(
        runtime, chat_id=123, text="done", request_id="req-q2", purpose="response"
    )
    await runtime_delivery.send_long_message(
        runtime, chat_id=123, text="failure", request_id="req-q3", purpose="error"
    )
    await runtime_delivery.send_long_message(
        runtime,
        chat_id=123,
        text="compaction problem",
        request_id="req-q4",
        purpose="context-compaction-warning",
    )
    await runtime_delivery.send_long_message(
        runtime,
        chat_id=123,
        text="background complete",
        request_id="req-q5",
        purpose="bg-response",
    )
    await runtime_delivery.send_long_message(
        runtime,
        chat_id=123,
        text="command complete",
        request_id="req-q6",
        purpose="command",
    )

    assert [message["disable_notification"] for message in runtime.app.bot.messages] == [
        True,
        True,
        False,
        False,
        False,
        False,
        False,
    ]


@pytest.mark.asyncio
async def test_quiet_mode_only_notifies_last_final_chunk(tmp_path):
    runtime = _runtime(tmp_path)
    runtime._notify_mode = "quiet"

    _elapsed, chunks = await runtime_delivery.send_long_message(
        runtime,
        chat_id=123,
        text=("line of final output\n" * 500),
        request_id="req-long-final",
        purpose="response",
    )

    assert chunks > 1
    notifications = [message["disable_notification"] for message in runtime.app.bot.messages]
    assert notifications[:-1] == [True] * (len(notifications) - 1)
    assert notifications[-1] is False


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
    assert message["disable_notification"] is True
    assert "parse_mode" not in message
    assert "Backend error (codex-cli) | req-err" in message["text"]
    assert "Full log (local):" in message["text"]
    assert "... (truncated) ..." in message["text"]


def test_format_backend_error_for_user_adds_upgrade_action_for_version_gated_model():
    raw = (
        '{"type":"error","status":400,"error":{"type":"invalid_request_error",'
        '"message":"The \'gpt-5.6-sol\' model requires a newer version of Codex. '
        'Please upgrade to the latest app or CLI and try again."}}'
    )

    text = runtime_delivery.format_backend_error_for_user("codex-cli", raw)

    assert "Exact backend failure: The 'gpt-5.6-sol' model requires a newer version of Codex." in text
    assert "Action: this model is not supported by the installed Codex." in text
    assert "Raw error:" in text


@pytest.mark.asyncio
async def test_send_long_message_formats_backend_failure_once(tmp_path):
    runtime = _runtime(tmp_path)
    raw = (
        '{"type":"error","status":400,"error":{"type":"invalid_request_error",'
        '"message":"The \'gpt-5.6-sol\' model requires a newer version of Codex. '
        'Please upgrade to the latest app or CLI and try again."}}'
    )

    _elapsed, chunks = await runtime_delivery.send_long_message(
        runtime,
        chat_id=123,
        text=raw,
        request_id="req-version",
        purpose="error",
    )

    assert chunks == 1
    message = runtime.app.bot.messages[0]["text"]
    assert message.count("Exact backend failure:") == 1
    assert "Action: this model is not supported by the installed Codex." in message
    assert "Raw error:" in message


@pytest.mark.asyncio
async def test_send_long_message_skips_retry_after_without_raising(tmp_path):
    runtime = _runtime(tmp_path, bot_error=RetryAfter(123))

    _elapsed, chunks = await runtime_delivery.send_long_message(
        runtime,
        chat_id=123,
        text="hello",
        request_id="req-flood",
    )

    assert chunks == 0
    assert runtime.app.bot.messages == []
    assert any("Telegram flood control" in message for _level, message in runtime.telegram_logger.messages)


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
