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


def _capacity_item(store, owner, session, accepted, *, silent=False):
    run = store.get_run(accepted.run_id, owner_id=owner)
    return SimpleNamespace(
        request_id=accepted.request_id, run_id=accepted.run_id,
        session_id=session["session_id"], owner_id=owner,
        context_generation=session["context_generation"],
        fencing_token=run["fencing_token"], silent=silent,
    )


@pytest.mark.asyncio
async def test_capacity_notice_survives_full_verbose_queue_and_reaches_frozen_route(
    tmp_path, monkeypatch,
):
    import asyncio
    from adapters.stream_events import KIND_PROGRESS, StreamEvent
    from orchestrator import telegram_delivery_failover
    from orchestrator.frontend_notice_delivery import publish_run_capacity_notice
    from orchestrator.frontend_projection import poll_frontend_feed
    from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime
    from orchestrator.runtime_capacity_recovery import _emit

    monkeypatch.setattr(telegram_delivery_failover, "handle_blocked_send", AsyncMock(return_value=False))
    route, store, owner, session, accepted, runtime = setup_delivery(tmp_path)
    item = _capacity_item(store, owner, session, accepted)
    runtime._verbose = True
    runtime.logger.debug = lambda *_args: None
    queue = asyncio.Queue(maxsize=1)
    queue.put_nowait(StreamEvent(kind=KIND_PROGRESS, summary="ordinary telemetry"))
    callback = FlexibleAgentRuntime._make_stream_callback(runtime, event_queue=queue)
    publications = []

    async def publish(event):
        publications.append(event)
        return await publish_run_capacity_notice(runtime, item, event)

    callback.publish_capacity_notice = publish
    runtime.telegram_connected = False
    await _emit(callback, "fallback.retry.wait", attempt=1, count=1, seconds=5)
    assert queue.qsize() == 1
    assert getattr(queue, "_hashi_omitted", 0) == 0
    assert len(publications) == 1 and publications[0].event_id
    # Duplicate publication of the same event does not create another message.
    await publish(publications[0])
    feed = poll_frontend_feed(store, session["session_id"], owner_id=owner, run_id=accepted.run_id)
    notices = [event for event in feed["durable_events"] if event["semantic_kind"] == "notification"]
    assert len(notices) == 1
    assert notices[0]["run_id"] == accepted.run_id
    assert notices[0]["content_blocks"][0]["text"] == publications[0].summary
    assert await dispatch_pending_telegram_deliverables(runtime) == 0
    runtime.telegram_connected = True
    assert await dispatch_pending_telegram_deliverables(runtime) == 1
    assert await dispatch_pending_telegram_deliverables(runtime) == 0
    assert len(runtime.app.bot.messages) == 1
    assert runtime.app.bot.messages[0]["chat_id"] == 7
    store.finish_request(accepted.request_id, success=True, assistant_text="Final result")
    completed = poll_frontend_feed(
        store, session["session_id"], owner_id=owner, run_id=accepted.run_id,
    )
    assert any(event["semantic_kind"] == "final"
               and event["content_blocks"][0]["text"] == "Final result"
               for event in completed["durable_events"])
    assert queue.qsize() == 1


@pytest.mark.asyncio
async def test_capacity_notice_cannot_be_published_or_replayed_after_stop(tmp_path, monkeypatch):
    from adapters.stream_events import KIND_PROGRESS, StreamEvent
    from orchestrator import telegram_delivery_failover
    from orchestrator.frontend_notice_delivery import publish_run_capacity_notice
    from orchestrator.session_store import StaleFencingToken

    monkeypatch.setattr(telegram_delivery_failover, "handle_blocked_send", AsyncMock(return_value=False))
    route, store, owner, session, accepted, runtime = setup_delivery(tmp_path)
    item = _capacity_item(store, owner, session, accepted)
    notice = StreamEvent(kind=KIND_PROGRESS, summary="Retrying", event_id="capacity-1")
    result = await publish_run_capacity_notice(runtime, item, notice)
    store.cancel_run(accepted.run_id, owner_id=owner)
    with pytest.raises(StaleFencingToken):
        await publish_run_capacity_notice(runtime, item, notice)
    await dispatch_pending_telegram_deliverables(runtime)
    with store._connection() as db:
        task = db.execute(
            "SELECT state FROM connector_delivery_tasks WHERE event_id=? AND connector_id='telegram'",
            (result["delivery_event_id"],),
        ).fetchone()
    assert task["state"] == "suppressed"
    assert runtime.app.bot.messages == []


@pytest.mark.asyncio
async def test_capacity_notice_does_not_escape_silent_or_wrong_run(tmp_path):
    from adapters.stream_events import KIND_PROGRESS, StreamEvent
    from orchestrator.frontend_notice_delivery import publish_run_capacity_notice
    from orchestrator.session_store import StaleFencingToken

    route, store, owner, session, accepted, runtime = setup_delivery(tmp_path)
    notice = StreamEvent(kind=KIND_PROGRESS, summary="Retrying", event_id="capacity-1")
    item = _capacity_item(store, owner, session, accepted, silent=True)
    assert await publish_run_capacity_notice(runtime, item, notice) is False
    item.silent = False
    item.owner_id = "another-owner"
    with pytest.raises(StaleFencingToken):
        await publish_run_capacity_notice(runtime, item, notice)
    assert not [e for e in store.events(session["session_id"], owner_id=owner)
                if e["kind"] == "frontend.message.recorded"]




@pytest.mark.asyncio
async def test_capacity_notice_unknown_telegram_outcome_is_not_replayed(tmp_path, monkeypatch):
    from adapters.stream_events import KIND_PROGRESS, StreamEvent
    from orchestrator import telegram_delivery_failover
    from orchestrator.frontend_notice_delivery import publish_run_capacity_notice

    monkeypatch.setattr(telegram_delivery_failover, "handle_blocked_send", AsyncMock(return_value=False))
    route, store, owner, session, accepted, runtime = setup_delivery(tmp_path)
    item = _capacity_item(store, owner, session, accepted)
    notice = StreamEvent(kind=KIND_PROGRESS, summary="Retrying", event_id="capacity-unknown")
    result = await publish_run_capacity_notice(runtime, item, notice)
    original = runtime.app.bot.send_message

    async def uncertain(**kwargs):
        try:
            raise httpx.ReadTimeout("fixture: transport outcome unknown")
        except httpx.ReadTimeout as cause:
            raise NetworkError("wrapped") from cause

    runtime.app.bot.send_message = uncertain
    assert await dispatch_pending_telegram_deliverables(runtime) == 1
    runtime.app.bot.send_message = original
    assert await dispatch_pending_telegram_deliverables(runtime) == 0
    with store._connection() as db:
        row = db.execute(
            "SELECT state FROM connector_delivery_tasks WHERE event_id=? AND connector_id='telegram'",
            (result["delivery_event_id"],),
        ).fetchone()
    assert row["state"] == "unknown"
    assert runtime.app.bot.messages == []


@pytest.mark.asyncio
async def test_capacity_notice_waiting_for_database_cannot_outlive_stop(tmp_path, monkeypatch):
    import asyncio
    from threading import Event
    from adapters.stream_events import KIND_PROGRESS, StreamEvent
    from orchestrator.frontend_notice_delivery import publish_run_capacity_notice
    from orchestrator.session_store import StaleFencingToken

    route, store, owner, session, accepted, runtime = setup_delivery(tmp_path)
    item = _capacity_item(store, owner, session, accepted)
    notice = StreamEvent(kind=KIND_PROGRESS, summary="Retrying", event_id="capacity-wait")
    started = Event()
    original_guard = store.hold_active_run

    def observed_guard(**binding):
        started.set()
        return original_guard(**binding)

    monkeypatch.setattr(store, "hold_active_run", observed_guard)
    with store._lock:
        task = asyncio.create_task(publish_run_capacity_notice(runtime, item, notice))
        for _ in range(200):
            if started.is_set():
                break
            await asyncio.sleep(.005)
        assert started.is_set()
        store.cancel_run(accepted.run_id, owner_id=owner)
    with pytest.raises(StaleFencingToken):
        await task
    assert not [e for e in store.events(session["session_id"], owner_id=owner)
                if e["kind"] == "frontend.message.recorded"]




@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["projection", "decision"])
async def test_capacity_notice_preflight_database_error_retries_without_transport_effect(tmp_path, monkeypatch, stage):
    import sqlite3
    from adapters.stream_events import KIND_PROGRESS, StreamEvent
    from orchestrator import frontend_dispatch, telegram_delivery_failover
    from orchestrator.frontend_notice_delivery import publish_run_capacity_notice

    monkeypatch.setattr(telegram_delivery_failover, "handle_blocked_send", AsyncMock(return_value=False))
    route, store, owner, session, accepted, runtime = setup_delivery(tmp_path)
    item = _capacity_item(store, owner, session, accepted)
    notice = StreamEvent(kind=KIND_PROGRESS, summary="Retrying", event_id="capacity-busy")
    result = await publish_run_capacity_notice(runtime, item, notice)
    target, name = ((store, "get_run") if stage == "decision"
                    else (frontend_dispatch, "project_claimed_frontend_event"))
    original = getattr(target, name)
    failures = []

    def one_busy_read(*args, **kwargs):
        if not failures:
            failures.append(True)
            error = sqlite3.OperationalError("database is locked")
            error.sqlite_errorcode = sqlite3.SQLITE_BUSY
            raise error
        return original(*args, **kwargs)

    monkeypatch.setattr(target, name, one_busy_read)
    assert await dispatch_pending_telegram_deliverables(runtime) == 1
    with store._connection() as db:
        row = db.execute(
            "SELECT state FROM connector_delivery_tasks WHERE event_id=? AND connector_id='telegram'",
            (result["delivery_event_id"],),
        ).fetchone()
    assert row["state"] == "retry"
    assert runtime.app.bot.messages == []
    assert await dispatch_pending_telegram_deliverables(runtime) == 1
    assert await dispatch_pending_telegram_deliverables(runtime) == 0
    assert len(runtime.app.bot.messages) == 1




@pytest.mark.asyncio
async def test_run_notice_transcript_identity_never_collides_with_another_notice_or_final(tmp_path):
    from adapters.stream_events import KIND_PROGRESS, StreamEvent
    from orchestrator.chat_transcript_projection import _canonical_projection_row
    from orchestrator.frontend_notice_delivery import publish_run_capacity_notice

    route, store, owner, session, accepted, runtime = setup_delivery(tmp_path)
    item = _capacity_item(store, owner, session, accepted)
    first = await publish_run_capacity_notice(
        runtime, item, StreamEvent(kind=KIND_PROGRESS, summary="Retrying", event_id="notice-1"))
    second = await publish_run_capacity_notice(
        runtime, item, StreamEvent(kind=KIND_PROGRESS, summary="Switching", event_id="notice-2"))
    store.finish_request(accepted.request_id, success=True, assistant_text="Finished")
    rows = store.visible_messages_after(session["session_id"], owner_id=owner)
    projected = [_canonical_projection_row(store, row, owner_id=owner) for row in rows]
    refs = {row["text"]: row["message_ref"] for row in projected}
    assert refs["Retrying"] == "message:" + first["message_id"]
    assert refs["Switching"] == "message:" + second["message_id"]
    assert refs["Finished"] == "run:" + accepted.run_id + ":assistant"
    assert len({refs["Retrying"], refs["Switching"], refs["Finished"]}) == 3


# HASHI-Fingerprint: hfp_2e14767b5cab44cba223ac3c35afc3d0
