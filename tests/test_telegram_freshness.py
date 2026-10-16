"""Tests for Telegram ingress freshness gate and confirmation card lifecycle.

Functional owner: Frontend Connector / PAO
Engineering layer: Functions
Verification: Red-green proof for Telegram message freshness and confirmation cards.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest

from orchestrator.session_store import SessionStore
from orchestrator.telegram_freshness_gate import (
    check_telegram_message_freshness,
    handle_telegram_confirmation_callback,
    format_delay_display,
    restore_telegram_confirmation_state,
)


class _MockBot:
    def __init__(self):
        self.sent_messages = []
        self.edited_messages = []
        self.edit_should_fail = False

    async def send_message(self, *, chat_id, text, reply_markup=None, **kwargs):
        msg_id = len(self.sent_messages) + 1
        msg = {
            "message_id": msg_id,
            "chat_id": chat_id,
            "text": text,
            "reply_markup": reply_markup,
        }
        self.sent_messages.append(msg)
        return SimpleNamespace(message_id=msg_id, chat_id=chat_id, text=text)

    async def edit_message_text(self, *, chat_id, message_id, text, reply_markup=None, **kwargs):
        if self.edit_should_fail:
            raise RuntimeError("Telegram BadRequest: message is not modified or too old")
        self.edited_messages.append(
            {"chat_id": chat_id, "message_id": message_id, "text": text}
        )
        return True


class _MockCallbackQuery:
    def __init__(self, data: str, message, user_id=123):
        self.data = data
        self.message = message
        self.from_user = SimpleNamespace(id=user_id)
        self.answered = False

    async def answer(self, text=None, show_alert=False):
        self.answered = True


class _MockUpdate:
    def __init__(self, text: str, date: datetime, message_id: int = 101, chat_id: int = 123, user_id: int = 123):
        self.effective_chat = SimpleNamespace(id=chat_id)
        self.effective_user = SimpleNamespace(id=user_id)
        self.update_id = message_id + 1000
        self.message = SimpleNamespace(
            message_id=message_id,
            chat_id=chat_id,
            text=text,
            date=date,
            caption=None,
            photo=None,
            voice=None,
            document=None,
        )
        self.effective_message = self.message
        self.callback_query = None


class _MockRuntime:
    def __init__(self, tmp_path: Path):
        self.name = "kasumi"
        self.authorized_id = 123
        self.session_store = SessionStore(tmp_path / "sessions.sqlite", instance_id="HASHI3")
        self.bot = _MockBot()
        self.global_config = SimpleNamespace(
            ui_language="zh-CN", authorized_id=123, bridge_home=tmp_path
        )
        self.app = SimpleNamespace(bot=self.bot, process_update=self._process_update)
        self.enqueue_request_calls = []
        self.workspace_dir = tmp_path / "workspaces" / "kasumi"
        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        self._background_tasks = set()

    async def _process_update(self, update):
        intercepted = await check_telegram_message_freshness(
            self, update, handler_kind="text"
        )
        assert intercepted is False
        text = update.effective_message.text
        await self.enqueue_request(
            update.effective_chat.id, text, "telegram", text[:160]
        )

    def _is_authorized_user(self, user_id):
        return user_id == self.authorized_id

    async def enqueue_request(self, chat_id, text, source, summary=None, **kwargs):
        call = {"chat_id": chat_id, "text": text, "source": source, "kwargs": kwargs}
        self.enqueue_request_calls.append(call)
        return f"req-{len(self.enqueue_request_calls)}"


@pytest.fixture
def runtime(tmp_path: Path):
    return _MockRuntime(tmp_path)


def test_format_delay_display():
    assert format_delay_display(15.2) == "15 秒"
    assert format_delay_display(75.0) == "1 分 15 秒"
    assert format_delay_display(258.0) == "4 分 18 秒"


@pytest.mark.asyncio
async def test_telegram_fresh_message_not_intercepted(runtime):
    now = datetime.now(timezone.utc)
    # 5 seconds old -> fresh for telegram (30s)
    update = _MockUpdate("Fresh message", now - timedelta(seconds=5))
    intercepted = await check_telegram_message_freshness(runtime, update, handler_kind="text")
    assert intercepted is False
    assert len(runtime.bot.sent_messages) == 0


@pytest.mark.asyncio
async def test_telegram_stale_text_message_intercepted_with_card(runtime):
    now = datetime.now(timezone.utc)
    # 40 seconds old -> stale for telegram (threshold 30s)
    update = _MockUpdate("Stale text message", now - timedelta(seconds=40), message_id=201)
    intercepted = await check_telegram_message_freshness(runtime, update, handler_kind="text")
    assert intercepted is True
    # Verify card was sent
    assert len(runtime.bot.sent_messages) == 1
    sent = runtime.bot.sent_messages[0]
    assert "消息已暂停" in sent["text"]
    assert "Stale text message" in sent["text"]
    assert sent["reply_markup"] is not None
    # Check inline keyboard has confirm and discard buttons
    keyboard = sent["reply_markup"].inline_keyboard
    assert len(keyboard) == 1
    buttons = keyboard[0]
    assert buttons[0].text == "仍然执行"
    assert buttons[0].callback_data.startswith("ingress_confirm:")
    assert buttons[0].callback_data.endswith(":confirm")
    assert buttons[1].text == "丢弃"
    assert buttons[1].callback_data.endswith(":discard")


@pytest.mark.asyncio
async def test_telegram_stale_command_intercepted(runtime):
    now = datetime.now(timezone.utc)
    update = _MockUpdate("/say hello", now - timedelta(seconds=35), message_id=202)
    intercepted = await check_telegram_message_freshness(
        runtime, update, handler_kind="command", command_name="say", args=["hello"]
    )
    assert intercepted is True
    assert len(runtime.bot.sent_messages) == 1
    assert "/say hello" in runtime.bot.sent_messages[0]["text"]


@pytest.mark.asyncio
async def test_telegram_stale_media_stops_before_download_handler(
    runtime, monkeypatch
):
    from orchestrator import runtime_media
    from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime

    update = _MockUpdate(
        "", datetime.now(timezone.utc) - timedelta(seconds=40), message_id=206
    )
    update.message.caption = "delayed photo"
    downstream = AsyncMock()
    monkeypatch.setattr(runtime_media, "handle_media_message", downstream)

    await FlexibleAgentRuntime._handle_media_message(
        runtime,
        update,
        "Photo",
        "photo.jpg",
        "telegram-file-id",
        "inspect {local_path}",
        "photo",
    )

    downstream.assert_not_awaited()
    assert len(runtime.bot.sent_messages) == 1
    assert runtime.enqueue_request_calls == []


@pytest.mark.asyncio
async def test_telegram_stale_voice_stops_before_download_or_transcription(
    runtime, monkeypatch
):
    from orchestrator import runtime_media
    from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime

    update = _MockUpdate(
        "", datetime.now(timezone.utc) - timedelta(seconds=40), message_id=207
    )
    downstream = AsyncMock()
    monkeypatch.setattr(runtime_media, "handle_voice_or_audio", downstream)

    await FlexibleAgentRuntime._handle_voice_or_audio(
        runtime,
        update,
        "Voice",
        "voice.ogg",
        "telegram-voice-id",
    )

    downstream.assert_not_awaited()
    assert len(runtime.bot.sent_messages) == 1
    assert runtime.enqueue_request_calls == []


@pytest.mark.asyncio
async def test_telegram_replayed_update_reuses_one_confirmation_card(runtime):
    update = _MockUpdate(
        "same delayed update",
        datetime.now(timezone.utc) - timedelta(seconds=45),
        message_id=208,
    )

    assert await check_telegram_message_freshness(runtime, update) is True
    assert await check_telegram_message_freshness(runtime, update) is True

    assert len(runtime.bot.sent_messages) == 1
    records = runtime.session_store.resolve_primary_session(
        owner_id="user:123", agent_id="kasumi", establish=False
    )
    assert records["agent_id"] == "kasumi"


@pytest.mark.asyncio
async def test_telegram_restart_projects_visible_expiry(runtime, tmp_path):
    update = _MockUpdate(
        "expires across restart",
        datetime.now(timezone.utc) - timedelta(seconds=45),
        message_id=209,
    )
    await check_telegram_message_freshness(runtime, update)
    with runtime.session_store._lock, runtime.session_store._connection() as connection:
        connection.execute(
            "UPDATE ingress_confirmations SET expires_at = ?",
            ("2000-01-01T00:00:00Z",),
        )

    restarted = _MockRuntime(tmp_path)
    await restore_telegram_confirmation_state(restarted)

    assert len(restarted.bot.edited_messages) == 1
    assert "已失效" in restarted.bot.edited_messages[0]["text"]


@pytest.mark.asyncio
async def test_telegram_callback_confirm_executes_once(runtime):
    now = datetime.now(timezone.utc)
    update = _MockUpdate("Execute this delayed message", now - timedelta(seconds=50), message_id=203)
    await check_telegram_message_freshness(runtime, update, handler_kind="text")
    card_msg = runtime.bot.sent_messages[0]
    conf_id = card_msg["reply_markup"].inline_keyboard[0][0].callback_data.split(":")[1]

    cb_query = _MockCallbackQuery(
        data=f"ingress_confirm:{conf_id}:confirm",
        message=SimpleNamespace(message_id=card_msg["message_id"], chat_id=123),
    )
    cb_update = SimpleNamespace(callback_query=cb_query, effective_user=SimpleNamespace(id=123))

    handled = await handle_telegram_confirmation_callback(runtime, cb_update)
    assert handled is True
    assert cb_query.answered is True
    # Enqueued exactly once!
    assert len(runtime.enqueue_request_calls) == 1
    assert runtime.enqueue_request_calls[0]["text"] == "Execute this delayed message"
    # Card edited to confirmed terminal state
    assert len(runtime.bot.edited_messages) == 1
    assert "已确认执行" in runtime.bot.edited_messages[0]["text"]

    # Repeated callback click does not create a second Run
    await handle_telegram_confirmation_callback(runtime, cb_update)
    assert len(runtime.enqueue_request_calls) == 1


@pytest.mark.asyncio
async def test_telegram_callback_discard_never_executes(runtime):
    now = datetime.now(timezone.utc)
    update = _MockUpdate("Discard this message", now - timedelta(seconds=45), message_id=204)
    await check_telegram_message_freshness(runtime, update, handler_kind="text")
    card_msg = runtime.bot.sent_messages[0]
    conf_id = card_msg["reply_markup"].inline_keyboard[0][1].callback_data.split(":")[1]

    cb_query = _MockCallbackQuery(
        data=f"ingress_confirm:{conf_id}:discard",
        message=SimpleNamespace(message_id=card_msg["message_id"], chat_id=123),
    )
    cb_update = SimpleNamespace(callback_query=cb_query, effective_user=SimpleNamespace(id=123))

    handled = await handle_telegram_confirmation_callback(runtime, cb_update)
    assert handled is True
    assert cb_query.answered is True
    # Never enqueued!
    assert len(runtime.enqueue_request_calls) == 0
    # Card edited to discarded
    assert len(runtime.bot.edited_messages) == 1
    assert "已丢弃" in runtime.bot.edited_messages[0]["text"]


@pytest.mark.asyncio
async def test_telegram_edit_failure_falls_back_to_new_message(runtime):
    now = datetime.now(timezone.utc)
    update = _MockUpdate("Message with edit failure", now - timedelta(seconds=45), message_id=205)
    await check_telegram_message_freshness(runtime, update, handler_kind="text")
    conf_id = runtime.bot.sent_messages[0]["reply_markup"].inline_keyboard[0][1].callback_data.split(":")[1]

    # Configure edit failure
    runtime.bot.edit_should_fail = True
    cb_query = _MockCallbackQuery(
        data=f"ingress_confirm:{conf_id}:discard",
        message=SimpleNamespace(message_id=runtime.bot.sent_messages[0]["message_id"], chat_id=123),
    )
    cb_update = SimpleNamespace(callback_query=cb_query, effective_user=SimpleNamespace(id=123))

    handled = await handle_telegram_confirmation_callback(runtime, cb_update)
    assert handled is True
    # Sent fallback message since edit failed
    assert len(runtime.bot.sent_messages) == 2
    assert "已丢弃" in runtime.bot.sent_messages[1]["text"]
