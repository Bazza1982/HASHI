import asyncio
import hashlib
import json
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from telegram import constants
from telegram.error import RetryAfter

from orchestrator import runtime_delivery
from orchestrator.telegram_delivery_state import telegram_bot_fingerprint


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
        config=SimpleNamespace(
            active_backend="codex-cli",
            telegram_token_key="test-agent",
            extra={"agent_lifecycle_id": "a" * 32},
        ),
        global_config=SimpleNamespace(project_root=tmp_path, instance_id="HASHI1"),
        logger=_Logger(),
        name="test-agent",
        token="token-test-agent",
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
async def test_telegram_adapter_persists_endpoint_receipt_for_canonical_event(
    tmp_path, monkeypatch
):
    from orchestrator.frontend_connector_registry import endpoint_id_for
    from orchestrator.session_store import SessionStore

    store = SessionStore(
        tmp_path / "state" / "sessions.sqlite3",
        instance_id="HASHI1",
    )
    owner = "user:7"
    session = store.ensure_default_session(owner_id=owner, agent_id="test-agent")
    message = store.append_presentation_message(
        session_id=session["session_id"],
        owner_id=owner,
        agent_id="test-agent",
        role="assistant",
        text="meter card",
        source="meter-cost",
        idempotency_key="meter:req-receipt",
        presentation_channel="meter",
    )
    runtime = _runtime(tmp_path)
    runtime.session_store = store
    audit_records = []
    runtime.canonical_audit = SimpleNamespace(
        record=lambda *args, **kwargs: audit_records.append((args, kwargs))
    )

    async def send_message(**kwargs):
        runtime.app.bot.messages.append(kwargs)
        return SimpleNamespace(message_id=9876)

    async def no_wait(*_args, **_kwargs):
        return None

    async def not_blocked(*_args, **_kwargs):
        return False

    runtime.app.bot.send_message = send_message
    monkeypatch.setattr(runtime_delivery.runtime_delivery_order, "wait_for_turn", no_wait)
    monkeypatch.setattr(
        runtime_delivery.telegram_delivery_failover,
        "handle_blocked_send",
        not_blocked,
    )

    _elapsed, chunks = await runtime_delivery.send_long_message(
        runtime,
        chat_id=99,
        text="meter card",
        request_id="req-meter",
        purpose="meter-cost",
        frontend_event_id=message["delivery_event_id"],
        frontend_session_id=session["session_id"],
        frontend_owner_id=owner,
    )

    receipts = store.frontend_delivery_receipts(
        session_id=session["session_id"],
        owner_id=owner,
        event_id=message["delivery_event_id"],
    )
    assert chunks == 1
    assert len(receipts) == 1
    assert receipts[0]["endpoint_id"] == endpoint_id_for(
        "telegram",
        ingress_transport="telegram",
        channel_key="99",
    )
    assert receipts[0]["status"] == "delivered"
    assert receipts[0]["proof"]["type"] == "telegram-api-accepted"
    assert receipts[0]["endpoint_id"] != "99"
    assert receipts[0]["proof"]["value"] != "99"
    assert "chat_id" not in receipts[0]
    audit_payload = audit_records[0][0][1]
    assert audit_payload["connector_id"] == "telegram"
    assert audit_payload["endpoint_id"] == receipts[0]["endpoint_id"]
    assert audit_payload["content_sha256"] == hashlib.sha256(
        b"meter card"
    ).hexdigest()
    assert "chat_id" not in audit_payload
    assert "text" not in audit_payload
    assert "meter card" not in str(audit_records)


@pytest.mark.asyncio
async def test_canonical_background_reply_uses_outbox_and_does_not_double_send(
    tmp_path, monkeypatch
):
    from orchestrator.frontend_delivery import freeze_run_delivery_route
    from orchestrator.session_store import SessionStore

    store = SessionStore(
        tmp_path / "state" / "sessions.sqlite3",
        instance_id="HASHI1",
    )
    owner = "user:7"
    session = store.ensure_default_session(owner_id=owner, agent_id="test-agent")
    route = freeze_run_delivery_route(
        message_source_id="telegram",
        session_surface="telegram",
        session_channel_key="7",
        chat_id=7,
        telegram_requested=False,
    )
    accepted = store.accept_run(
        session_id=session["session_id"],
        owner_id=owner,
        agent_id="test-agent",
        request_id="req-outbox-send",
        text="question",
        source="telegram",
        idempotency_key="outbox-send-key",
        delivery_route=route,
    )
    store.mark_request_running(accepted.request_id, worker_id="test-worker")
    store.finish_request(
        accepted.request_id,
        success=True,
        assistant_text="answer",
    )
    runtime = _runtime(tmp_path)
    runtime.session_store = store
    runtime.global_config.authorized_id = 7

    async def no_wait(*_args, **_kwargs):
        return None

    async def not_blocked(*_args, **_kwargs):
        return False

    monkeypatch.setattr(runtime_delivery.runtime_delivery_order, "wait_for_turn", no_wait)
    monkeypatch.setattr(
        runtime_delivery.telegram_delivery_failover,
        "handle_blocked_send",
        not_blocked,
    )

    first = await runtime_delivery.send_long_message(
        runtime,
        chat_id=7,
        text="answer",
        request_id=accepted.request_id,
        purpose="bg-response",
        frontend_outbox=True,
    )
    replay = await runtime_delivery.send_long_message(
        runtime,
        chat_id=7,
        text="answer",
        request_id=accepted.request_id,
        purpose="bg-response",
        frontend_outbox=True,
    )

    assert first[1] == 1
    assert replay == (0.0, 0)
    assert len(runtime.app.bot.messages) == 1
    event = next(
        item for item in store.events(session["session_id"], owner_id=owner)
        if item["kind"] == "run.completed"
    )
    receipt = store.frontend_delivery_receipts(
        session_id=session["session_id"],
        owner_id=owner,
        event_id=event["event_id"],
    )
    assert len(receipt) == 1
    assert receipt[0]["status"] == "delivered"
    with store._connection() as connection:
        state = connection.execute(
            "SELECT state FROM delivery_outbox WHERE event_id=?",
            (event["event_id"],),
        ).fetchone()[0]
    assert state == "completed"


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
    assert "Diagnostic log (local):" in message["text"]
    assert "search the log for HASHI request ID req-err" in message["text"]
    assert "/verbose off" not in message["text"]
    assert "... (truncated) ..." in message["text"]


def test_format_backend_error_for_user_adds_upgrade_action_for_version_gated_model():
    raw = (
        '{"type":"error","status":400,"error":{"type":"invalid_request_error",'
        '"message":"The \'gpt-5.6-sol\' model requires a newer version of Codex. '
        'Please upgrade to the latest app or CLI and try again."}}'
    )

    text = runtime_delivery.format_backend_error_for_user("codex-cli", raw)

    assert "Error details: The 'gpt-5.6-sol' model requires a newer version of Codex." in text
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
    assert message.count("Error details:") == 1
    assert "Action: this model is not supported by the installed Codex." in message
    assert "Raw error:" in message


@pytest.mark.asyncio
async def test_backend_error_exposes_actionable_typed_context_in_chinese(tmp_path):
    runtime = _runtime(tmp_path)
    runtime.global_config.ui_language = "zh-CN"

    _elapsed, chunks = await runtime_delivery.send_long_message(
        runtime,
        chat_id=123,
        text="[PROVIDER_BAD_REQUEST] The provider rejected the request as invalid.",
        request_id="req-sunny-0003",
        purpose="error",
        error_context={
            "error_code": "PROVIDER_BAD_REQUEST",
            "error_retryable": False,
            "http_status": 400,
            "provider_request_id": "provider-abc",
            "side_effects_possible": True,
        },
    )

    assert chunks == 1
    message = runtime.app.bot.messages[0]["text"]
    assert "错误详情：[PROVIDER_BAD_REQUEST]" in message
    assert "HTTP 状态：400" in message
    assert "服务提供方请求编号：provider-abc" in message
    assert "任务可能已部分执行" in message
    assert "原样重试通常无法解决问题" in message
    assert "诊断日志：" in message
    assert "HASHI 请求编号 req-sunny-0003" in message
    assert "准确错误" not in message
    assert "/verbose off" not in message


@pytest.mark.asyncio
async def test_send_long_message_skips_retry_after_without_raising(tmp_path):
    runtime = _runtime(tmp_path, bot_error=RetryAfter(timedelta(seconds=123)))

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
async def test_send_long_message_resumes_immediately_after_retry_wait(tmp_path):
    runtime = _runtime(tmp_path)
    state_path = tmp_path / "state" / "telegram_delivery_health.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps(
            {
                "version": 2,
                "agents": {
                    "test-agent": {
                        "owner": {
                            "instance_id": "HASHI1",
                            "agent_lifecycle_id": "a" * 32,
                            "telegram_bot_fingerprint": telegram_bot_fingerprint(
                                "token-test-agent"
                            ),
                        },
                        "token_key": "telegram:test-agent",
                        "status": "blocked",
                        "blocked_until": "2000-01-01T00:00:00+00:00",
                        "retry_after_s": 5,
                        "incident_id": "tg-test-agent-expired",
                        "per_chat": {},
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    _elapsed, chunks = await runtime_delivery.send_long_message(
        runtime,
        chat_id=123,
        text="normal business is back",
        request_id="req-after-wait",
        purpose="response",
    )

    assert chunks == 1
    assert runtime.app.bot.messages[0]["text"] == "normal business is back"
    record = json.loads(state_path.read_text(encoding="utf-8"))["agents"]["test-agent"]
    assert record["status"] == "healthy"
    assert not (tmp_path / "undelivered" / "req-after-wait.md").exists()


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
