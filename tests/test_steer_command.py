from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from orchestrator import (
    runtime_cancel,
    runtime_control,
    runtime_execution,
    runtime_retry,
)
from orchestrator.runtime_command_binding import BOT_COMMAND_BINDINGS, COMMAND_BINDINGS


def test_steer_is_registered_in_command_and_telegram_menus():
    assert any(b.name == "steer" and b.method_name == "cmd_steer" for b in COMMAND_BINDINGS)
    assert any(b.name == "steer" for b in BOT_COMMAND_BINDINGS)
    binding = next(b for b in BOT_COMMAND_BINDINGS if b.name == "steer")
    assert "direction" in binding.description.lower() or "continue" in binding.description.lower()


def test_extract_steer_direction_preserves_text():
    msg = SimpleNamespace(text="/steer also include unit tests for auth")
    update = SimpleNamespace(effective_message=msg, message=None)
    context = SimpleNamespace(args=["also", "include", "unit", "tests", "for", "auth"])
    assert runtime_control.extract_steer_direction(update, context) == "also include unit tests for auth"

    msg2 = SimpleNamespace(text="/steer@XishiBot keep artefacts\nand add logging")
    update2 = SimpleNamespace(effective_message=msg2, message=None)
    direction = runtime_control.extract_steer_direction(update2, SimpleNamespace(args=[]))
    assert "keep artefacts" in direction
    assert "add logging" in direction


def test_build_steer_prompt_keeps_original_and_forbids_reset():
    prompt = runtime_control.build_steer_prompt(
        direction="also include xxx in your tasks",
        original_prompt="Build the OAuth login flow",
        backend="her-v2",
    )
    assert "[HASHI /steer" in prompt
    assert "also include xxx in your tasks" in prompt
    assert "Build the OAuth login flow" in prompt
    assert "KEEP all interim progress" in prompt
    assert "Do NOT call session-reset" in prompt
    assert "her" in prompt


@pytest.mark.asyncio
async def test_cmd_steer_requires_direction():
    replies: list[str] = []

    async def _reply(_update, text, **_kwargs):
        replies.append(text)

    runtime = SimpleNamespace(
        name="xishi",
        logger=SimpleNamespace(warning=lambda *a, **k: None),
        config=SimpleNamespace(active_backend="codex-cli", engine="codex-cli"),
        queue=asyncio.Queue(),
        backend_manager=SimpleNamespace(current_backend=None),
        current_request_meta=None,
        last_prompt=None,
        is_generating=False,
        _is_authorized_user=lambda _uid: True,
        _reply_text=_reply,
        enqueue_request=AsyncMock(),
    )
    msg = SimpleNamespace(text="/steer", chat=SimpleNamespace(id=42))
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=1),
        effective_chat=SimpleNamespace(id=42),
        effective_message=msg,
        message=msg,
    )
    await runtime_control.cmd_steer(runtime, update, SimpleNamespace(args=[]))
    assert replies
    assert "Usage: /steer" in replies[0]
    runtime.enqueue_request.assert_not_awaited()


@pytest.mark.asyncio
async def test_cmd_steer_stops_clears_queue_and_enqueues_continuation():
    replies: list[str] = []
    shutdown = AsyncMock()
    enqueued: list[tuple] = []

    async def _reply(_update, text, **_kwargs):
        replies.append(text)

    async def _enqueue(chat_id, prompt, source, summary, **_kwargs):
        enqueued.append((chat_id, prompt, source, summary, _kwargs))
        return "req-steer-1"

    queue: asyncio.Queue = asyncio.Queue()
    await queue.put(object())
    await queue.put(object())

    runtime = SimpleNamespace(
        name="xishi",
        logger=SimpleNamespace(warning=lambda *a, **k: None),
        config=SimpleNamespace(active_backend="grok-cli", engine="grok-cli"),
        queue=queue,
        backend_manager=SimpleNamespace(
            current_backend=SimpleNamespace(shutdown=shutdown),
            initialize_active_backend=AsyncMock(return_value=True),
        ),
        current_request_meta={
            "request_id": "req-old",
            "chat_id": 42,
            "prompt": "Implement feature X end to end",
            "source": "text",
            "summary": "Feature X",
            "hashi_run_id": "run-old",
        },
        last_prompt=SimpleNamespace(
            prompt="Implement feature X end to end", chat_id=42
        ),
        is_generating=True,
        _is_authorized_user=lambda _uid: True,
        _reply_text=_reply,
        _notify_right_brain_interrupted=lambda *a, **k: None,
        enqueue_request=_enqueue,
        session_store=SimpleNamespace(
            get_run_by_request=lambda request_id, **_kwargs: {
                "request_id": request_id,
                "run_id": "run-new",
            }
        ),
    )
    runtime.backend_manager.current_backend.steer_continuation_evidence = (
        lambda request_id: {
            "kind": "herv3_active_turn_recovery",
            "source_request_id": request_id,
            "evidence_ref": "herv3:session-1:turn-old",
        }
    )
    text = "/steer also include xxx in your tasks"
    msg = SimpleNamespace(text=text, chat=SimpleNamespace(id=42))
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=1),
        effective_chat=SimpleNamespace(id=42),
        effective_message=msg,
        message=msg,
    )
    context = SimpleNamespace(
        args=["also", "include", "xxx", "in", "your", "tasks"]
    )
    receipt = await runtime_control.cmd_steer(runtime, update, context)

    shutdown.assert_awaited()
    assert queue.empty()
    assert len(enqueued) == 1
    chat_id, prompt, source, summary, options = enqueued[0]
    assert chat_id == 42
    assert source == "steer"
    assert options["request_metadata"]["parent_run_id"] == "run-old"
    assert context.derived_request_id == "req-steer-1"
    assert receipt["type"] == "hashi.steer-receipt"
    assert receipt["version"] == 2
    assert receipt["old_run"]["run_id"] == "run-old"
    assert receipt["new_run"]["run_id"] == "run-new"
    assert receipt["new_run"]["parent_run_id"] == "run-old"
    assert receipt["new_run"]["continues_run_id"] == "run-old"
    assert receipt["new_run"]["relationship"] == "linked_successor"
    assert receipt["new_run"]["created"] is True
    assert receipt["continuation"]["old_run_id"] == "run-old"
    assert receipt["continuation"]["new_run_id"] == "run-new"
    assert receipt["continuation"]["relationship"] == "linked_successor"
    assert (
        receipt["continuation"]["evidence_ref"]
        == "herv3:session-1:turn-old"
    )
    assert "also include xxx" in prompt
    assert "Implement feature X end to end" in prompt
    assert "KEEP all interim progress" in prompt
    assert replies and "Steered" in replies[0]
    assert "run-old" in replies[0]
    assert "run-new" in replies[0]
    # Intentional kill marker so exit -9 is not shown as backend error.
    assert getattr(runtime, "_user_interrupt", None) is not None
    assert runtime._user_interrupt["reason"] == "user_steer"
    assert runtime._user_interrupt["request_id"] == "req-old"
    assert runtime_control.consume_user_interrupt(runtime, "req-old") == "user_steer"
    assert runtime_control.consume_user_interrupt(runtime, "req-old") is None


@pytest.mark.asyncio
async def test_steer_successor_reuses_parent_telegram_destination(monkeypatch):
    from orchestrator.frontend_delivery import (
        RUN_DELIVERY_ROUTE_METADATA_KEY,
        freeze_run_delivery_route,
    )

    enqueue = AsyncMock(return_value="req-new")
    runtime = SimpleNamespace(enqueue_request=enqueue)
    monkeypatch.setattr(
        runtime_control,
        "_command_request_route",
        lambda _runtime, _update: (
            0,
            {
                "session_id": "session-1",
                "owner_id": "user:7",
                "session_surface": "session-api",
                "session_channel_key": "client-1",
            },
            False,
        ),
    )
    parent_route = freeze_run_delivery_route(
        message_source_id="session_api",
        session_surface="session-api",
        session_channel_key="client-1",
        chat_id=7430217666,
        telegram_requested=True,
    )
    parent_meta = {
        "hashi_session_id": "session-1",
        "chat_id": 7430217666,
        "request_metadata": {
            RUN_DELIVERY_ROUTE_METADATA_KEY: parent_route
        },
    }

    request_id = await runtime_control._enqueue_command_request(
        runtime,
        SimpleNamespace(),
        "continue",
        "steer",
        "Steer: continue",
        continuation_parent_meta=parent_meta,
    )

    assert request_id == "req-new"
    args, kwargs = enqueue.await_args
    assert args[0] == 7430217666
    assert kwargs["request_metadata"]["session_id"] == "session-1"
    assert (
        kwargs["request_metadata"][RUN_DELIVERY_ROUTE_METADATA_KEY] == parent_route
    )


@pytest.mark.asyncio
async def test_runtime_steer_and_focus_wrappers_return_typed_receipts(monkeypatch):
    from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime

    steer_receipt = {"type": "hashi.steer-receipt", "version": 2}
    focus_receipt = {"type": "hashi.focus-receipt", "version": 2}
    steer = AsyncMock(return_value=steer_receipt)
    focus = AsyncMock(return_value=focus_receipt)
    monkeypatch.setattr(runtime_control, "cmd_steer", steer)
    monkeypatch.setattr(runtime_control, "cmd_focus", focus)
    runtime = SimpleNamespace()
    update = SimpleNamespace()
    context = SimpleNamespace()

    assert (
        await FlexibleAgentRuntime.cmd_steer(runtime, update, context)
        == steer_receipt
    )
    assert (
        await FlexibleAgentRuntime.cmd_focus(runtime, update, context)
        == focus_receipt
    )


@pytest.mark.asyncio
async def test_cmd_steer_uses_native_provider_turn_without_rebuilding_run():
    replies: list[str] = []
    shutdown = AsyncMock()
    enqueue = AsyncMock()
    native_steer = AsyncMock(
        return_value={
            "kind": "codex_app_server_native_steer",
            "method": "turn/steer",
            "status": "accepted",
            "source_request_id": "req-old",
            "provider_thread_id": "thread-old",
            "provider_turn_id": "turn-old",
            "accepted_turn_id": "turn-old",
            "evidence_ref": "codex-app-server:thread-old:turn-old:steer:abc123",
        }
    )

    async def _reply(_update, text, **_kwargs):
        replies.append(text)

    queue: asyncio.Queue = asyncio.Queue()
    await queue.put(object())
    await queue.put(object())
    backend = SimpleNamespace(shutdown=shutdown, steer_active=native_steer)
    runtime = SimpleNamespace(
        name="xishi",
        logger=SimpleNamespace(warning=lambda *a, **k: None),
        config=SimpleNamespace(active_backend="codex-cli", engine="codex-cli"),
        queue=queue,
        backend_manager=SimpleNamespace(current_backend=backend),
        current_request_meta={
            "request_id": "req-old",
            "chat_id": 42,
            "prompt": "Implement feature X end to end",
            "source": "text",
            "summary": "Feature X",
            "hashi_run_id": "run-old",
        },
        last_prompt=SimpleNamespace(
            prompt="Implement feature X end to end", chat_id=42
        ),
        is_generating=True,
        _is_authorized_user=lambda _uid: True,
        _reply_text=_reply,
        enqueue_request=enqueue,
    )
    msg = SimpleNamespace(
        text="/steer focus on failing tests first",
        chat=SimpleNamespace(id=42),
    )
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=1),
        effective_chat=SimpleNamespace(id=42),
        effective_message=msg,
        message=msg,
    )
    context = SimpleNamespace(args=["focus", "on", "failing", "tests", "first"])

    receipt = await runtime_control.cmd_steer(runtime, update, context)

    native_steer.assert_awaited_once_with(
        "focus on failing tests first", request_id="req-old"
    )
    shutdown.assert_not_awaited()
    enqueue.assert_not_awaited()
    assert queue.empty()
    assert getattr(runtime, "_user_interrupt", None) is None
    assert context.derived_request_id == "req-old"
    assert receipt["version"] == 2
    assert receipt["old_run"]["run_id"] == "run-old"
    assert receipt["old_run"]["terminal"] is None
    assert receipt["new_run"]["run_id"] == "run-old"
    assert receipt["new_run"]["created"] is False
    assert receipt["new_run"]["relationship"] == "continued_in_place"
    assert receipt["continuation"]["provider_turn_id"] == "turn-old"
    assert receipt["continuation"]["old_run_id"] == "run-old"
    assert receipt["continuation"]["continuing_run_id"] == "run-old"
    assert replies and "Steered in place" in replies[0]
    assert "same Run" in replies[0]


@pytest.mark.asyncio
async def test_cmd_steer_idle_sends_plain_direction_without_wrapper():
    replies: list[str] = []
    enqueued: list[tuple] = []
    shutdown = AsyncMock()

    async def _reply(_update, text, **_kwargs):
        replies.append(text)

    async def _enqueue(chat_id, prompt, source, summary, **_kwargs):
        enqueued.append((chat_id, prompt, source, summary))
        return "req-idle-1"

    runtime = SimpleNamespace(
        name="xishi",
        logger=SimpleNamespace(warning=lambda *a, **k: None),
        config=SimpleNamespace(active_backend="codex-cli", engine="codex-cli"),
        queue=asyncio.Queue(),
        backend_manager=SimpleNamespace(
            current_backend=SimpleNamespace(shutdown=shutdown),
            initialize_active_backend=AsyncMock(return_value=True),
        ),
        current_request_meta=None,
        last_prompt=SimpleNamespace(prompt="Previous finished task", chat_id=42),
        is_generating=False,
        _is_authorized_user=lambda _uid: True,
        _reply_text=_reply,
        _notify_right_brain_interrupted=lambda *a, **k: None,
        enqueue_request=_enqueue,
    )
    text = "/steer also include unit tests"
    msg = SimpleNamespace(text=text, chat=SimpleNamespace(id=42))
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=1),
        effective_chat=SimpleNamespace(id=42),
        effective_message=msg,
        message=msg,
    )
    await runtime_control.cmd_steer(
        runtime,
        update,
        SimpleNamespace(args=["also", "include", "unit", "tests"]),
    )

    shutdown.assert_not_awaited()
    assert len(enqueued) == 1
    chat_id, prompt, source, summary = enqueued[0]
    assert chat_id == 42
    assert prompt == "also include unit tests"
    assert source == "text"
    assert "[HASHI /steer" not in prompt
    assert "Previous finished task" not in prompt
    assert replies and "idle" in replies[0].lower()
    assert "no steer wrapper" in replies[0].lower()
    # Idle path must not mark a user interrupt (nothing was killed).
    assert getattr(runtime, "_user_interrupt", None) is None


@pytest.mark.asyncio
async def test_cmd_steer_treats_detached_her_generation_as_busy():
    replies: list[str] = []
    enqueued: list[tuple] = []
    interrupted: list[tuple] = []
    shutdown = AsyncMock()

    async def _reply(_update, text, **_kwargs):
        replies.append(text)

    async def _enqueue(chat_id, prompt, source, summary, **_kwargs):
        enqueued.append((chat_id, prompt, source, summary))
        return "req-steer-detached"

    def _notify(item, prompt, **kwargs):
        interrupted.append((item, prompt, kwargs))

    detached_meta = {
        "request_id": "req-detached",
        "chat_id": 42,
        "prompt": "Find the AI detector named in Heidi's meeting summary",
        "source": "voice_transcript",
        "summary": "Voice request",
    }
    runtime = SimpleNamespace(
        name="sunny",
        logger=SimpleNamespace(warning=lambda *a, **k: None),
        config=SimpleNamespace(active_backend="her-v2", engine="her-v2"),
        queue=asyncio.Queue(),
        backend_manager=SimpleNamespace(
            current_backend=SimpleNamespace(
                shutdown=shutdown,
                persistent_session_busy=True,
            ),
            initialize_active_backend=AsyncMock(return_value=True),
        ),
        current_request_meta=None,
        _background_request_ids={"req-detached"},
        _request_meta_by_id={"req-detached": detached_meta},
        last_prompt=SimpleNamespace(**detached_meta),
        is_generating=False,
        _is_authorized_user=lambda _uid: True,
        _reply_text=_reply,
        _notify_right_brain_interrupted=_notify,
        enqueue_request=_enqueue,
    )
    msg = SimpleNamespace(text="/steer the name is Heidi", chat=SimpleNamespace(id=42))
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=1),
        effective_chat=SimpleNamespace(id=42),
        effective_message=msg,
        message=msg,
    )

    await runtime_control.cmd_steer(
        runtime,
        update,
        SimpleNamespace(args=["the", "name", "is", "Heidi"]),
    )

    shutdown.assert_awaited_once()
    assert len(enqueued) == 1
    chat_id, prompt, source, summary = enqueued[0]
    assert chat_id == 42
    assert source == "steer"
    assert "the name is Heidi" in prompt
    assert detached_meta["prompt"] in prompt
    assert summary == "Steer: the name is Heidi"
    assert replies and "Steered" in replies[0]
    assert "idle" not in replies[0].lower()
    assert runtime._user_interrupt["request_id"] == "req-detached"
    assert interrupted and interrupted[0][0].request_id == "req-detached"
    assert interrupted[0][2]["reason"] == "user_steer"


def test_non_generation_background_work_does_not_make_agent_busy():
    runtime = SimpleNamespace(
        current_request_meta=None,
        is_generating=False,
        queue=asyncio.Queue(),
        _background_tasks={object()},
        _background_request_ids=set(),
        _request_meta_by_id={},
    )

    assert runtime_control._agent_is_busy(runtime) is False


def test_mark_and_consume_user_interrupt_matches_request():
    runtime = SimpleNamespace(current_request_meta={"request_id": "req-1"})
    runtime_control.mark_user_interrupt(runtime, "user_stop")
    assert runtime_control.peek_user_interrupt(runtime, "req-1") == "user_stop"
    assert runtime_control.peek_user_interrupt(runtime, "req-other") is None
    assert runtime_control.consume_user_interrupt(runtime, "req-1") == "user_stop"
    assert runtime_control.consume_user_interrupt(runtime, "req-1") is None


def test_stop_interrupt_markers_are_scoped_to_each_detached_request():
    runtime = SimpleNamespace(current_request_meta={"request_id": "req-1"})
    runtime_control.mark_user_interrupt(
        runtime,
        "user_stop",
        request_meta={"request_id": "req-1"},
    )
    runtime_control.mark_user_interrupt(
        runtime,
        "user_stop",
        request_meta={"request_id": "req-2"},
    )

    assert runtime_control.consume_user_interrupt(runtime, "req-1") == "user_stop"
    assert runtime_control.consume_user_interrupt(runtime, "req-2") == "user_stop"


@pytest.mark.asyncio
async def test_stop_preserves_each_active_session_for_explicit_resume(tmp_path):
    shutdown = AsyncMock()
    runtime = SimpleNamespace(
        name="ajiao",
        workspace_dir=tmp_path,
        logger=SimpleNamespace(warning=lambda *a, **k: None),
        config=SimpleNamespace(active_backend="her-v2", engine="her-v2"),
        queue=asyncio.Queue(),
        backend_manager=SimpleNamespace(current_backend=SimpleNamespace(shutdown=shutdown)),
        current_request_meta={
            "request_id": "req-foreground",
            "session_id": "session-a",
            "chat_id": 42,
            "prompt": "foreground work",
            "source": "text",
            "summary": "Foreground",
        },
        _background_request_ids={"req-detached"},
        _request_meta_by_id={
            "req-detached": {
                "request_id": "req-detached",
                "session_id": "session-b",
                "chat_id": 84,
                "prompt": "detached work",
                "source": "text",
                "summary": "Detached",
            }
        },
        last_prompt=None,
        is_generating=True,
        _is_authorized_user=lambda _uid: True,
        _reply_text=AsyncMock(),
        _notify_right_brain_interrupted=lambda *a, **k: None,
    )
    message = SimpleNamespace(text="/stop", chat=SimpleNamespace(id=42))
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=1),
        effective_chat=SimpleNamespace(id=42),
        effective_message=message,
        message=message,
    )

    await runtime_control.cmd_stop(runtime, update, SimpleNamespace(args=[]))

    assert runtime_retry.capture_interrupted_task(
        runtime, session_id="session-a"
    ).request_id == "req-foreground"
    assert runtime_retry.capture_interrupted_task(
        runtime, session_id="session-b"
    ).request_id == "req-detached"


@pytest.mark.asyncio
async def test_stop_interrupts_every_active_session_backend(tmp_path):
    class _Runtime:
        def __getattribute__(self, name):
            if name in runtime_execution.LOCAL_FIELDS:
                found, value = runtime_execution.read_field(self, name)
                if found:
                    return value
            return object.__getattribute__(self, name)

        def __setattr__(self, name, value):
            if (
                name in runtime_execution.LOCAL_FIELDS
                and runtime_execution.write_field(self, name, value)
            ):
                return
            object.__setattr__(self, name, value)

    runtime = _Runtime()
    runtime.name = "ajiao"
    runtime.workspace_dir = tmp_path
    runtime.logger = SimpleNamespace(warning=lambda *a, **k: None)
    runtime.config = SimpleNamespace(active_backend="root", engine="root")
    runtime.queue = asyncio.Queue()
    runtime.backend_manager = SimpleNamespace(
        current_backend=SimpleNamespace(shutdown=AsyncMock())
    )
    runtime.current_request_meta = None
    runtime.last_prompt = None
    runtime.is_generating = False
    runtime._background_request_ids = set()
    runtime._request_meta_by_id = {}
    runtime._is_authorized_user = lambda _uid: True
    runtime._reply_text = AsyncMock()
    runtime._notify_right_brain_interrupted = lambda *a, **k: None

    shutdown_a = AsyncMock()
    shutdown_b = AsyncMock()
    executions = {}
    for session_id, request_id, shutdown in (
        ("session-a", "req-a", shutdown_a),
        ("session-b", "req-b", shutdown_b),
    ):
        execution = runtime_execution.Execution(
            runtime,
            session_id,
            f"sig-{session_id}",
            active_request=request_id,
        )
        execution.values.update(
            config=SimpleNamespace(active_backend=session_id, engine=session_id),
            backend_manager=SimpleNamespace(
                current_backend=SimpleNamespace(shutdown=shutdown)
            ),
            current_request_meta={
                "request_id": request_id,
                "session_id": session_id,
                "chat_id": 42,
                "prompt": f"work for {session_id}",
                "source": "text",
                "summary": session_id,
            },
            is_generating=True,
            last_prompt=None,
            last_response=None,
            _user_interrupt=None,
        )
        executions[session_id] = execution
    runtime._session_executions = executions
    runtime._session_execution_tasks = {}

    message = SimpleNamespace(text="/stop", chat=SimpleNamespace(id=42))
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=1),
        effective_chat=SimpleNamespace(id=42),
        effective_message=message,
        message=message,
    )

    await runtime_control.cmd_stop(runtime, update, SimpleNamespace(args=[]))

    shutdown_a.assert_awaited_once()
    shutdown_b.assert_awaited_once()
    assert runtime_cancel.requested_ids(runtime) >= {"req-a", "req-b"}
    assert executions["session-a"].values["_user_interrupt"]["reason"] == "user_stop"
    assert executions["session-b"].values["_user_interrupt"]["reason"] == "user_stop"
    assert runtime_retry.capture_interrupted_task(
        runtime, session_id="session-a"
    ).request_id == "req-a"
    assert runtime_retry.capture_interrupted_task(
        runtime, session_id="session-b"
    ).request_id == "req-b"


@pytest.mark.asyncio
async def test_stop_job_cancellation_keeps_post_fence_job():
    cancelled: list[str] = []
    records = [
        SimpleNamespace(job_id="job-old", origin={"agent_stop_epoch": 0}),
        SimpleNamespace(job_id="job-new", origin={"agent_stop_epoch": 1}),
    ]

    class _Manager:
        def list(self, **_kwargs):
            return records

        async def cancel(self, job_id, **_kwargs):
            cancelled.append(job_id)

    runtime = SimpleNamespace(name="zelda", background_job_manager=_Manager())

    count = await runtime_control._cancel_managed_background_jobs(
        runtime,
        before_agent_stop_epoch=1,
    )

    assert count == 1
    assert cancelled == ["job-old"]


@pytest.mark.asyncio
async def test_cmd_stop_persists_active_task_before_killing_backend(tmp_path):
    replies: list[str] = []
    shutdown = AsyncMock()

    async def _reply(_update, text, **_kwargs):
        replies.append(text)

    runtime = SimpleNamespace(
        name="ajiao",
        workspace_dir=tmp_path,
        logger=SimpleNamespace(warning=lambda *a, **k: None),
        config=SimpleNamespace(active_backend="her-v2", engine="her-v2"),
        queue=asyncio.Queue(),
        backend_manager=SimpleNamespace(current_backend=SimpleNamespace(shutdown=shutdown)),
        current_request_meta={
            "request_id": "req-health",
            "chat_id": 42,
            "prompt": "Research common illnesses and write a detailed report",
            "source": "text",
            "summary": "Health report",
        },
        last_prompt=None,
        is_generating=True,
        _is_authorized_user=lambda _uid: True,
        _reply_text=_reply,
        _notify_right_brain_interrupted=lambda *a, **k: None,
    )
    message = SimpleNamespace(text="/stop", chat=SimpleNamespace(id=42))
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=1),
        effective_chat=SimpleNamespace(id=42),
        effective_message=message,
        message=message,
    )

    await runtime_control.cmd_stop(runtime, update, SimpleNamespace(args=[]))

    shutdown.assert_awaited_once()
    saved = runtime_retry.capture_interrupted_task(runtime)
    assert saved is not None
    assert saved.request_id == "req-health"
    assert saved.prompt == "Research common illnesses and write a detailed report"
    assert "send “continue”" in replies[0]
    assert runtime._user_interrupt["reason"] == "user_stop"


@pytest.mark.asyncio
async def test_handle_backend_error_suppresses_telegram_on_user_steer():
    """Exit -9 after /steer must not deliver ❌ Backend error to Telegram."""
    from datetime import datetime

    from orchestrator import runtime_pipeline

    sent: list[dict] = []
    logs: list[str] = []
    maintenance: list[tuple] = []
    listeners: list[dict] = []

    async def _send_long_message(**kwargs):
        sent.append(kwargs)
        return 0.0, 1

    runtime = SimpleNamespace(
        config=SimpleNamespace(active_backend="grok-cli"),
        logger=SimpleNamespace(info=lambda msg, *a, **k: logs.append(str(msg))),
        error_logger=SimpleNamespace(error=lambda msg, *a, **k: logs.append(f"ERR:{msg}")),
        _mark_error=lambda err: setattr(runtime, "marked_error", err),
        _should_buffer_during_transfer=lambda _rid: False,
        _record_suppressed_transfer_result=lambda *a, **k: None,
        _notify_request_listeners=AsyncMock(side_effect=lambda _rid, payload: listeners.append(payload)),
        _should_retry_codex_scheduler_failure=lambda *a, **k: False,
        send_long_message=_send_long_message,
        _log_maintenance=lambda item, event, **fields: maintenance.append((event, fields)),
        _user_interrupt={"reason": "user_steer", "request_id": "req-0001", "at": 0.0},
    )
    item = SimpleNamespace(
        request_id="req-0001",
        chat_id=42,
        source="text",
        summary="Test",
        silent=False,
        deliver_to_telegram=True,
    )
    response = SimpleNamespace(error="Grok CLI exited with code -9")

    await runtime_pipeline.handle_backend_error(
        runtime,
        item,
        response,
        queued_at=datetime.now(),
        queue_wait_s=0.1,
        backend_elapsed_s=1.0,
    )

    assert sent == [], "Telegram ❌ Backend error must be suppressed for /steer"
    assert not hasattr(runtime, "marked_error"), "must not mark real error state"
    assert maintenance and maintenance[0][0] == "user_interrupt"
    assert listeners and listeners[0].get("interrupted") is True
    assert listeners[0].get("interrupt_reason") == "user_steer"
    assert any("Suppressed backend exit" in m for m in logs)
    assert not any(m.startswith("ERR:") for m in logs)
    assert getattr(runtime, "_user_interrupt", None) is None
