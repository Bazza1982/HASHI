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

@pytest.mark.asyncio
async def test_text_transport_recovers_frozen_owner_from_request(tmp_path, monkeypatch):
    runtime, _observations, config_path = instance(tmp_path)
    owner = _delegated_owner(runtime_snapshot(runtime), 100)
    runtime._request_meta_by_id = {
        "one": {"request_id": "one", "owner_id": owner, "hashi_session_id": "s"}
    }
    runtime.telegram_connected = True
    runtime.logger = logging.getLogger("test")
    runtime.telegram_logger = runtime.logger
    runtime.error_logger = runtime.logger
    runtime.workspace_dir = tmp_path
    runtime.config = SimpleNamespace(extra={})
    sent = []

    async def send_message(**kwargs):
        sent.append(kwargs)
        config = read_config_json(config_path)
        config["global"]["authorized_telegram_user_ids"] = []
        write_config_json(config_path, config)
        return SimpleNamespace(message_id=1)

    runtime.app.bot = SimpleNamespace(send_message=send_message)
    monkeypatch.setattr(runtime_delivery.runtime_delivery_order, "wait_for_turn", AsyncMock())
    monkeypatch.setattr(runtime_delivery.telegram_delivery_failover, "handle_blocked_send", AsyncMock(return_value=False))
    outcome = {}
    await runtime_delivery._send_long_message_transport(
        runtime, chat_id=100, text="a" * 9000, request_id="one",
        skip_local_capture=True, outbox_outcome=outcome,
    )
    assert len(sent) == 1
    assert outcome["status"] == "unknown"

@pytest.mark.asyncio
async def test_her_resolution_does_not_edit_after_access_revocation(tmp_path):
    from adapters.stream_events import (
        DELIVERY_USER_COMMENTARY, KIND_ACKNOWLEDGEMENT, KIND_INITIAL_RESOLUTION, StreamEvent,
    )
    from orchestrator import runtime_pipeline, telegram_stream_policy
    from tests.test_runtime_pipeline import _item, _runtime

    fixture, _observations, config_path = instance(tmp_path)
    owner = _delegated_owner(runtime_snapshot(fixture), 100)
    runtime = _runtime()
    runtime.name = "lily"
    runtime.global_config.config_path = config_path
    runtime.global_config.secrets_path = fixture.global_config.secrets_path
    runtime.global_config.owner_scope_id = "user:42"
    runtime.config.active_backend = "her-v2"
    runtime.backend_manager.current_backend.effort = "medium"
    runtime._commentary = True
    telegram_stream_policy.set_typing_enabled(runtime, False)

    async def send_text(chat_id, text, **kwargs):
        return await runtime.app.bot.send_message(chat_id=chat_id, text=text, parse_mode=kwargs.get("parse_mode"))
    runtime._send_text = send_text
    item = _item(request_id="scoped-her", chat_id=100, owner_id=owner)
    feedback = await runtime_pipeline.setup_interactive_feedback(
        runtime, item, audit_active=False, audit_collector=None,
    )
    accepted = await feedback.on_stream_event(StreamEvent(
        kind=KIND_ACKNOWLEDGEMENT, summary="Checking.", event_id="event-1",
        delivery_class=DELIVERY_USER_COMMENTARY, origin="her_v2", phase="immediate",
    ))
    assert accepted is True
    config = read_config_json(config_path)
    config["global"]["authorized_telegram_user_ids"] = []
    write_config_json(config_path, config)
    await feedback.on_stream_event(StreamEvent(
        kind=KIND_INITIAL_RESOLUTION, summary="Private answer.", event_id="event-2",
        delivery_class="internal", origin="her_v2", phase="initial_resolution",
        resolution="final", target_event_id="event-1", delivery_id="delivery-final",
    ))
    assert runtime.app.bot.edits == []

@pytest.mark.asyncio
async def test_verbose_placeholder_is_not_sent_after_access_revocation(tmp_path, monkeypatch):
    from orchestrator import runtime_pipeline

    runtime, _observations, config_path = instance(tmp_path)
    owner = _delegated_owner(runtime_snapshot(runtime), 100)
    config = read_config_json(config_path)
    config["global"]["authorized_telegram_user_ids"] = []
    write_config_json(config_path, config)
    runtime.app.bot = SimpleNamespace(send_message=AsyncMock())
    runtime.get_progress_placeholder = lambda: ("working", None)
    runtime.telegram_logger = logging.getLogger("test")
    item = SimpleNamespace(chat_id=100, request_id="r", owner_id=owner)
    monkeypatch.setattr(runtime_pipeline.telegram_delivery_failover, "handle_blocked_send", AsyncMock(return_value=False))
    result = await runtime_pipeline._send_live_verbose_placeholder(runtime, item)
    assert result is None
    assert runtime.app.bot.send_message.await_count == 0

@pytest.mark.asyncio
async def test_interactive_feedback_does_not_start_for_revoked_user(tmp_path):
    from orchestrator import runtime_pipeline, telegram_stream_policy
    from tests.test_runtime_pipeline import _item, _runtime

    fixture, _observations, config_path = instance(tmp_path)
    owner = _delegated_owner(runtime_snapshot(fixture), 100)
    config = read_config_json(config_path)
    config["global"]["authorized_telegram_user_ids"] = []
    write_config_json(config_path, config)
    runtime = _runtime()
    runtime.name = "lily"
    runtime.global_config.config_path = config_path
    runtime.global_config.secrets_path = fixture.global_config.secrets_path
    runtime.global_config.owner_scope_id = "user:42"
    telegram_stream_policy.set_typing_enabled(runtime, True)
    item = _item(request_id="revoked-feedback", chat_id=100, owner_id=owner)
    feedback = await runtime_pipeline.setup_interactive_feedback(
        runtime, item, audit_active=False, audit_collector=None,
    )
    assert feedback.placeholder is None
    assert runtime.app.bot.sent == []
    assert feedback.typing_task is None

@pytest.mark.asyncio
async def test_interactive_feedback_stops_if_access_is_revoked_during_wait(tmp_path, monkeypatch):
    from orchestrator import runtime_pipeline, telegram_stream_policy
    from tests.test_runtime_pipeline import _item, _runtime

    fixture, _observations, config_path = instance(tmp_path)
    owner = _delegated_owner(runtime_snapshot(fixture), 100)
    runtime = _runtime()
    runtime.name = "lily"
    runtime.global_config.config_path = config_path
    runtime.global_config.secrets_path = fixture.global_config.secrets_path
    runtime.global_config.owner_scope_id = "user:42"
    telegram_stream_policy.set_typing_enabled(runtime, True)

    async def revoke(*args, **kwargs):
        config = read_config_json(config_path)
        config["global"]["authorized_telegram_user_ids"] = []
        write_config_json(config_path, config)
        return False

    monkeypatch.setattr(runtime_pipeline.telegram_delivery_failover, "handle_blocked_send", revoke)
    item = _item(request_id="revoked-during-feedback", chat_id=100, owner_id=owner)
    feedback = await runtime_pipeline.setup_interactive_feedback(
        runtime, item, audit_active=False, audit_collector=None,
    )
    try:
        assert feedback.placeholder is None
        assert runtime.app.bot.sent == []
        assert feedback.typing_task is None
    finally:
        for task in (feedback.typing_task, feedback.escalation_task, feedback.think_flush_task):
            if task is not None:
                task.cancel()

@pytest.mark.asyncio
async def test_placeholder_edits_stop_when_access_is_revoked(tmp_path):
    import asyncio
    from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime

    runtime, _observations, config_path = instance(tmp_path)
    owner = _delegated_owner(runtime_snapshot(runtime), 100)
    runtime.telegram_connected = True
    runtime.config = SimpleNamespace(extra={"escalation_thresholds": [0]})
    runtime._verbose = False
    runtime._request_meta_by_id = {"r": {"owner_id": owner}}
    runtime.app.bot = SimpleNamespace(edit_message_text=AsyncMock())
    runtime.telegram_logger = logging.getLogger("test")
    config = read_config_json(config_path)
    config["global"]["authorized_telegram_user_ids"] = []
    write_config_json(config_path, config)

    stop = asyncio.Event()
    task = asyncio.create_task(FlexibleAgentRuntime._escalating_placeholder_loop(
        runtime, 100, SimpleNamespace(message_id=12), "r", stop
    ))
    await asyncio.sleep(0.02)
    stop.set()
    await asyncio.wait_for(task, timeout=1)
    runtime.app.bot.edit_message_text.assert_not_awaited()

@pytest.mark.asyncio
async def test_streaming_progress_stops_when_access_is_revoked(tmp_path):
    import asyncio
    from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime
    from adapters.stream_events import KIND_REVIEW, StreamEvent

    runtime, _observations, config_path = instance(tmp_path)
    owner = _delegated_owner(runtime_snapshot(runtime), 100)
    runtime.telegram_connected = True
    runtime.workspace_dir = tmp_path
    runtime.config = SimpleNamespace(active_backend="her-v2", extra={})
    runtime._verbose = True
    runtime._request_meta_by_id = {"r": {"owner_id": owner}}
    runtime.app.bot = SimpleNamespace(edit_message_text=AsyncMock(), send_message=AsyncMock())
    runtime.telegram_logger = logging.getLogger("test")
    config = read_config_json(config_path)
    config["global"]["authorized_telegram_user_ids"] = []
    write_config_json(config_path, config)

    queue = asyncio.Queue()
    await queue.put(StreamEvent(kind=KIND_REVIEW, summary="Finished", phase="review"))
    stop = asyncio.Event()
    stop.set()
    await FlexibleAgentRuntime._streaming_display_loop(
        runtime, 100, SimpleNamespace(message_id=12), "r", stop, queue,
    )
    runtime.app.bot.edit_message_text.assert_not_awaited()
    runtime.app.bot.send_message.assert_not_awaited()
