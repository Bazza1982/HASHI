from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from orchestrator import runtime_delivery
from orchestrator.config_json import read_config_json, write_config_json
from orchestrator.telegram_actor_scope import _delegated_owner, runtime_snapshot
from tests.test_telegram_actor_scope import instance


@pytest.mark.asyncio
async def test_each_text_chunk_checks_the_current_grant(tmp_path, monkeypatch):
    runtime, _observations, config_path = instance(tmp_path)
    owner = _delegated_owner(runtime_snapshot(runtime), 100)
    sent = []

    async def send_message(**kwargs):
        sent.append(kwargs)
        config = read_config_json(config_path)
        config["global"]["authorized_telegram_user_ids"] = []
        write_config_json(config_path, config)
        return SimpleNamespace(message_id=1)

    runtime.app.bot = SimpleNamespace(send_message=send_message)
    runtime.telegram_connected = True
    runtime.logger = logging.getLogger("test")
    runtime.telegram_logger = runtime.logger
    runtime.error_logger = runtime.logger
    runtime.workspace_dir = tmp_path
    runtime.config = SimpleNamespace(extra={})
    monkeypatch.setattr(runtime_delivery.runtime_delivery_order, "wait_for_turn", AsyncMock())
    monkeypatch.setattr(runtime_delivery.telegram_delivery_failover, "handle_blocked_send", AsyncMock(return_value=False))
    outcome = {}
    _, count = await runtime_delivery._send_long_message_transport(
        runtime, chat_id=100, text="a" * 9000, frontend_owner_id=owner,
        skip_local_capture=True, outbox_outcome=outcome,
    )
    assert len(sent) == 1
    assert count == 1
    assert outcome["status"] == "unknown"

@pytest.mark.asyncio
async def test_media_stops_between_files_when_access_is_revoked(tmp_path):
    runtime, _observations, config_path = instance(tmp_path)
    owner = _delegated_owner(runtime_snapshot(runtime), 100)
    file_path = tmp_path / "image.png"
    file_path.write_bytes(b"image")
    sent = []

    async def send_photo(**kwargs):
        sent.append(kwargs["chat_id"])
        config = read_config_json(config_path)
        config["global"]["authorized_telegram_user_ids"] = []
        write_config_json(config_path, config)
        return SimpleNamespace(message_id=12)

    runtime.app.bot = SimpleNamespace(send_photo=send_photo)
    runtime.config = SimpleNamespace(extra={})
    runtime.telegram_logger = logging.getLogger("test")
    store = SimpleNamespace(visible_message_attachment=lambda *args, **kwargs: {
        "local_ref": str(file_path), "mime_type": "image/png"
    })
    event = {"content_blocks": [
        {"type": "media_ref", "attachment_id": "one", "role": "photo"},
        {"type": "media_ref", "attachment_id": "two", "role": "photo"},
    ]}
    with pytest.raises(runtime_delivery.TelegramAccessRevoked):
        await runtime_delivery._send_standard_event_media_to_telegram(
            runtime, chat_id=100, store=store,
            claim={"session_id": "s", "detail": {"message_id": "m"}},
            owner_id=owner, event=event, request_id="r", purpose="response",
            delivery_mode="final_delivery", outbox_outcome={},
        )
    assert sent == [100]

@pytest.mark.asyncio
async def test_media_revoked_while_waiting_is_settled_without_retry(tmp_path, monkeypatch):
    import orchestrator.frontend_connector_registry as connector
    import orchestrator.frontend_dispatch as dispatch
    import orchestrator.frontend_run_questions as questions

    runtime, _observations, config_path = instance(tmp_path)
    owner = _delegated_owner(runtime_snapshot(runtime), 100)
    file_path = tmp_path / "image.png"
    file_path.write_bytes(b"image")
    runtime.telegram_connected = True
    runtime.telegram_logger = logging.getLogger("test")
    runtime.config = SimpleNamespace(extra={})
    runtime.app.bot = SimpleNamespace(send_photo=AsyncMock())
    completions, receipts = [], []
    store = SimpleNamespace(
        visible_message_attachment=lambda *args, **kwargs: {"local_ref": str(file_path)},
        complete_delivery_outbox=lambda **entry: completions.append(entry),
    )
    event = {"content_blocks": [
        {"type": "media_ref", "attachment_id": "one", "role": "photo"},
    ]}
    async def revoke(*args):
        config = read_config_json(config_path)
        config["global"]["authorized_telegram_user_ids"] = []
        write_config_json(config_path, config)
    monkeypatch.setattr(runtime_delivery.runtime_delivery_order, "wait_for_turn", revoke)
    monkeypatch.setattr(runtime_delivery.telegram_delivery_failover, "handle_blocked_send", AsyncMock(return_value=False))
    monkeypatch.setattr(connector, "require_connector_event", lambda *args: None)
    monkeypatch.setattr(dispatch, "project_claimed_frontend_event", lambda *args, **kw: event)
    monkeypatch.setattr(questions, "question_identity", lambda event: None)
    monkeypatch.setattr(runtime_delivery, "_render_standard_event_for_telegram", lambda event: ("", None))
    monkeypatch.setattr(runtime_delivery, "telegram_reply_markup_for_event", lambda *args: None)
    monkeypatch.setattr(runtime_delivery, "_record_standard_telegram_receipt", lambda *args, **kw: receipts.append(kw))
    claim = {"outbox_id": "ob", "lease_token": "lease", "session_id": "s",
             "event_id": "event", "endpoint_id": "ep", "detail": {"message_id": "m"}}
    result = await runtime_delivery.dispatch_claimed_telegram_event(
        runtime, chat_id=100, store=store, claim=claim, frontend_owner_id=owner,
        request_id="r", purpose="response",
    )
    assert result[1] == 0
    assert runtime.app.bot.send_photo.await_count == 0
    assert completions[0]["status"] == "suppressed"
    assert receipts[0]["status"] == "skipped"

@pytest.mark.asyncio
async def test_text_and_media_revoked_before_sending_are_suppressed(tmp_path, monkeypatch):
    import orchestrator.frontend_connector_registry as connector
    import orchestrator.frontend_dispatch as dispatch
    import orchestrator.frontend_run_questions as questions

    runtime, _observations, config_path = instance(tmp_path)
    owner = _delegated_owner(runtime_snapshot(runtime), 100)
    runtime.telegram_connected = True
    runtime.logger = logging.getLogger("test")
    runtime.telegram_logger = runtime.logger
    runtime.error_logger = runtime.logger
    runtime.workspace_dir = tmp_path
    runtime.config = SimpleNamespace(extra={})
    runtime.app.bot = SimpleNamespace(send_message=AsyncMock())
    completions, receipts = [], []
    store = SimpleNamespace(complete_delivery_outbox=lambda **entry: completions.append(entry))
    event = {"content_blocks": [{"type": "media_ref", "attachment_id": "one"}]}

    async def revoke(*args):
        config = read_config_json(config_path)
        config["global"]["authorized_telegram_user_ids"] = []
        write_config_json(config_path, config)
    monkeypatch.setattr(runtime_delivery.runtime_delivery_order, "wait_for_turn", revoke)
    monkeypatch.setattr(runtime_delivery.telegram_delivery_failover, "handle_blocked_send", AsyncMock(return_value=False))
    monkeypatch.setattr(connector, "require_connector_event", lambda *args: None)
    monkeypatch.setattr(dispatch, "project_claimed_frontend_event", lambda *args, **kw: event)
    monkeypatch.setattr(questions, "question_identity", lambda event: None)
    monkeypatch.setattr(runtime_delivery, "_render_standard_event_for_telegram", lambda event: ("hello", None))
    monkeypatch.setattr(runtime_delivery, "telegram_reply_markup_for_event", lambda *args: None)
    monkeypatch.setattr(runtime_delivery, "_record_standard_telegram_receipt", lambda *args, **kw: receipts.append(kw))
    claim = {"outbox_id": "ob", "lease_token": "lease", "session_id": "s",
             "event_id": "event", "endpoint_id": "ep", "detail": {"message_id": "m"}}
    await runtime_delivery.dispatch_claimed_telegram_event(
        runtime, chat_id=100, store=store, claim=claim, frontend_owner_id=owner,
        request_id="r", purpose="response",
    )
    assert runtime.app.bot.send_message.await_count == 0
    assert completions[0]["status"] == "suppressed"
    assert receipts[0]["status"] == "skipped"

@pytest.mark.asyncio
async def test_live_preview_stops_after_access_revocation(tmp_path, monkeypatch):
    import asyncio
    from adapters.stream_events import KIND_TEXT_DELTA, StreamEvent
    from orchestrator import runtime_pipeline

    runtime, _observations, config_path = instance(tmp_path)
    owner = _delegated_owner(runtime_snapshot(runtime), 100)
    sent = []

    async def edit_message_text(**kwargs):
        sent.append(kwargs)
        config = read_config_json(config_path)
        config["global"]["authorized_telegram_user_ids"] = []
        write_config_json(config_path, config)

    runtime.app.bot = SimpleNamespace(edit_message_text=edit_message_text)
    runtime.config = SimpleNamespace(extra={"answer_stream_min_chars": 1})
    runtime.telegram_logger = logging.getLogger("test")
    policy = SimpleNamespace(edit_interval_s=0.01, heartbeat_interval_s=999,
                             max_edits_per_request=100, progress_enabled=False)
    monkeypatch.setattr(runtime_pipeline.telegram_stream_policy, "get_policy", lambda runtime: policy)
    monkeypatch.setattr(runtime_pipeline.telegram_delivery_failover, "is_delivery_blocked", lambda runtime: False)
    item = SimpleNamespace(chat_id=100, request_id="r", owner_id=owner)
    stop, events = asyncio.Event(), asyncio.Queue()
    task = asyncio.create_task(runtime_pipeline.answer_preview_loop(
        runtime, item, placeholder=SimpleNamespace(message_id=1), stop_event=stop, event_queue=events,
    ))
    await events.put(StreamEvent(kind=KIND_TEXT_DELTA, summary="one"))
    await asyncio.sleep(0.05)
    await events.put(StreamEvent(kind=KIND_TEXT_DELTA, summary=" first"))
    for _ in range(50):
        if task.done():
            await task
        if sent:
            break
        await asyncio.sleep(0.01)
    assert len(sent) == 1
    await events.put(StreamEvent(kind=KIND_TEXT_DELTA, summary="two"))
    await asyncio.sleep(0.06)
    stop.set()
    await task
    assert len(sent) == 1

@pytest.mark.asyncio
async def test_final_stream_is_not_promoted_after_access_revocation(tmp_path):
    from datetime import datetime
    from orchestrator import runtime_pipeline

    runtime, _observations, config_path = instance(tmp_path)
    owner = _delegated_owner(runtime_snapshot(runtime), 100)
    config = read_config_json(config_path)
    config["global"]["authorized_telegram_user_ids"] = []
    write_config_json(config_path, config)
    runtime.app.bot = SimpleNamespace(edit_message_text=AsyncMock(), delete_message=AsyncMock())
    item = SimpleNamespace(owner_id=owner, chat_id=100, request_id="r")
    state = runtime_pipeline.StreamedAnswerState(
        request_id="r", chat_id=100, placeholder=SimpleNamespace(message_id=1),
        buffer=["partial"], started_at=datetime.now(), delta_count=1,
    )
    result = await runtime_pipeline.finalize_streamed_answer(
        runtime, item, stream_state=state, final_text="private answer",
    )
    assert result.final_delivered is False
    assert result.fallback_required is True
    assert result.error == "telegram_access_revoked"
    assert runtime.app.bot.edit_message_text.await_count == 0

@pytest.mark.asyncio
async def test_final_stream_rechecks_grant_after_async_gate(tmp_path, monkeypatch):
    from datetime import datetime
    from orchestrator import runtime_pipeline

    runtime, _observations, config_path = instance(tmp_path)
    owner = _delegated_owner(runtime_snapshot(runtime), 100)
    runtime.app.bot = SimpleNamespace(edit_message_text=AsyncMock())
    item = SimpleNamespace(owner_id=owner, chat_id=100, request_id="r")
    state = runtime_pipeline.StreamedAnswerState(
        request_id="r", chat_id=100, placeholder=SimpleNamespace(message_id=1),
        buffer=["partial"], started_at=datetime.now(), delta_count=1,
    )
    completed = []
    store = SimpleNamespace(complete_delivery_outbox=lambda **entry: completed.append(entry))
    monkeypatch.setattr(runtime_pipeline, "_canonical_run_has_media", lambda *args: False)
    monkeypatch.setattr(runtime_pipeline.runtime_session, "ensure_store", lambda *args: store)
    monkeypatch.setattr(runtime_pipeline.runtime_session, "claim_run_delivery_outbox", lambda *args, **kwargs: {
        "state": "claimed", "claim": {"outbox_id": "ob", "lease_token": "lease"}
    })
    async def revoke(*args, **kwargs):
        config = read_config_json(config_path)
        config["global"]["authorized_telegram_user_ids"] = []
        write_config_json(config_path, config)
        return False
    monkeypatch.setattr(runtime_pipeline.telegram_delivery_failover, "handle_blocked_send", revoke)
    result = await runtime_pipeline.finalize_streamed_answer(
        runtime, item, stream_state=state, final_text="private answer",
    )
    assert result.final_delivered is False
    assert result.fallback_required is False
    assert result.error == "telegram_access_revoked"
    assert runtime.app.bot.edit_message_text.await_count == 0
    assert completed[0]["status"] == "suppressed"
