from __future__ import annotations

import asyncio
import json
import multiprocessing
import os
from contextlib import suppress
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestrator import (
    runtime_background_status,
    runtime_delivery_order,
    runtime_lifecycle,
    runtime_pipeline,
    runtime_session,
    terminal_console,
)
from orchestrator.function_worker_host import FunctionWorkerHost
from orchestrator.function_worker_protocol import JsonConnectionPeer
from orchestrator.function_worker_supervisor import (
    AgentRuntimeHandle,
    FunctionWorkerClient,
    FunctionWorkerSupervisor,
)
from orchestrator.request_activity import RequestActivityStore
from orchestrator.runtime_common import QueuedRequest
from orchestrator.workbench_api import WorkbenchApiServer


class _Logger:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def _record(self, message: object, *args: object) -> None:
        rendered = str(message)
        if args:
            with suppress(TypeError):
                rendered = rendered % args
        self.messages.append(rendered)

    debug = _record
    info = _record
    warning = _record
    error = _record
    exception = _record


class _AliveProcess:
    exitcode = None

    def __init__(self) -> None:
        self.pid = os.getpid()

    @staticmethod
    def is_alive() -> bool:
        return True


class _Kernel:
    def __init__(self) -> None:
        self.runtimes: list[AgentRuntimeHandle] = []
        self._startup_tasks: dict[str, object] = {}
        self.runtime_fingerprint = SimpleNamespace(
            runtime_id="runtime-test",
            python="3.12.13",
            platform_abi="cp312",
            core_api=3,
            function_api=3,
            dependency_digest="deps",
            core_source_digest="core",
        )

    def _runtime_map(self) -> dict[str, AgentRuntimeHandle]:
        return {runtime.name: runtime for runtime in self.runtimes}


class _BackendManager:
    def __init__(self, outcome: str) -> None:
        self.outcome = outcome
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.current_backend = SimpleNamespace()
        self.agent_mode = "flex"

    async def generate_response(self, *_args, **_kwargs):
        self.entered.set()
        await self.release.wait()
        if self.outcome == "failure":
            return SimpleNamespace(is_success=False, text="", error="backend failed")
        return SimpleNamespace(is_success=True, text="done", error="")


def _runtime(tmp_path: Path, *, outcome: str) -> SimpleNamespace:
    logger = _Logger()
    runtime = SimpleNamespace(
        name="zelda",
        display_name="Zelda",
        workspace_dir=tmp_path / "workspace",
        media_dir=tmp_path / "media",
        transcript_log_path=tmp_path / "workspace" / "transcript.jsonl",
        core_transcript_log_path=tmp_path / "workspace" / "core_transcript.jsonl",
        session_id_dt="session-test",
        startup_success=True,
        backend_ready=True,
        telegram_connected=True,
        org_id=None,
        queue=asyncio.Queue(),
        request_activity=RequestActivityStore(logger=logger),
        current_request_meta=None,
        is_generating=False,
        last_prompt=None,
        _request_meta_by_id={},
        _background_request_ids=set(),
        _verbose=False,
        _meter=False,
        _think=False,
        logger=logger,
        error_logger=logger,
        config=SimpleNamespace(
            active_backend="test-backend",
            type="flex",
            allowed_backends=[],
            extra={},
        ),
        backend_manager=_BackendManager(outcome),
    )
    runtime.workspace_dir.mkdir()
    runtime.media_dir.mkdir()
    runtime.get_runtime_metadata = lambda: {
        "id": runtime.name,
        "name": runtime.name,
        "display_name": runtime.display_name,
        "emoji": "👑",
        "engine": runtime.config.active_backend,
        "active_backend": runtime.config.active_backend,
        "model": "test-model",
        "provider": "test-provider",
        "workspace_dir": str(runtime.workspace_dir),
        "transcript_path": str(runtime.transcript_log_path),
        "online": True,
        "status": "online",
        "type": runtime.config.type,
        "telegram_connected": True,
    }
    runtime._primary_chat_id = lambda: 7
    runtime.has_active_transfer = lambda: False
    runtime._mark_activity = lambda: None
    runtime._mark_error = lambda error: setattr(runtime, "last_error", error)
    runtime._log_maintenance = lambda *_args, **_kwargs: None
    runtime._remote_backend_block_reason = lambda _source: None
    runtime._notify_right_brain_started = lambda *_args, **_kwargs: None
    runtime._notify_right_brain_completed = lambda *_args, **_kwargs: None
    runtime._notify_right_brain_interrupted = lambda *_args, **_kwargs: None
    runtime._audit_enabled = lambda: False

    async def notify_request_listeners(request_id: str, payload: dict) -> None:
        runtime.request_activity.complete(
            request_id,
            success=bool(payload.get("success")),
            error=payload.get("error") or "",
        )

    runtime._notify_request_listeners = notify_request_listeners
    return runtime


def _request() -> QueuedRequest:
    return QueuedRequest(
        request_id="req-metadata-1",
        chat_id=7,
        prompt="exercise metadata lifecycle",
        source="api",
        summary="metadata lifecycle",
        created_at=datetime.now().isoformat(),
        silent=True,
    )


def _install_lifecycle_boundaries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runtime_session, "apply_item_workzone", lambda *_args: None)
    monkeypatch.setattr(
        runtime_session, "activate_backend_binding", lambda *_args: None
    )
    monkeypatch.setattr(runtime_session, "mark_running", lambda *_args: None)
    monkeypatch.setattr(
        terminal_console, "start_request", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(terminal_console, "observe_exception", lambda *_args: None)
    monkeypatch.setattr(
        terminal_console, "finish_request", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(runtime_background_status, "prepare", lambda *_args: None)

    async def complete_turn(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(runtime_delivery_order, "complete_turn", complete_turn)
    monkeypatch.setattr(
        runtime_pipeline,
        "clear_context_compaction_request_state",
        lambda *_args: None,
    )

    async def build_turn_prompt(*_args, **_kwargs):
        return runtime_pipeline.TurnPrompt(
            effective_prompt="effective",
            final_prompt="final",
            extra_sections=[],
            incremental=False,
            prompt_audit={},
        )

    monkeypatch.setattr(runtime_pipeline, "build_turn_prompt", build_turn_prompt)
    monkeypatch.setattr(
        runtime_pipeline,
        "surface_context_compaction_warnings",
        lambda *_args: None,
    )

    async def setup_feedback(*_args, **_kwargs):
        return runtime_pipeline.InteractiveFeedback(
            stop_typing=None,
            typing_task=None,
            escalation_task=None,
            answer_preview_task=None,
            answer_stream_state=None,
            placeholder=None,
            stream_callback=None,
            think_flush_task=None,
            on_stream_event=None,
            her_message_router=None,
        )

    monkeypatch.setattr(runtime_pipeline, "setup_interactive_feedback", setup_feedback)

    async def cleanup_feedback(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(
        runtime_pipeline, "cleanup_interactive_feedback", cleanup_feedback
    )

    async def no_recovery(*_args, **_kwargs):
        return None

    monkeypatch.setattr(
        runtime_pipeline,
        "recover_typed_context_capacity_rejection",
        no_recovery,
    )
    monkeypatch.setattr(
        runtime_pipeline, "log_backend_finished", lambda *_args, **_kwargs: None
    )

    async def prepare_success(runtime, item, **_kwargs):
        await runtime._notify_request_listeners(
            item.request_id,
            {"request_id": item.request_id, "success": True, "error": ""},
        )
        return runtime_pipeline.SuccessfulResponse(
            display_text="done",
            visible_text="done",
            wrapper_result=None,
        )

    monkeypatch.setattr(
        runtime_pipeline, "prepare_successful_response", prepare_success
    )
    monkeypatch.setattr(
        runtime_pipeline,
        "record_foreground_usage_audit",
        lambda *_args, **_kwargs: None,
    )

    async def handle_backend_error(runtime, item, response, **_kwargs) -> None:
        await runtime._notify_request_listeners(
            item.request_id,
            {
                "request_id": item.request_id,
                "success": False,
                "error": response.error,
            },
        )

    monkeypatch.setattr(runtime_pipeline, "handle_backend_error", handle_backend_error)
    monkeypatch.setattr(
        "orchestrator.runtime_control.consume_user_interrupt",
        lambda *_args, **_kwargs: None,
    )


async def _build_stack(tmp_path: Path, *, outcome: str):
    runtime = _runtime(tmp_path, outcome=outcome)
    kernel = _Kernel()
    supervisor = FunctionWorkerSupervisor(kernel)
    core_connection, worker_connection = multiprocessing.Pipe(duplex=True)
    generation_id = "sha256:" + "a" * 64
    generation = SimpleNamespace(manifest=SimpleNamespace(generation_id=generation_id))
    client = FunctionWorkerClient(
        supervisor=supervisor,
        agent_name=runtime.name,
        generation=generation,
        generation_root=tmp_path,
        nonce="nonce-test",
        process=_AliveProcess(),
        connection=core_connection,
    )
    client.peer.start()
    initial = {
        **runtime.get_runtime_metadata(),
        "worker_pid": client.pid,
        "worker_phase": "ACTIVE",
        "worker_accepting": True,
        "is_generating": False,
        "queue_depth": 0,
        "current_request_meta": {},
    }
    handle = AgentRuntimeHandle(kernel, client, initial)
    kernel.runtimes.append(handle)
    worker_peer = JsonConnectionPeer(worker_connection, label="worker-metadata-test")
    worker_peer.start()
    host = FunctionWorkerHost.__new__(FunctionWorkerHost)
    host.runtime = runtime
    host.peer = worker_peer
    host.agent_name = runtime.name
    host.phase = "ACTIVE"
    host.accepting = True
    host.started_at = "2026-09-10T00:00:00+10:00"
    host.adopted_at = "2026-09-10T00:00:01+10:00"
    host.nonce = "nonce-test"
    host.manifest = SimpleNamespace(generation_id=generation_id, entries=())
    host.expected_runtime = SimpleNamespace(runtime_id="runtime-test")
    host._bridge_request_completions(runtime)

    server = WorkbenchApiServer.__new__(WorkbenchApiServer)
    server._runtime_map = lambda: {runtime.name: handle}
    server._load_agent_rows = lambda *, include_inactive=False: [
        {"name": runtime.name, "is_active": True}
    ]
    server._is_governed_profile = lambda: False
    return SimpleNamespace(
        runtime=runtime,
        host=host,
        handle=handle,
        client=client,
        worker_peer=worker_peer,
        server=server,
    )


async def _agent_projection(stack) -> dict:
    response = await stack.server.handle_agents(SimpleNamespace(query={}))
    return json.loads(response.text)["agents"][0]


async def _wait_for_projection(stack, predicate, *, timeout: float = 0.75) -> dict:
    deadline = asyncio.get_running_loop().time() + timeout
    last = await _agent_projection(stack)
    while asyncio.get_running_loop().time() < deadline:
        if predicate(last):
            return last
        await asyncio.sleep(0.01)
        last = await _agent_projection(stack)
    raise AssertionError(f"Worker list projection did not converge: {last!r}")


async def _close_stack(stack, process_task: asyncio.Task | None = None) -> None:
    if process_task is not None and not process_task.done():
        process_task.cancel()
        with suppress(asyncio.CancelledError):
            await process_task
    await asyncio.gather(
        stack.worker_peer.close(),
        stack.client.peer.close(),
        return_exceptions=True,
    )


@pytest.mark.asyncio
async def test_worker_projection_reports_actual_agent_mode(tmp_path):
    stack = await _build_stack(tmp_path, outcome="success")
    try:
        stack.runtime.backend_manager.agent_mode = "fixed"
        await stack.host.emit_metadata()
        projection = await _wait_for_projection(
            stack,
            lambda row: row.get("mode") == "fixed",
        )
        assert projection["mode"] == "fixed"
    finally:
        await _close_stack(stack)


@pytest.mark.asyncio
async def test_real_queue_start_updates_worker_list_projection(tmp_path, monkeypatch):
    _install_lifecycle_boundaries(monkeypatch)
    stack = await _build_stack(tmp_path, outcome="success")
    process_task = None
    try:
        item = _request()
        stack.runtime.request_activity.start(item.request_id, source=item.source)
        await stack.runtime.queue.put(item)
        await stack.host.emit_metadata()
        queued = await _wait_for_projection(
            stack,
            lambda row: row.get("queue_depth") == 1,
        )
        assert queued["is_generating"] is False

        process_task = asyncio.create_task(
            runtime_lifecycle.process_queue(stack.runtime)
        )
        await asyncio.wait_for(stack.runtime.backend_manager.entered.wait(), 1)

        running = await _wait_for_projection(
            stack,
            lambda row: row.get("is_generating") is True,
        )
        assert running["queue_depth"] == 0
        assert running["current_request_meta"]["request_id"] == item.request_id
    finally:
        stack.runtime.backend_manager.release.set()
        await _close_stack(stack, process_task)


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["success", "failure"])
async def test_real_request_completion_clears_worker_list_projection(
    tmp_path,
    monkeypatch,
    outcome,
):
    _install_lifecycle_boundaries(monkeypatch)
    stack = await _build_stack(tmp_path, outcome=outcome)
    process_task = None
    try:
        item = _request()
        stack.runtime.request_activity.start(item.request_id, source=item.source)
        await stack.runtime.queue.put(item)
        process_task = asyncio.create_task(
            runtime_lifecycle.process_queue(stack.runtime)
        )
        await asyncio.wait_for(stack.runtime.backend_manager.entered.wait(), 1)
        await stack.host.emit_metadata()
        await _wait_for_projection(
            stack,
            lambda row: row.get("is_generating") is True,
        )

        stack.runtime.backend_manager.release.set()
        await asyncio.wait_for(stack.runtime.queue.join(), 1)

        finished = await _wait_for_projection(
            stack,
            lambda row: (
                row.get("is_generating") is False
                and row.get("queue_depth") == 0
                and not row.get("current_request_meta")
            ),
        )
        assert finished["current_request_meta"] == {}
        activity = stack.runtime.request_activity.poll(item.request_id)
        assert activity["terminal"] is True
        assert activity["success"] is (outcome == "success")
    finally:
        await _close_stack(stack, process_task)


@pytest.mark.asyncio
async def test_real_request_cancellation_clears_worker_list_projection(
    tmp_path,
    monkeypatch,
):
    _install_lifecycle_boundaries(monkeypatch)
    stack = await _build_stack(tmp_path, outcome="success")
    process_task = None
    try:
        item = _request()
        stack.runtime.request_activity.start(item.request_id, source=item.source)
        await stack.runtime.queue.put(item)
        process_task = asyncio.create_task(
            runtime_lifecycle.process_queue(stack.runtime)
        )
        await asyncio.wait_for(stack.runtime.backend_manager.entered.wait(), 1)
        await stack.host.emit_metadata()
        await _wait_for_projection(
            stack,
            lambda row: row.get("is_generating") is True,
        )

        process_task.cancel()
        await asyncio.wait_for(process_task, 1)

        cancelled = await _wait_for_projection(
            stack,
            lambda row: (
                row.get("is_generating") is False
                and row.get("queue_depth") == 0
                and not row.get("current_request_meta")
            ),
        )
        assert cancelled["current_request_meta"] == {}
    finally:
        await _close_stack(stack, process_task)
