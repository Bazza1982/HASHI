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
from orchestrator.session_store import SessionStore
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
    server.global_config = SimpleNamespace(
        authorized_id=7, deployment_profile="personal",
    )
    server.session_store = SessionStore(
        tmp_path / "sessions.sqlite3", instance_id="fixture",
    )
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
    # A cold projection may finish at the deadline already in the correct
    # state. Inspect that completed observation before deciding to poll again.
    while not predicate(last):
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError(f"Worker list projection did not converge: {last!r}")
        await asyncio.sleep(0.01)
        last = await _agent_projection(stack)
    return last


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
@pytest.mark.parametrize("outbound,inbound,app_running,current_saved_bot,ready", [
    (False, True, False, True, False),
    (False, True, True, True, False),
    (True, False, True, True, False),
    (True, True, True, True, True),
    (True, True, False, True, False),
    (True, True, True, False, False),
])
async def test_ingress_rpc_preserves_outbound_readiness_for_saved_bot(
    tmp_path, outbound, inbound, app_running, current_saved_bot, ready,
):
    from orchestrator.config_json import write_config_json
    from orchestrator.telegram_credential import TelegramCredentialService
    from orchestrator.telegram_delivery_state import telegram_bot_fingerprint

    stack = await _build_stack(tmp_path, outcome="success")
    token = "123456:TEST_ONLY_CURRENT_BOT_abcdefghijklmnopqrstuvwxyz"
    saved_token = token if current_saved_bot else "654321:TEST_ONLY_OTHER_BOT_abcdefghijklmnopqrstuvwxyz"
    fingerprint = telegram_bot_fingerprint(token)
    runtime = stack.runtime
    runtime.token = token
    runtime.app = SimpleNamespace(running=app_running)
    runtime.telegram_connected = outbound
    stack.handle.kernel.function_workers = SimpleNamespace(
        telegram_ingress_snapshot=lambda _: {
            "running": True, "connected": inbound, "bot_fingerprint": fingerprint,
        },
    )
    config_path, secrets_path = tmp_path / "agents.json", tmp_path / "secrets.json"
    write_config_json(config_path, {
        "global": {"authorized_id": 42},
        "agents": [{"name": runtime.name, "telegram_token_key": "test.telegram"}],
    })
    write_config_json(secrets_path, {"test.telegram": saved_token})
    service = TelegramCredentialService(config_path=config_path, secrets_path=secrets_path)
    try:
        # Receiver transitions cannot enable an unprepared application, disable
        # a healthy sender, or make an old Bot ready for a newly saved token.
        observations = []
        for receiver_connected in (False, True, False, inbound):
            result = await stack.host.handle_request("worker.telegram_status", {
                "connected": receiver_connected,
            })
            observations.append((runtime.telegram_connected, result["metadata"]))
            stack.handle.update_metadata(stack.client, result["metadata"])
        metadata = stack.handle.get_runtime_metadata()
        status = service.status(runtime.name, runtime_active=True, runtime_metadata=metadata)
        assert status["bidirectional_ready"] is ready
        assert status["outbound_connected"] is bool(outbound and app_running and current_saved_bot)
        assert status["inbound_connected"] is bool(inbound and current_saved_bot)
        assert (status["activation_status"] == "connected") is ready
        assert token not in json.dumps(status)
        for receiver_connected, (application_ready, worker_metadata) in zip(
            (False, True, False, inbound), observations,
        ):
            assert application_ready is outbound
            assert worker_metadata["telegram_outbound_connected"] is bool(outbound and app_running)
            assert worker_metadata["telegram_ingress_connected"] is receiver_connected
    finally:
        await _close_stack(stack)


@pytest.mark.asyncio
async def test_blank_bot_reference_reaches_worker_ingress_and_ack(tmp_path, monkeypatch):
    """Exercise actual config and Host preparation, Worker IPC, poll and ACK."""
    from telegram import Bot

    from orchestrator import flexible_agent_runtime, function_worker_host
    from orchestrator.config import ConfigManager
    from orchestrator.config_json import write_config_json
    from orchestrator.telegram_credential import TelegramCredentialService
    from orchestrator.telegram_ingress import CoreTelegramIngress
    from tests.test_config import _minimal_flex_agent, _write_base_files

    cfg_root = tmp_path / "config"
    cfg_root.mkdir()
    agent = _minimal_flex_agent()
    agent.update(name="zelda", telegram_token_key="", workspace_dir="workspaces/zelda",
                 system_md="workspaces/zelda/agent.md")
    config_path, secrets_path = _write_base_files(cfg_root, agent)
    document = json.loads(config_path.read_text(encoding="utf-8"))
    document["global"]["authorized_id"] = 42
    write_config_json(config_path, document)
    token = "123456:TEST_ONLY_CURRENT_BOT_abcdefghijklmnopqrstuvwxyz"
    write_config_json(secrets_path, {"authorized_telegram_id": 42, "zelda": token})
    _, agents, secrets = ConfigManager(config_path, secrets_path, bridge_home=cfg_root).load()
    loaded = agents[0]
    assert loaded.telegram_token_key == "zelda"

    stack = await _build_stack(tmp_path, outcome="success")
    supervisor = stack.client.supervisor
    kernel = stack.handle.kernel
    kernel.function_workers = supervisor
    kernel.secrets = secrets
    kernel.paths = SimpleNamespace(config_path=config_path, secrets_path=secrets_path, bridge_home=cfg_root)
    stack.worker_peer.request_handler = stack.host.handle_request
    runtime = stack.runtime

    # Execute the real Host's config loading and token selection, while keeping
    # backend initialization and network-facing runtime services isolated.
    def construct_runtime(agent_config, global_config, worker_token, worker_secrets, _skills):
        runtime.config = agent_config
        runtime.global_config = global_config
        runtime.token = worker_token
        runtime.secrets = worker_secrets
        return runtime

    async def initialize_runtime():
        return True

    monkeypatch.setattr(flexible_agent_runtime, "FlexibleAgentRuntime", construct_runtime)
    monkeypatch.setattr(function_worker_host, "current_process_runtime", lambda: None)
    actual = SimpleNamespace(
        worker_protocol=function_worker_host.FUNCTION_WORKER_PROTOCOL_VERSION,
        generation_schema=function_worker_host.FUNCTION_GENERATION_SCHEMA_VERSION,
        runtime_id="runtime-test",
    )
    monkeypatch.setattr(function_worker_host, "enforce_runtime_contract", lambda *_args: actual)
    monkeypatch.setattr(function_worker_host, "compare_runtime_fingerprints", lambda *_args: None)
    runtime.initialize = initialize_runtime
    runtime.bind_handlers = lambda: None
    stack.host.bootstrap = {
        "protocol": function_worker_host.FUNCTION_WORKER_PROTOCOL_VERSION, "topology": {},
    }
    stack.host.code_root = Path(__file__).resolve().parents[1]
    stack.host.bridge_home = cfg_root
    stack.host.generation_root = stack.host.code_root
    stack.host.expected_runtime = actual
    stack.host._install_generation_import_paths = lambda: None
    try:
        await stack.host.prepare()
        assert runtime.token == token
        assert runtime.config.telegram_token_key == "zelda"
    except BaseException:
        await _close_stack(stack)
        raise
    # The fixture supplies an already-committed active route for the ingress.
    stack.host.phase = "ACTIVE"
    stack.host.accepting = True
    runtime.telegram_connected = True
    runtime._telegram_scoped_access_enabled = False
    acknowledged = asyncio.Event()
    continued = asyncio.Event()
    release_poll = asyncio.Event()
    payload = {
        "update_id": 7,
        "message": {
            "message_id": 1, "date": 1700000000, "text": "test message",
            "chat": {"id": 42, "type": "private"},
            "from": {"id": 42, "is_bot": False, "first_name": "Test"},
        },
    }
    received = []

    async def process_update(update):
        received.append((update.update_id, update.effective_user.id))
        acknowledged.set()

    runtime.app = SimpleNamespace(running=True, bot=Bot(token=token), process_update=process_update)
    stack.handle.update_metadata(stack.client, stack.host.metadata())

    poll_calls = []

    class PollBot:
        id = 123456

        async def initialize(self):
            pass

        async def delete_webhook(self, *, drop_pending_updates):
            assert not drop_pending_updates

        async def get_updates(self, **kwargs):
            poll_calls.append(kwargs["offset"])
            if kwargs["offset"] is None:
                return [SimpleNamespace(update_id=7, to_dict=lambda: payload)]
            assert kwargs["offset"] == 8
            continued.set()
            await release_poll.wait()
            return []

        async def shutdown(self):
            pass

    from orchestrator import function_worker_supervisor
    kernel.global_cfg = runtime.global_config
    monkeypatch.setattr(function_worker_supervisor, "CoreTelegramIngress",
        lambda **kwargs: CoreTelegramIngress(**kwargs, bot_factory=lambda _token: PollBot()))
    service = TelegramCredentialService(
        config_path=config_path, secrets_path=secrets_path, state_root=cfg_root,
    )
    try:
        before = service.status("zelda", runtime_active=True,
                                runtime_metadata=stack.handle.get_runtime_metadata())
        assert before["configured"] is True
        assert before["bidirectional_ready"] is False
        assert await supervisor.start_telegram_ingress("zelda", runtime.token, drop_pending_updates=False)
        ingress = supervisor._telegram_ingress["zelda"]
        await asyncio.wait_for(acknowledged.wait(), timeout=5)
        await asyncio.wait_for(continued.wait(), timeout=5)
        after = service.status("zelda", runtime_active=True,
                               runtime_metadata=stack.handle.get_runtime_metadata())
        assert received == [(7, 42)]
        assert ingress.offset == 8
        assert after["activation_status"] == "connected"
        assert after["bidirectional_ready"] is True
        assert token not in json.dumps(after)
        before_summary_polls = list(poll_calls)
        for _ in range(3):
            summary = await runtime.orchestrator.telegram_summary("user:42", 42)
            assert summary["configured_verified"] == 1
            assert summary["ready"] == 1
            assert summary["mirror_active"] == 1
            assert token not in json.dumps(summary)
        assert poll_calls == before_summary_polls
        denied = await runtime.orchestrator.telegram_summary("user:42", 99)
        assert denied["configured_verified"] == 0
    finally:
        release_poll.set()
        await supervisor.stop_telegram_ingress("zelda")
        await _close_stack(stack)


@pytest.mark.asyncio
async def test_worker_projects_revisioned_workzone_support(tmp_path):
    stack = await _build_stack(tmp_path, outcome="success")
    try:
        await stack.host.emit_metadata()
        old = await _wait_for_projection(stack, lambda row: row.get("workzone_revisioned_profile") is False)
        assert old["workzone_revisioned_profile"] is False
        stack.runtime.session_store = SimpleNamespace(
            get_agent_workzone_set=lambda **_kwargs: None,
            set_agent_workzone_slot=lambda **_kwargs: None,
            delete_agent_workzone_slot=lambda **_kwargs: None,
        )
        stack.runtime._sync_workzone_to_backend_config = lambda: None
        await stack.host.emit_metadata()
        supported = await _wait_for_projection(stack, lambda row: row.get("workzone_revisioned_profile") is True)
        assert supported["workzone_revisioned_profile"] is True
    finally:
        await _close_stack(stack)


@pytest.mark.asyncio
async def test_worker_reports_scoped_telegram_access_capability(tmp_path):
    stack = await _build_stack(tmp_path, outcome="success")
    try:
        await stack.host.emit_metadata()
        deadline = asyncio.get_running_loop().time() + 1
        while (
            stack.handle.get_runtime_metadata().get("telegram_scoped_access_protocol") != 2
            and asyncio.get_running_loop().time() < deadline
        ):
            await asyncio.sleep(0.01)
        assert stack.handle.get_runtime_metadata()["telegram_scoped_access_protocol"] == 2
    finally:
        await _close_stack(stack)


@pytest.mark.asyncio
async def test_worker_projects_guarded_parked_store_capability(tmp_path):
    from orchestrator.parked_topics import ParkedTopicStore

    stack = await _build_stack(tmp_path, outcome="success")
    try:
        await stack.host.emit_metadata()
        first = await _wait_for_projection(
            stack, lambda row: row.get("parked_topic_guarded_writes") is False,
        )
        assert first["parked_topic_guarded_writes"] is False
        stack.runtime.parked_topics = ParkedTopicStore(stack.runtime.workspace_dir)
        await stack.host.emit_metadata()
        ready = await _wait_for_projection(
            stack, lambda row: row.get("parked_topic_guarded_writes") is True,
        )
        assert ready["parked_topic_guarded_writes"] is True
    finally:
        await _close_stack(stack)


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
# HASHI-Fingerprint: hfp_296da2898592479ca078e3a633b8ff97
