from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from orchestrator import runtime_lifecycle


@pytest.mark.asyncio
async def test_prompt_failure_finishes_durable_run_and_visible_activity(tmp_path,monkeypatch):
    from types import MethodType
    from unittest.mock import AsyncMock
    from tests.test_runtime_pipeline import _runtime, _item
    from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime
    from orchestrator.phone_context_handoff import PhoneContextError
    from orchestrator.request_activity import RequestActivityStore
    from orchestrator.session_store import SessionStore
    runtime=_runtime()
    runtime.session_store=SessionStore(tmp_path/"sessions.sqlite3",instance_id="HASHI1")
    session=runtime.session_store.create_session(owner_id="owner",agent_id=runtime.name,title="test")
    accepted=runtime.session_store.accept_run(session_id=session["session_id"],owner_id="owner",
        agent_id=runtime.name,request_id="req-context-error",text="Read the test status",
        source="session-api",idempotency_key="context-error")
    item=_item(request_id=accepted.request_id,session_id=session["session_id"],run_id=accepted.run_id,
        context_generation=1,owner_id="owner",source="session-api",session_surface="workbench",
        session_channel_key="default")
    runtime.request_activity=RequestActivityStore()
    runtime.request_activity.start(item.request_id)
    runtime.queue=asyncio.Queue()
    runtime._request_listeners={}
    runtime._pending_request_results={}
    runtime._notify_request_listeners=MethodType(FlexibleAgentRuntime._notify_request_listeners,runtime)
    runtime._remote_backend_block_reason=lambda source: None
    runtime._notify_right_brain_interrupted=lambda *args,**kwargs: None
    runtime.error_logger.exception=lambda message: None
    monkeypatch.setattr(runtime_lifecycle.runtime_workzone,"activate_backend_state",AsyncMock())
    async def fail_prompt(*args,**kwargs):
        raise PhoneContextError("phone_context_handoff_unavailable",source=session["session_id"])
    monkeypatch.setattr(runtime_lifecycle.runtime_pipeline,"build_turn_prompt",fail_prompt)
    monkeypatch.setattr("orchestrator.runtime_media.finish_native_voice_transcript_path",AsyncMock())
    monkeypatch.setattr("orchestrator.frontend_whatsapp_mirror.deliver_whatsapp_mirror",AsyncMock())
    monkeypatch.setattr("orchestrator.runtime_debug_reporting.schedule_terminal_diagnostic",lambda *a,**k: None)
    monkeypatch.setattr("orchestrator.runtime_debug_reporting.schedule_failure_report",lambda *a,**k: None)
    await runtime.queue.put(item)
    task=asyncio.create_task(runtime_lifecycle.process_queue(runtime))
    try:
        await asyncio.wait_for(runtime.queue.join(),timeout=2)
    finally:
        task.cancel()
        await task
    run=runtime.session_store.get_run(accepted.run_id)
    assert run["state"]=="failed"
    activity=runtime.request_activity.poll(item.request_id)
    assert activity["terminal"] is True and activity["state"]=="failed"
    assert runtime.backend_manager.calls==[]
    restored=SessionStore(tmp_path/"sessions.sqlite3",instance_id="HASHI1")
    assert restored.get_run(accepted.run_id)["state"]=="failed"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "terminal_fault", [None, "before_commit", "after_commit", "readback", "missing_run"]
)
async def test_user_stop_cancellation_persists_stopped_run_for_refresh(
    tmp_path, monkeypatch, terminal_fault
):
    from types import MethodType
    from unittest.mock import AsyncMock

    from orchestrator import runtime_control, ui_language
    from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime
    from orchestrator.request_activity import RequestActivityStore
    from orchestrator.session_store import SessionNotFound, SessionStore
    from tests.test_runtime_pipeline import _item, _runtime

    runtime = _runtime()
    database = tmp_path / "sessions.sqlite3"
    runtime.session_store = SessionStore(database, instance_id="HASHI1")
    owner = "owner"
    session = runtime.session_store.create_session(
        owner_id=owner, agent_id=runtime.name, title="stop refresh"
    )
    accepted = runtime.session_store.accept_run(
        session_id=session["session_id"],
        owner_id=owner,
        agent_id=runtime.name,
        request_id="req-user-stop",
        text="Keep working until stopped",
        source="session-api",
        idempotency_key="user-stop-refresh",
    )
    item = _item(
        request_id=accepted.request_id,
        session_id=session["session_id"],
        run_id=accepted.run_id,
        context_generation=1,
        owner_id=owner,
        source="session-api",
        session_surface="workbench",
        session_channel_key="default",
    )
    runtime.request_activity = RequestActivityStore()
    runtime.request_activity.start(item.request_id)
    runtime.queue = asyncio.Queue()
    runtime._request_listeners = {}
    runtime._pending_request_results = {}
    runtime._background_request_ids = set()
    runtime._request_meta_by_id = {}
    runtime._session_executions = {}
    runtime._session_execution_tasks = {}
    runtime._notify_request_listeners = MethodType(
        FlexibleAgentRuntime._notify_request_listeners, runtime
    )
    runtime._remote_backend_block_reason = lambda _source: None
    runtime._notify_right_brain_interrupted = lambda *args, **kwargs: None
    runtime._is_authorized_user = lambda _user_id: True
    runtime._reply_text = AsyncMock()
    runtime.error_logger.exception = lambda _message: None

    entered_prompt_build = asyncio.Event()

    async def hold_prompt_build(*_args, **_kwargs):
        entered_prompt_build.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(
        runtime_lifecycle.runtime_workzone, "activate_backend_state", AsyncMock()
    )
    monkeypatch.setattr(
        runtime_lifecycle.runtime_pipeline, "build_turn_prompt", hold_prompt_build
    )
    monkeypatch.setattr(
        "orchestrator.runtime_media.finish_native_voice_transcript_path", AsyncMock()
    )
    monkeypatch.setattr(
        "orchestrator.frontend_whatsapp_mirror.deliver_whatsapp_mirror", AsyncMock()
    )
    monkeypatch.setattr(
        "orchestrator.runtime_debug_reporting.schedule_terminal_diagnostic",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "orchestrator.runtime_debug_reporting.schedule_failure_report",
        lambda *args, **kwargs: None,
    )

    await runtime.queue.put(item)
    dequeued = await runtime.queue.get()
    execution_task = asyncio.create_task(
        runtime_lifecycle._process_single_item(runtime, dequeued)
    )
    runtime._session_execution_tasks[item.request_id] = execution_task
    await asyncio.wait_for(entered_prompt_build.wait(), timeout=2)
    assert runtime.session_store.get_run(accepted.run_id)["state"] == "running"

    message = SimpleNamespace(text="/stop", chat=SimpleNamespace(id=123))
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=123),
        effective_chat=SimpleNamespace(id=123),
        effective_message=message,
        message=message,
    )
    write_attempts = []
    if terminal_fault in {"before_commit", "after_commit"}:
        original_finish = runtime.session_store.finish_request

        def finish_with_fault(*args, **kwargs):
            write_attempts.append(1)
            if terminal_fault == "before_commit":
                raise OSError("isolated terminal write failed before commit")
            original_finish(*args, **kwargs)
            raise OSError("isolated terminal write reported an error after commit")

        monkeypatch.setattr(runtime.session_store, "finish_request", finish_with_fault)
    elif terminal_fault in {"readback", "missing_run"}:

        def readback_with_fault(*args, **kwargs):
            if terminal_fault == "missing_run":
                raise SessionNotFound("isolated Run could not be found")
            raise OSError("isolated terminal readback failed")

        monkeypatch.setattr(runtime.session_store, "get_run_by_request", readback_with_fault)
    await runtime_control.cmd_stop(runtime, update, SimpleNamespace(args=[]))

    assert execution_task.done()
    reopened = SessionStore(database, instance_id="HASHI1")
    expected_state = "running" if terminal_fault == "before_commit" else "stopped"
    assert reopened.get_run(accepted.run_id, owner_id=owner)["state"] == expected_state
    events = reopened.events(session["session_id"], owner_id=owner)
    assert [event["kind"] for event in events].count("run.stopped") == (
        0 if terminal_fault == "before_commit" else 1
    )
    reply = runtime._reply_text.await_args.args[1]
    unconfirmed = ui_language.tr("control.stop.unconfirmed", count=1)
    if terminal_fault in {"before_commit", "readback", "missing_run"}:
        assert reply == unconfirmed
    else:
        assert reply != unconfirmed
        assert ui_language.tr("control.stop.stopped", count=0) in reply
    if terminal_fault in {"before_commit", "after_commit"}:
        assert len(write_attempts) == 1


class _Logger:
    def __init__(self):
        self.messages = []

    def info(self, message):
        self.messages.append(message)

    def warning(self, message):
        self.messages.append(message)


@pytest.mark.asyncio
async def test_terminal_run_releases_capability_task_by_request_id():
    released = []

    async def release(request_id):
        released.append(request_id)
        return 1

    runtime = SimpleNamespace(
        orchestrator=SimpleNamespace(cancel_capability_task=release),
        error_logger=_Logger(),
    )

    await runtime_lifecycle.release_capability_task(runtime, "req-terminal")

    assert released == ["req-terminal"]


@pytest.mark.asyncio
async def test_shutdown_cancels_long_batch_timeout_and_finalize_tasks():
    async def _wait_forever():
        await asyncio.Event().wait()

    async def _shutdown_backend():
        return None

    timeout_task = asyncio.create_task(_wait_forever())
    finalize_task = asyncio.create_task(_wait_forever())
    await asyncio.sleep(0)
    shutdown_states = []
    runtime = SimpleNamespace(
        name="zelda",
        logger=_Logger(),
        error_logger=_Logger(),
        is_shutting_down=False,
        _scheduled_retry_tasks=set(),
        _persona_background_status_tasks=set(),
        _background_tasks=set(),
        _long_buffer_timeout_task=timeout_task,
        _long_finalize_task=finalize_task,
        process_task=None,
        backend_manager=SimpleNamespace(shutdown=_shutdown_backend),
        startup_success=False,
        _mark_runtime_shutdown=lambda clean: shutdown_states.append(clean),
    )

    await runtime_lifecycle.shutdown(runtime)

    assert timeout_task.cancelled()
    assert finalize_task.cancelled()
    assert runtime._long_buffer_timeout_task is None
    assert runtime._long_finalize_task is None
    assert shutdown_states == [True]


@pytest.mark.asyncio
async def test_shutdown_marks_unclean_and_finishes_when_queue_ignores_cancel(monkeypatch):
    release_queue = asyncio.Event()
    queue_cancelled = asyncio.Event()
    backend_shutdown = asyncio.Event()
    shutdown_states = []

    async def _stubborn_queue():
        while not release_queue.is_set():
            try:
                await release_queue.wait()
            except asyncio.CancelledError:
                queue_cancelled.set()

    async def _shutdown_backend():
        backend_shutdown.set()

    process_task = asyncio.create_task(_stubborn_queue())
    await asyncio.sleep(0)
    runtime = SimpleNamespace(
        name="samantha",
        logger=_Logger(),
        error_logger=_Logger(),
        is_shutting_down=False,
        _scheduled_retry_tasks=set(),
        _persona_background_status_tasks=set(),
        _background_tasks=set(),
        process_task=process_task,
        backend_manager=SimpleNamespace(shutdown=_shutdown_backend),
        startup_success=False,
        _mark_runtime_shutdown=lambda clean: shutdown_states.append(clean),
    )
    monkeypatch.setattr(
        runtime_lifecycle,
        "RUNTIME_TASK_SHUTDOWN_TIMEOUT_SECONDS",
        0.01,
    )

    with pytest.raises(RuntimeError, match="shutdown was incomplete"):
        await asyncio.wait_for(runtime_lifecycle.shutdown(runtime), timeout=0.5)

    assert queue_cancelled.is_set()
    assert backend_shutdown.is_set()
    assert shutdown_states == [False]
    assert runtime.process_task is process_task
    release_queue.set()
    await process_task


@pytest.mark.asyncio
async def test_shutdown_skips_dormant_worker_updater_and_application_stop():
    shutdown_states = []

    async def _shutdown_backend():
        return None

    async def _not_running():
        raise RuntimeError("This Application is not running!")

    runtime = SimpleNamespace(
        name="samantha",
        logger=_Logger(),
        error_logger=_Logger(),
        is_shutting_down=False,
        _scheduled_retry_tasks=set(),
        _persona_background_status_tasks=set(),
        _background_tasks=set(),
        process_task=None,
        backend_manager=SimpleNamespace(shutdown=_shutdown_backend),
        startup_success=True,
        app=SimpleNamespace(
            updater=SimpleNamespace(running=False, stop=_not_running),
            running=False,
            stop=_not_running,
            shutdown=_not_running,
        ),
        _mark_runtime_shutdown=lambda clean: shutdown_states.append(clean),
    )

    await runtime_lifecycle.shutdown(runtime)

    assert shutdown_states == [True]
    assert len(runtime.error_logger.messages) == 1
    assert "telegram-app-shutdown" in runtime.error_logger.messages[0]


@pytest.mark.asyncio
async def test_shutdown_gives_telegram_updater_request_timeout_headroom(monkeypatch):
    shutdown_states = []
    calls = []

    async def _shutdown_backend():
        calls.append("backend")

    async def _slow_updater_stop():
        await asyncio.sleep(0.03)
        calls.append("telegram-updater")

    async def _stop_app():
        calls.append("telegram-app-stop")

    async def _shutdown_app():
        calls.append("telegram-app-shutdown")

    runtime = SimpleNamespace(
        name="sunny",
        logger=_Logger(),
        error_logger=_Logger(),
        is_shutting_down=False,
        _scheduled_retry_tasks=set(),
        _persona_background_status_tasks=set(),
        _background_tasks=set(),
        process_task=None,
        backend_manager=SimpleNamespace(shutdown=_shutdown_backend),
        startup_success=True,
        app=SimpleNamespace(
            updater=SimpleNamespace(running=True, stop=_slow_updater_stop),
            running=True,
            stop=_stop_app,
            shutdown=_shutdown_app,
        ),
        _mark_runtime_shutdown=lambda clean: shutdown_states.append(clean),
    )
    monkeypatch.setattr(
        runtime_lifecycle,
        "RUNTIME_SERVICE_SHUTDOWN_TIMEOUT_SECONDS",
        0.01,
    )
    monkeypatch.setattr(
        runtime_lifecycle,
        "RUNTIME_TELEGRAM_UPDATER_SHUTDOWN_TIMEOUT_SECONDS",
        0.1,
    )

    await runtime_lifecycle.shutdown(runtime)

    assert calls == [
        "backend",
        "telegram-updater",
        "telegram-app-stop",
        "telegram-app-shutdown",
    ]
    assert shutdown_states == [True]
    assert runtime.error_logger.messages == []
