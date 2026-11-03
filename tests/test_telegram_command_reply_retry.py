"""Command execution and durable delivery under Telegram connection failures."""
import sqlite3
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import httpx
import pytest
from telegram.error import NetworkError, TimedOut

from orchestrator import runtime_session
from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime
from orchestrator.session_store import SessionStore
from tests.test_frontend_command_admission import _Runtime, _execute, _update


def _transport_error(cause, wrapper=NetworkError):
    error = wrapper("transport unavailable")
    error.__cause__ = cause
    return error


@pytest.fixture
def reply_runtime(tmp_path, monkeypatch):
    import orchestrator.admin_local_testing as local_testing
    import orchestrator.flexible_agent_runtime as agent_runtime

    runtime = _Runtime(tmp_path)
    runtime.global_config.project_root = tmp_path
    runtime.app = NS(bot=None)
    runtime.telegram_logger = NS(warning=lambda *args, **kwargs: None)
    runtime.session_store = SessionStore(tmp_path / "sessions.sqlite3", instance_id="HASHI1")
    update = _update()
    update.effective_chat.id = 42
    update.message.reply_text = AsyncMock(return_value=NS(message_id=61))
    monkeypatch.setattr(local_testing, "local_command_capture", lambda *_args: None)
    monkeypatch.setattr(agent_runtime, "apply_disable_notification_default", lambda *_args: None)
    monkeypatch.setattr(
        agent_runtime.telegram_delivery_failover,
        "handle_blocked_send",
        AsyncMock(return_value=False),
    )
    monkeypatch.setattr(agent_runtime.asyncio, "sleep", AsyncMock())
    return runtime, update


def _persisted_delivery(runtime, update):
    # Reopen the real store: in-memory success must not hide a lost durable receipt.
    runtime.session_store = SessionStore(
        runtime.workspace_dir / "sessions.sqlite3", instance_id="HASHI1"
    )
    session = runtime_session.current_session_for_update(runtime, update)
    owner = runtime_session.owner_id(runtime)
    messages = runtime.session_store.messages(session["session_id"], owner_id=owner)
    receipts = runtime.session_store.frontend_delivery_receipts(
        session_id=session["session_id"], owner_id=owner
    )
    with sqlite3.connect(runtime.workspace_dir / "sessions.sqlite3") as connection:
        connection.row_factory = sqlite3.Row
        tasks = [dict(row) for row in connection.execute(
            "SELECT state, last_error_code FROM connector_delivery_tasks"
        )]
    return [message for message in messages if message["role"] == "assistant"], receipts, tasks


@pytest.mark.asyncio
@pytest.mark.parametrize("cause", [httpx.ConnectError("offline"), httpx.ConnectTimeout("offline")])
async def test_pre_submission_retry_delivers_once_without_reexecuting_command(reply_runtime, cause):
    runtime, update = reply_runtime
    sent = NS(message_id=61)
    update.message.reply_text.side_effect = [_transport_error(cause), sent]
    mutations = []

    async def handler():
        mutations.append("model changed")
        await FlexibleAgentRuntime._reply_text(runtime, update, "Model selected")

    first = await _execute(runtime, update, handler)
    replay = await _execute(runtime, update, handler)

    assert first["ok"] is True
    assert replay["replayed"] is True
    assert mutations == ["model changed"]
    assert update.message.reply_text.await_count == 2
    messages, receipts, tasks = _persisted_delivery(runtime, update)
    assert [message["text"] for message in messages] == ["Model selected"]
    assert len(receipts) == 1
    assert receipts[0]["status"] == "delivered"
    assert receipts[0]["proof"]["value"] == "61"

    assert tasks == [{"state": "completed", "last_error_code": None}]

@pytest.mark.asyncio
async def test_exhausted_connect_failure_is_bounded_and_known_not_submitted(reply_runtime):
    runtime, update = reply_runtime
    update.message.reply_text.side_effect = _transport_error(httpx.ConnectError("offline"))

    with pytest.raises(NetworkError):
        await FlexibleAgentRuntime._reply_text(runtime, update, "Status")

    assert update.message.reply_text.await_count == 3
    messages, receipts, tasks = _persisted_delivery(runtime, update)
    assert len(messages) == 1
    assert receipts == []
    assert tasks == [{"state": "failed", "last_error_code": "telegram_reply_not_submitted"}]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        _transport_error(httpx.ReadTimeout("outcome unknown"), TimedOut),
        _transport_error(httpx.WriteTimeout("outcome unknown"), TimedOut),
        NetworkError("httpx.ConnectError: do not classify copied text"),
    ],
)
async def test_ambiguous_transport_failure_is_never_retried(reply_runtime, error):
    runtime, update = reply_runtime
    update.message.reply_text.side_effect = error

    with pytest.raises(NetworkError):
        await FlexibleAgentRuntime._reply_text(runtime, update, "Status")

    assert update.message.reply_text.await_count == 1
    messages, receipts, tasks = _persisted_delivery(runtime, update)
    assert len(messages) == 1
    assert receipts == []
    assert tasks == [{"state": "unknown", "last_error_code": "telegram_reply_outcome_unknown"}]


@pytest.mark.asyncio
async def test_revoked_authorization_suppresses_pre_submission_retry(reply_runtime, monkeypatch):
    import orchestrator.telegram_actor_scope as actor_scope

    runtime, update = reply_runtime
    allowed = True

    async def disconnected(*_args, **_kwargs):
        nonlocal allowed
        allowed = False
        raise _transport_error(httpx.ConnectError("offline"))

    monkeypatch.setattr(actor_scope, "delivery_allowed", lambda *args, **kwargs: allowed)
    update.message.reply_text.side_effect = disconnected

    assert await FlexibleAgentRuntime._reply_text(runtime, update, "Private status") is None
    assert update.message.reply_text.await_count == 1
    messages, receipts, tasks = _persisted_delivery(runtime, update)
    assert len(messages) == 1
    assert receipts == []
    assert tasks == [{"state": "suppressed", "last_error_code": "telegram_access_revoked"}]


@pytest.mark.asyncio
async def test_real_ptb_httpx_adapter_recovers_before_submission(reply_runtime):
    from telegram import Bot, Message
    from telegram.request import HTTPXRequest

    runtime, update = reply_runtime
    attempts = 0

    def transport(request):
        nonlocal attempts
        if request.url.path.endswith("/getMe"):
            return httpx.Response(200, json={"ok": True, "result": {
                "id": 123, "is_bot": True, "first_name": "Test", "username": "test_bot",
            }})
        assert request.url.path.endswith("/sendMessage")
        attempts += 1
        if attempts == 1:
            raise httpx.ConnectError("offline", request=request)
        return httpx.Response(200, json={"ok": True, "result": {
            "message_id": 61, "date": 0, "chat": {"id": 42, "type": "private"},
            "text": "Status",
        }})

    request = HTTPXRequest(httpx_kwargs={"transport": httpx.MockTransport(transport)})
    async with Bot("123:synthetic", request=request, get_updates_request=request) as bot:
        update.message = update.effective_message = Message.de_json({
            "message_id": 9001, "date": 0, "chat": {"id": 42, "type": "private"},
            "text": "/status",
        }, bot)
        sent = await FlexibleAgentRuntime._reply_text(runtime, update, "Status")

    assert sent.message_id == 61
    assert attempts == 2
    messages, receipts, tasks = _persisted_delivery(runtime, update)
    assert [message["text"] for message in messages] == ["Status"]
    assert len(receipts) == 1 and receipts[0]["status"] == "delivered"
    assert tasks == [{"state": "completed", "last_error_code": None}]


@pytest.mark.asyncio
async def test_post_send_persistence_failure_never_resends(reply_runtime, monkeypatch):
    runtime, update = reply_runtime
    monkeypatch.setattr(
        runtime.session_store,
        "record_frontend_transport_reference",
        lambda **kwargs: (_ for _ in ()).throw(httpx.ConnectError("receipt store failure")),
    )

    with pytest.raises(httpx.ConnectError):
        await FlexibleAgentRuntime._reply_text(runtime, update, "Already sent")

    assert update.message.reply_text.await_count == 1
    _messages, receipts, tasks = _persisted_delivery(runtime, update)
    assert receipts == []
    assert tasks == [{"state": "unknown", "last_error_code": "telegram_reply_outcome_unknown"}]
