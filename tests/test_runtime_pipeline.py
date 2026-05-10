from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from orchestrator import runtime_pipeline


def test_begin_queue_item_sets_request_meta():
    maintenance = []
    runtime = SimpleNamespace(
        last_prompt=None,
        logger=SimpleNamespace(info=lambda *args, **kwargs: None),
        config=SimpleNamespace(active_backend="codex"),
        current_request_meta=None,
        is_generating=False,
        _mark_activity=lambda: None,
        _log_maintenance=lambda *args, **kwargs: maintenance.append((args, kwargs)),
    )
    item = SimpleNamespace(
        silent=False,
        source="telegram",
        created_at=(datetime.now() - timedelta(seconds=3)).isoformat(),
        request_id="req-1",
        summary="hello",
        prompt="hi",
    )

    result = runtime_pipeline.begin_queue_item(runtime, item)

    assert runtime.last_prompt is item
    assert runtime.current_request_meta["request_id"] == "req-1"
    assert runtime.is_generating is True
    assert result.is_bridge_request is False
    assert result.queue_wait_s >= 0
    assert maintenance


def test_persist_success_memory_updates_local_state():
    turns = []
    exchanges = []
    observer_calls = []
    transcripts = []
    runtime = SimpleNamespace(
        memory_store=SimpleNamespace(
            record_turn=lambda role, source, text: turns.append((role, source, text)),
            record_exchange=lambda user_text, assistant_text, source: exchanges.append((user_text, assistant_text, source)),
        ),
        config=SimpleNamespace(active_backend="codex"),
        _schedule_post_turn_observers=lambda *args, **kwargs: observer_calls.append((args, kwargs)),
        handoff_builder=SimpleNamespace(
            append_transcript=lambda role, text, source=None: transcripts.append((role, text, source)),
            refresh_recent_context=lambda: transcripts.append(("refresh", "", None)),
        ),
        project_chat_logger=SimpleNamespace(
            log_exchange=lambda prompt, response, source: transcripts.append(("log", prompt, response, source))
        ),
    )
    item = SimpleNamespace(
        prompt="hello",
        source="telegram",
        summary="hello",
    )

    runtime_pipeline.persist_success_memory(runtime, item, "world", is_bridge_request=False)

    assert ("user", "telegram", "hello") in turns
    assert ("assistant", "codex", "world") in turns
    assert exchanges == [("hello", "world", "telegram")]
    assert observer_calls
    assert ("user", "hello", "telegram") in transcripts
    assert ("assistant", "world", None) in transcripts


@pytest.mark.asyncio
async def test_handle_background_completion_updates_delivery_and_memory():
    turns = []
    sent = []
    voice = []
    observer_calls = []
    runtime = SimpleNamespace(
        _strip_transfer_accept_prefix=lambda item, text: text,
        _mark_success=lambda: None,
        _record_habit_outcome=lambda *args, **kwargs: None,
        _should_buffer_during_transfer=lambda request_id: False,
        last_response=None,
        workspace_dir="/tmp",
        get_current_model=lambda: "model",
        config=SimpleNamespace(active_backend="codex"),
        session_id_dt="sid",
        _last_full_prompt_tokens=0,
        _thinking_chars_this_req=0,
        _last_prompt_audit={},
        _get_system_prompt_text=lambda: "sys",
        memory_store=SimpleNamespace(
            record_turn=lambda role, source, text: turns.append((role, source, text)),
            record_exchange=lambda user_text, assistant_text, source: turns.append(("exchange", user_text, assistant_text, source)),
        ),
        _schedule_post_turn_observers=lambda *args, **kwargs: observer_calls.append((args, kwargs)),
        handoff_builder=SimpleNamespace(
            append_transcript=lambda role, text, source=None: turns.append(("transcript", role, text, source)),
            refresh_recent_context=lambda: turns.append(("refresh",)),
        ),
        project_chat_logger=SimpleNamespace(log_exchange=lambda prompt, response, source: turns.append(("log", prompt, response, source))),
        name="tester",
        send_long_message=lambda **kwargs: _record_send(sent, kwargs),
        _send_voice_reply=lambda chat_id, text, request_id: _record_voice(voice, chat_id, text, request_id),
        logger=SimpleNamespace(info=lambda *args, **kwargs: None),
    )
    item = SimpleNamespace(
        request_id="req-bg",
        chat_id=1,
        prompt="hello",
        source="telegram",
        summary="hello",
        created_at=datetime.now().isoformat(),
        silent=False,
        is_retry=False,
    )
    response = SimpleNamespace(is_success=True, text="world", usage=None)

    await runtime_pipeline.handle_background_completion(runtime, item, response)

    assert runtime.last_response["text"] == "world"
    assert sent[0]["purpose"] == "bg-response"
    assert voice == [(1, "world", "req-bg")]
    assert ("user", "telegram", "hello") in turns
    assert observer_calls


@pytest.mark.asyncio
async def test_handle_background_failure_clips_and_sends_error():
    sent = []
    runtime = SimpleNamespace(
        _mark_error=lambda *args, **kwargs: None,
        _record_habit_outcome=lambda *args, **kwargs: None,
        _should_buffer_during_transfer=lambda request_id: False,
        _record_suppressed_transfer_result=lambda *args, **kwargs: None,
        error_logger=SimpleNamespace(error=lambda *args, **kwargs: None),
        config=SimpleNamespace(active_backend="codex"),
        send_long_message=lambda chat_id, text, request_id, purpose: _record_send(sent, {"chat_id": chat_id, "text": text, "request_id": request_id, "purpose": purpose}),
    )
    item = SimpleNamespace(chat_id=1, request_id="req-bg", summary="sum")
    response = SimpleNamespace(error="x" * 3205)

    await runtime_pipeline.handle_background_failure(runtime, item, response)

    assert sent[0]["purpose"] == "bg-error"
    assert "[truncated]" in sent[0]["text"]


@pytest.mark.asyncio
async def test_handle_background_cancelled_sends_notice():
    sent = []
    runtime = SimpleNamespace(
        _mark_error=lambda *args, **kwargs: None,
        _record_habit_outcome=lambda *args, **kwargs: None,
        logger=SimpleNamespace(warning=lambda *args, **kwargs: None),
        send_long_message=lambda chat_id, text, request_id, purpose: _record_send(sent, {"chat_id": chat_id, "text": text, "request_id": request_id, "purpose": purpose}),
    )
    item = SimpleNamespace(chat_id=1, request_id="req-bg", summary="sum")

    await runtime_pipeline.handle_background_cancelled(runtime, item)

    assert sent[0]["purpose"] == "bg-cancelled"


@pytest.mark.asyncio
async def test_finalize_background_task_routes_success(monkeypatch):
    called = []

    async def _success(runtime, item, response):
        called.append(("success", response.text))

    monkeypatch.setattr(runtime_pipeline, "handle_background_completion", _success)
    task = SimpleNamespace(
        cancelled=lambda: False,
        exception=lambda: None,
        result=lambda: SimpleNamespace(is_success=True, text="ok"),
    )

    await runtime_pipeline.finalize_background_task(SimpleNamespace(), task, SimpleNamespace())

    assert called == [("success", "ok")]


@pytest.mark.asyncio
async def test_handle_detached_generation_registers_background_task():
    edited = []
    registered = []
    maintenance = []
    runtime = SimpleNamespace(
        app=SimpleNamespace(
            bot=SimpleNamespace(
                edit_message_text=lambda **kwargs: _record_edit(edited, kwargs),
            )
        ),
        _register_background_task=lambda task, item: registered.append((task, item.request_id)),
        logger=SimpleNamespace(info=lambda *args, **kwargs: None),
        config=SimpleNamespace(active_backend="codex"),
        _log_maintenance=lambda *args, **kwargs: maintenance.append((args, kwargs)),
    )
    item = SimpleNamespace(chat_id=1, request_id="req-detached")
    feedback = runtime_pipeline.InteractiveFeedback(
        stop_typing=None,
        typing_task=None,
        escalation_task=None,
        placeholder=SimpleNamespace(message_id=99),
        stream_callback=None,
        think_flush_task=None,
        on_stream_event=None,
    )
    generation = runtime_pipeline.BackendGeneration(
        response=None,
        detached=True,
        backend_started=datetime.now(),
        detach_after_s=30.0,
        generation_task="task-1",
    )

    await runtime_pipeline.handle_detached_generation(runtime, item, feedback, generation)

    assert edited[0]["message_id"] == 99
    assert registered == [("task-1", "req-detached")]
    assert maintenance


@pytest.mark.asyncio
async def test_handle_foreground_response_success_dispatches(monkeypatch):
    delivered = []
    notified = []
    runtime = SimpleNamespace(
        _strip_transfer_accept_prefix=lambda item, text: text,
        _mark_success=lambda: None,
        _record_habit_outcome=lambda *args, **kwargs: None,
        _notify_request_listeners=lambda request_id, payload: _record_notify(notified, request_id, payload),
    )
    item = SimpleNamespace(request_id="req-1", source="telegram", summary="hello")
    response = SimpleNamespace(is_success=True, text="world")
    start = runtime_pipeline.QueueItemStart(is_bridge_request=False, queued_at=datetime.now(), queue_wait_s=0.1)
    turn_prompt = runtime_pipeline.TurnPrompt(effective_prompt="hi", final_prompt="hi", incremental=False)

    async def _deliver(runtime_arg, item_arg, response_arg, **kwargs):
        delivered.append((item_arg.request_id, kwargs["display_text"]))

    monkeypatch.setattr(runtime_pipeline, "handle_success_delivery", _deliver)

    await runtime_pipeline.handle_foreground_response(
        runtime,
        item,
        response,
        start=start,
        turn_prompt=turn_prompt,
        backend_elapsed_s=0.2,
    )

    assert delivered == [("req-1", "world")]
    assert notified[0]["payload"]["success"] is True


@pytest.mark.asyncio
async def test_process_queue_item_stops_when_remote_blocked(monkeypatch):
    runtime = SimpleNamespace()
    item = SimpleNamespace(prompt="hello")
    called = []

    async def _blocked(runtime_arg, item_arg):
        called.append("blocked")
        return True

    monkeypatch.setattr(runtime_pipeline.runtime_remote, "handle_remote_backend_block", _blocked)

    await runtime_pipeline.process_queue_item(runtime, item)

    assert called == ["blocked"]


def test_handle_queue_processing_exception_marks_runtime_error():
    outcomes = []
    logs = []
    runtime = SimpleNamespace(
        _mark_error=lambda message: logs.append(("mark", message)),
        _record_habit_outcome=lambda *args, **kwargs: outcomes.append(kwargs),
        error_logger=SimpleNamespace(exception=lambda message: logs.append(("exception", message))),
        is_generating=True,
    )
    item = SimpleNamespace(request_id="req-1")

    runtime_pipeline.handle_queue_processing_exception(runtime, item, RuntimeError("boom"))

    assert runtime.is_generating is False
    assert outcomes[0]["success"] is False
    assert "boom" in outcomes[0]["error_text"]


def test_finalize_queue_iteration_clears_meta_and_tasks_done():
    called = []
    runtime = SimpleNamespace(
        current_request_meta={"request_id": "req-1"},
        queue=SimpleNamespace(task_done=lambda: called.append("done")),
    )
    item = SimpleNamespace(request_id="req-1")

    runtime_pipeline.finalize_queue_iteration(runtime, item)

    assert runtime.current_request_meta is None
    assert called == ["done"]


@pytest.mark.asyncio
async def test_run_queue_loop_processes_item_then_stops_on_cancel(monkeypatch):
    processed = []
    queue = asyncio.Queue()
    await queue.put(SimpleNamespace(prompt="hello", source="telegram", request_id="req-1"))
    runtime = SimpleNamespace(
        logger=SimpleNamespace(info=lambda *args, **kwargs: None, debug=lambda *args, **kwargs: None),
        queue=queue,
        current_request_meta=None,
    )

    async def _process(runtime_arg, item_arg):
        processed.append(item_arg.request_id)

    monkeypatch.setattr(runtime_pipeline, "process_queue_item", _process)

    task = asyncio.create_task(runtime_pipeline.run_queue_loop(runtime))
    await asyncio.sleep(0)
    task.cancel()
    await task

    assert processed == ["req-1"]


async def _record_send(sent, kwargs):
    sent.append(kwargs)
    return (0.0, 1)


async def _record_voice(voice, chat_id, text, request_id):
    voice.append((chat_id, text, request_id))


async def _record_edit(edited, kwargs):
    edited.append(kwargs)


async def _record_notify(notified, request_id, payload):
    notified.append({"request_id": request_id, "payload": payload})
