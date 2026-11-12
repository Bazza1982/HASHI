from types import SimpleNamespace
from unittest.mock import AsyncMock
import httpx
import pytest
from telegram.error import NetworkError
from orchestrator.telegram_transport import bot_api_kwargs
from orchestrator.frontend_delivery import freeze_run_delivery_route
from orchestrator.frontend_incremental_delivery import dispatch_pending_telegram_deliverables
from tests.test_frontend_attachment_tool import _running_session, _ToolRuntime


@pytest.mark.parametrize("url",[
    "http://example.com", "https://u:p@example.com", "https://example.com/bot",
    "https://example.com?token=private", "file:///tmp/api",
])
def test_unsafe_bot_api_endpoint_is_rejected(url):
    with pytest.raises(ValueError):
        bot_api_kwargs(SimpleNamespace(extra={"telegram_api_url":url}))


def test_official_and_loopback_bot_api_endpoints():
    assert bot_api_kwargs(SimpleNamespace(extra={}))=={}
    assert bot_api_kwargs(SimpleNamespace(extra={"telegram_api_url":"http://127.0.0.1:18990/"}))=={
        "base_url":"http://127.0.0.1:18990/bot","base_file_url":"http://127.0.0.1:18990/file/bot"}


def setup_delivery(tmp_path):
    route=freeze_run_delivery_route(message_source_id="session_api",session_surface="workbench",
        session_channel_key="default",chat_id=7,telegram_requested=True)
    store,owner,session,accepted=_running_session(tmp_path,delivery_route=route)
    runtime=_ToolRuntime(tmp_path,store)
    return route,store,owner,session,accepted,runtime


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_type", [httpx.ConnectError,httpx.ReadTimeout])
async def test_only_proved_prewrite_failures_retry_the_same_event(tmp_path,monkeypatch,failure_type):
    from orchestrator import telegram_delivery_failover
    monkeypatch.setattr(telegram_delivery_failover,"handle_blocked_send",AsyncMock(return_value=False))
    route,store,owner,session,accepted,runtime=setup_delivery(tmp_path)
    store.finish_request(accepted.request_id,success=True,assistant_text="Business result")
    original=runtime.app.bot.send_message
    reconnect=[]
    runtime._telegram_reconnect=lambda:reconnect.append(True)
    async def fail(**kwargs):
        try:
            raise failure_type("typed transport evidence")
        except failure_type as cause:
            raise NetworkError("wrapped") from cause
    runtime.app.bot.send_message=fail
    assert await dispatch_pending_telegram_deliverables(runtime)==1
    with store._connection() as db:
        states={row[0] for row in db.execute(
            "SELECT t.state FROM connector_delivery_tasks t JOIN run_events e ON e.event_id=t.event_id "
            "WHERE t.connector_id='telegram' AND e.kind='run.completed'")}
    if failure_type is httpx.ConnectError:
        assert states=={"retry"} and reconnect==[True] and not runtime.telegram_connected
        runtime.app.bot.send_message=original
        runtime.telegram_connected=True
        assert await dispatch_pending_telegram_deliverables(runtime)==1
        assert await dispatch_pending_telegram_deliverables(runtime)==0
        assert len(runtime.app.bot.messages)==1
    else:
        assert states=={"unknown"} and reconnect==[]
        runtime.app.bot.send_message=original
        assert await dispatch_pending_telegram_deliverables(runtime)==0
        assert runtime.app.bot.messages==[]
    assert store.get_run_by_request(accepted.request_id,owner_id=owner)["state"]=="completed"


@pytest.mark.asyncio
async def test_offline_lifecycle_notice_retains_its_frozen_destination(tmp_path,monkeypatch):
    from orchestrator import telegram_delivery_failover
    monkeypatch.setattr(telegram_delivery_failover,"handle_blocked_send",AsyncMock(return_value=False))
    route,store,owner,session,accepted,runtime=setup_delivery(tmp_path)
    store.append_presentation_message(session_id=session["session_id"],owner_id=owner,agent_id="agent1",
        role="assistant",text="Local recovery confirmed",source="frontend.runtime_notice",
        idempotency_key="offline-notice",history_eligible=False,
        message_context={"delivery_recovery":{"category":"command_result"}},
        outbox=True,delivery_route=route)
    runtime.telegram_connected=False
    assert await dispatch_pending_telegram_deliverables(runtime)==0
    runtime.telegram_connected=True
    assert await dispatch_pending_telegram_deliverables(runtime)==1
    assert await dispatch_pending_telegram_deliverables(runtime)==0
    assert len(runtime.app.bot.messages)==1
# HASHI-Fingerprint: hfp_2e14767b5cab44cba223ac3c35afc3d0
