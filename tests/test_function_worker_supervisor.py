from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import multiprocessing
from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestrator.function_generation import (
    CandidateProbeReceipt,
    SourceEntry,
    VerifiedFunctionGeneration,
    build_source_manifest_from_entries,
    build_source_manifest,
    verify_qualified_manifest_bytes,
)
from orchestrator.function_worker_protocol import (
    FunctionWorkerDisconnected,
    FunctionWorkerRemoteError,
    JsonConnectionPeer,
)
from orchestrator.function_worker_features import WORKER_LOG_RELAY_FEATURE
from orchestrator.function_worker_supervisor import (
    AgentRuntimeHandle,
    FunctionWorkerError,
    FunctionWorkerSupervisor,
    load_qualified_generation_cache,
    materialize_generation_artifact,
    persist_qualified_generation_cache,
    verify_generation_artifact,
)
from orchestrator.function_worker_host import FunctionWorkerHost, WorkerSchedulerFacade
from orchestrator.runtime_contract import (
    current_runtime_fingerprint,
    load_runtime_policy,
)
from orchestrator.scheduler import TaskScheduler

ROOT = Path(__file__).resolve().parents[1]


class _Process:
    def __init__(self, pid: int, *, alive: bool = True) -> None:
        self.pid = pid
        self.alive = alive
        self.exitcode = None if alive else 1

    def is_alive(self) -> bool:
        return self.alive


class _Generation:
    def __init__(self, marker: str = "a") -> None:
        self.manifest = SimpleNamespace(generation_id="sha256:" + marker * 64)


class _Client:
    def __init__(
        self,
        name: str,
        pid: int,
        *,
        generation: _Generation | None = None,
        alive: bool = True,
        responder=None,
    ) -> None:
        self.agent_name = name
        self.process = _Process(pid, alive=alive)
        self.generation = generation or _Generation()
        self.generation_root = Path("generation")
        self.responder = responder
        self.calls: list[str] = []

    @property
    def pid(self) -> int:
        return self.process.pid

    @property
    def generation_id(self) -> str:
        return self.generation.manifest.generation_id

    async def call(self, method, params=None, **_kwargs):
        self.calls.append(method)
        if self.responder is not None:
            result = self.responder(method, params)
            if asyncio.iscoroutine(result):
                return await result
            return result
        if method == "runtime.enqueue_api_text":
            return f"request-{self.pid}"
        return {"ok": True}


class _Kernel:
    def __init__(self) -> None:
        self.runtimes: list[AgentRuntimeHandle] = []
        self._startup_tasks = {}
        self.runtime_fingerprint = SimpleNamespace(
            runtime_id="core-runtime",
            python="3.12.13",
            platform_abi="cp312",
            core_api=2,
            function_api=2,
            dependency_digest="deps",
            core_source_digest="core",
        )
        self.function_generation = {}

    def _runtime_map(self):
        return {handle.name: handle for handle in self.runtimes}


def _metadata(name: str, pid: int) -> dict:
    return {
        "name": name,
        "worker_pid": pid,
        "worker_phase": "ACTIVE",
        "worker_accepting": True,
        "startup_success": True,
        "backend_ready": True,
        "online": True,
        "telegram_connected": False,
    }


def _handle(kernel: _Kernel, client: _Client) -> AgentRuntimeHandle:
    return AgentRuntimeHandle(kernel, client, _metadata(client.agent_name, client.pid))


def _scheduler_rpc_stack(tmp_path: Path, *, worker_agent: str):
    kernel = _Kernel()
    state_path = tmp_path / "scheduler_state.json"
    kernel.scheduler = TaskScheduler(
        tasks_path=tmp_path / "tasks.json",
        state_path=state_path,
        runtimes=[],
        authorized_id=7,
    )
    supervisor = FunctionWorkerSupervisor(kernel)
    client = _Client(worker_agent, 101)
    core_connection, worker_connection = multiprocessing.Pipe(duplex=True)

    async def handle_request(method, params):
        return await supervisor.handle_worker_request(client, method, params)

    core_peer = JsonConnectionPeer(
        core_connection,
        label="scheduler-core",
        request_handler=handle_request,
    )
    worker_peer = JsonConnectionPeer(worker_connection, label="scheduler-worker")
    core_peer.start()
    worker_peer.start()
    facade = WorkerSchedulerFacade(worker_peer, worker_agent)
    return kernel.scheduler, state_path, facade, core_peer, worker_peer


@pytest.mark.asyncio
async def test_worker_scheduler_facade_creates_via_rpc_in_scheduler_owned_state(
    tmp_path,
):
    scheduler, state_path, facade, core_peer, worker_peer = _scheduler_rpc_stack(
        tmp_path,
        worker_agent="zelda",
    )
    try:
        record = await facade.schedule_delayed_message(
            agent_name="zelda",
            chat_id=42,
            prompt="follow up later",
            delay_minutes=5,
            idempotency_key="delay-request-1",
            request_metadata={"session_id": "session-1", "surface": "telegram"},
            deliver_to_telegram=False,
        )

        persisted = json.loads(state_path.read_text(encoding="utf-8"))
        assert persisted["delayed_messages"][record["id"]] == record
        assert record["agent"] == "zelda"
        assert record["chat_id"] == 42
        assert record["prompt"] == "follow up later"
        assert record["due_at"] - record["created_at"] == 300
        assert record["request_metadata"] == {
            "session_id": "session-1",
            "surface": "telegram",
        }
        assert record["deliver_to_telegram"] is False
        assert await scheduler.list_delayed_messages("zelda") == [record]
    finally:
        await asyncio.gather(core_peer.close(), worker_peer.close())


@pytest.mark.asyncio
async def test_worker_scheduler_rpc_rejects_cross_agent_creation_without_state_write(
    tmp_path,
):
    scheduler, state_path, facade, core_peer, worker_peer = _scheduler_rpc_stack(
        tmp_path,
        worker_agent="zelda",
    )
    try:
        with pytest.raises(
            FunctionWorkerRemoteError,
            match="Worker 'zelda' cannot access Scheduler state for Agent 'sunny'",
        ):
            await facade.schedule_delayed_message(
                agent_name="sunny",
                chat_id=42,
                prompt="must not cross the Worker boundary",
                delay_minutes=5,
            )

        assert await scheduler.list_delayed_messages("zelda") == []
        assert await scheduler.list_delayed_messages("sunny") == []
        assert not state_path.exists()
    finally:
        await asyncio.gather(core_peer.close(), worker_peer.close())


@pytest.mark.asyncio
async def test_shutdown_status_change_does_not_call_exiting_worker():
    kernel = _Kernel()
    kernel.is_stopping = True
    client = _Client("alpha", 101)
    handle = _handle(kernel, client)
    kernel.runtimes.append(handle)
    supervisor = FunctionWorkerSupervisor(kernel)

    await supervisor.set_worker_telegram_status("alpha", False)

    assert client.calls == []
    assert handle.metadata["telegram_connected"] is False


@pytest.mark.asyncio
async def test_runtime_status_failures_are_aggregated_once(
    monkeypatch,
    caplog,
):
    def fail_status(_method, _params):
        raise ConnectionResetError("worker channel reset")

    kernel = _Kernel()
    kernel.global_cfg = SimpleNamespace(instance_id="HASHI2")
    alpha = _handle(kernel, _Client("alpha", 101, responder=fail_status))
    beta = _handle(kernel, _Client("beta", 202, responder=fail_status))
    kernel.runtimes.extend((alpha, beta))
    supervisor = FunctionWorkerSupervisor(kernel)
    monkeypatch.setattr(
        "orchestrator.function_worker_supervisor.TELEGRAM_STATUS_WARNING_DEBOUNCE_SECONDS",
        0.0,
    )

    with caplog.at_level(logging.WARNING, logger="BridgeU.Orchestrator"):
        await asyncio.gather(
            supervisor.set_worker_telegram_status("alpha", False),
            supervisor.set_worker_telegram_status("beta", False),
        )
        await supervisor._telegram_status_warning_task

    messages = [
        record.message
        for record in caplog.records
        if record.name == "BridgeU.Orchestrator"
        and "Telegram status synchronisation" in record.message
    ]
    assert len(messages) == 1
    assert "2 Function Worker(s): alpha, beta" in messages[0]
    assert "Worker IPC channels (ConnectionResetError)" in messages[0]
    assert "does not itself stop an active task" in messages[0]
    assert "Diagnostic code: telegram_status_channel_disconnected" in messages[0]


@pytest.mark.asyncio
async def test_core_proxy_routes_external_stop_through_worker_control_protocol():
    observed = {}

    def responder(method, params):
        observed["method"] = method
        observed["params"] = dict(params or {})
        return {"ok": True, "command": "stop"}

    kernel = _Kernel()
    handle = _handle(kernel, _Client("alpha", 101, responder=responder))

    result = await handle.execute_slash_command(
        "/stop",
        source_channel="workbench_api",
        chat_id=7,
        session_metadata={"session_surface": "workbench"},
    )

    assert result == {"ok": True, "command": "stop"}
    assert observed == {
        "method": "runtime.slash",
        "params": {
            "text": "/stop",
            "source_channel": "workbench_api",
            "chat_id": 7,
            "session_metadata": {"session_surface": "workbench"},
        },
    }


@pytest.mark.asyncio
async def test_worker_activation_reconciles_only_its_agent_before_accepting(
    monkeypatch,
):
    calls = []

    class _Store:
        def reconcile_incomplete_runs(self, *, agent_id=None):
            calls.append(agent_id)
            return [{"request_id": "req-stale"}]

    from orchestrator import runtime_session

    monkeypatch.setattr(runtime_session, "ensure_store", lambda _runtime: _Store())
    host = FunctionWorkerHost.__new__(FunctionWorkerHost)
    host.agent_name = "alpha"

    reconciled = await host._reconcile_interrupted_session_runs(object())

    assert calls == ["alpha"]
    assert reconciled == [{"request_id": "req-stale"}]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("terminal", "native_enabled", "expected_safe_voice"),
    [
        ("desktop", True, False),
        ("session-api", False, True),
    ],
)
async def test_worker_voice_transcript_gate_respects_terminal_native_authority(
    tmp_path,
    monkeypatch,
    terminal,
    native_enabled,
    expected_safe_voice,
):
    from orchestrator import voice_transcriber

    class _Transcriber:
        async def transcribe(self, _path):
            return "confirmed transcript"

    monkeypatch.setattr(
        voice_transcriber,
        "get_transcriber",
        lambda: _Transcriber(),
    )
    audio_path = tmp_path / f"{terminal}.ogg"
    audio_path.write_bytes(b"OggS-test")
    runtime = SimpleNamespace(
        _native_voice_transcripts={},
        _safevoice_enabled=True,
        voice_manager=SimpleNamespace(
            native_audio_enabled=lambda selected: bool(
                native_enabled and selected == terminal
            )
        ),
    )
    host = FunctionWorkerHost.__new__(FunctionWorkerHost)
    host.runtime = runtime
    state = await host._begin_native_voice_transcription(
        {
            "parts": [
                {
                    "type": "media",
                    "modality": "audio",
                    "semantic_role": "voice_message",
                    "attachment_id": f"att-{terminal}",
                    "local_ref": str(audio_path),
                }
            ]
        },
        terminal=terminal,
    )

    assert state is not None
    assert state["safe_voice"] is expected_safe_voice
    await state["task"]


@pytest.mark.asyncio
async def test_core_worker_exit_reconciliation_uses_agent_filter(tmp_path):
    from orchestrator.session_store import SessionStore

    kernel = _Kernel()
    store = SessionStore(tmp_path / "sessions.sqlite3", instance_id="HASHI3")
    alpha_session = store.ensure_default_session(owner_id="user:7", agent_id="alpha")
    beta_session = store.ensure_default_session(owner_id="user:7", agent_id="beta")
    alpha = store.accept_run(
        session_id=alpha_session["session_id"],
        owner_id="user:7",
        agent_id="alpha",
        request_id="req-alpha-worker-exit",
        text="alpha",
        source="api",
        idempotency_key="alpha-worker-exit",
    )
    beta = store.accept_run(
        session_id=beta_session["session_id"],
        owner_id="user:7",
        agent_id="beta",
        request_id="req-beta-live",
        text="beta",
        source="api",
        idempotency_key="beta-live",
    )
    store.mark_request_running(alpha.request_id, worker_id="alpha-old")
    store.mark_request_running(beta.request_id, worker_id="beta-live")
    kernel.workbench_api = SimpleNamespace(session_store=store)
    supervisor = FunctionWorkerSupervisor(kernel)

    reconciled = await supervisor.reconcile_interrupted_session_runs(
        "alpha",
        lifecycle_reason="test-worker-exit",
    )

    assert [row["run_id"] for row in reconciled] == [alpha.run_id]
    assert store.get_run(alpha.run_id, owner_id="user:7")["state"] == "interrupted"
    assert store.get_run(beta.run_id, owner_id="user:7")["state"] == "running"


@pytest.mark.asyncio
async def test_core_replaces_worker_supplied_device_path_authority(tmp_path):
    kernel = _Kernel()
    kernel.agent_authority_roots = {"agent1": str(tmp_path / "authorized")}
    received = []

    class Broker:
        async def invoke(self, kind, action, arguments, **kwargs):
            received.append((kind, action, arguments, kwargs))
            return "ok"

    kernel.capability_broker = Broker()
    supervisor = FunctionWorkerSupervisor(kernel)
    client = _Client("agent1", 4321)

    result = await supervisor._capability_request(
        client,
        "core.capability.invoke",
        {
            "capability_kind": "browser_control",
            "action": "upload",
            "args": {
                "file_path": str(tmp_path / "authorized" / "file.txt"),
                "_authorized_roots": [str(tmp_path.parent)],
            },
            "task_id": "task-1",
            "lease_id": "lease-1",
        },
    )

    assert result == "ok"
    assert received[0][2]["_authorized_roots"] == [
        str(tmp_path / "authorized")
    ]
    assert received[0][3]["lease_id"] == "lease-1"


def _verified_generation(tmp_path: Path) -> VerifiedFunctionGeneration:
    package = tmp_path / "orchestrator"
    package.mkdir(parents=True)
    (package / "worker_demo.py").write_text("VALUE = 1\n", encoding="utf-8")
    asset = package / "assets" / "prompt.txt"
    asset.parent.mkdir()
    asset.write_text("immutable prompt", encoding="utf-8")
    mutable = tmp_path / "flow" / "runs" / "run-1" / "state.json"
    mutable.parent.mkdir(parents=True)
    mutable.write_text("{}", encoding="utf-8")
    manifest = build_source_manifest(
        ["orchestrator.worker_demo"],
        code_root=tmp_path,
    )
    policy = load_runtime_policy(ROOT)
    runtime = current_runtime_fingerprint(policy, code_root=ROOT)
    return VerifiedFunctionGeneration(
        code_root=tmp_path,
        manifest=manifest,
        receipt=CandidateProbeReceipt(
            generation_id=manifest.generation_id,
            module_names=manifest.module_names,
            runtime=runtime,
            probe_pid=1234,
        ),
    )


def test_generation_artifact_contains_only_verified_immutable_bytes(tmp_path):
    source_root = tmp_path / "source"
    generation = _verified_generation(source_root)

    artifact = materialize_generation_artifact(tmp_path / "bridge", generation)

    verify_generation_artifact(artifact, generation)
    relative_files = {
        path.relative_to(artifact).as_posix()
        for path in artifact.rglob("*")
        if path.is_file()
    }
    assert relative_files == {
        "function-generation.json",
        "orchestrator/worker_demo.py",
        "orchestrator/assets/prompt.txt",
    }
    assert not (artifact / "flow" / "runs").exists()


def test_generation_artifact_tamper_is_rejected_before_worker_spawn(tmp_path):
    source_root = tmp_path / "source"
    generation = _verified_generation(source_root)
    artifact = materialize_generation_artifact(tmp_path / "bridge", generation)
    source = artifact / "orchestrator" / "worker_demo.py"
    try:
        source.chmod(0o644)
    except OSError:
        pass
    source.write_text("VALUE = 2\n", encoding="utf-8")

    with pytest.raises(Exception, match="source or asset changed"):
        verify_generation_artifact(artifact, generation)


def test_qualified_byte_verification_does_not_rebuild_dependency_graph(
    tmp_path,
    monkeypatch,
):
    generation = _verified_generation(tmp_path / "source")

    monkeypatch.setattr(
        "orchestrator.function_generation.build_source_manifest",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("qualified bytes must not rebuild the AST graph")
        ),
    )

    verify_qualified_manifest_bytes(
        generation.manifest,
        code_root=generation.code_root,
    )


def test_qualified_byte_verification_rejects_new_asset(tmp_path):
    generation = _verified_generation(tmp_path / "source")
    added = generation.code_root / "orchestrator" / "assets" / "unqualified.txt"
    added.write_text("not in the accepted manifest", encoding="utf-8")

    with pytest.raises(Exception, match="source or asset changed"):
        verify_qualified_manifest_bytes(
            generation.manifest,
            code_root=generation.code_root,
        )


def test_qualified_byte_verification_rejects_parent_traversal(tmp_path):
    escaped = tmp_path / "escaped.py"
    escaped.write_text("VALUE = 1\n", encoding="utf-8")
    manifest = build_source_manifest_from_entries(
        [
            SourceEntry(
                module="orchestrator.escaped",
                relative_path="../escaped.py",
                sha256=hashlib.sha256(escaped.read_bytes()).hexdigest(),
            )
        ]
    )
    root = tmp_path / "root"
    root.mkdir()

    with pytest.raises(Exception, match="source or asset changed"):
        verify_qualified_manifest_bytes(manifest, code_root=root)


def test_qualified_generation_cache_round_trip_and_tamper_rejection(
    tmp_path,
    monkeypatch,
):
    source_root = tmp_path / "source"
    bridge_home = tmp_path / "bridge"
    generation = _verified_generation(source_root)
    artifact = materialize_generation_artifact(bridge_home, generation)
    persist_qualified_generation_cache(bridge_home, generation, artifact)
    monkeypatch.setattr(
        VerifiedFunctionGeneration,
        "verify_qualified_source",
        lambda self, expected_runtime: None,
    )

    loaded = load_qualified_generation_cache(
        bridge_home,
        source_root,
        object(),
    )

    assert loaded is not None
    loaded_generation, loaded_artifact = loaded
    assert loaded_generation.manifest == generation.manifest
    assert loaded_artifact == artifact

    source = artifact / "orchestrator" / "worker_demo.py"
    source.chmod(0o644)
    source.write_text("VALUE = 999\n", encoding="utf-8")

    assert (
        load_qualified_generation_cache(
            bridge_home,
            source_root,
            object(),
        )
        is None
    )


@pytest.mark.asyncio
async def test_route_gate_drains_inflight_then_holds_new_work_until_commit():
    kernel = _Kernel()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def old_response(method, _params):
        assert method == "runtime.enqueue_api_text"
        entered.set()
        await release.wait()
        return "old-request"

    old = _Client("alpha", 101, responder=old_response)
    new = _Client("alpha", 202, generation=_Generation("b"))
    handle = _handle(kernel, old)
    kernel.runtimes.append(handle)

    inflight = asyncio.create_task(handle.enqueue_api_text("first"))
    await entered.wait()
    cutover = asyncio.create_task(handle.begin_cutover())
    await asyncio.sleep(0)
    assert not cutover.done()

    release.set()
    assert await inflight == "old-request"
    assert await cutover is old

    waiting = asyncio.create_task(handle.enqueue_api_text("second"))
    await asyncio.sleep(0)
    assert not waiting.done()
    await handle.commit_cutover(new, _metadata("alpha", 202))

    assert await waiting == "request-202"
    assert old.calls == ["runtime.enqueue_api_text"]
    assert new.calls == ["runtime.enqueue_api_text"]


@pytest.mark.asyncio
async def test_multi_agent_commit_publishes_all_routes_before_any_waiter_resumes():
    kernel = _Kernel()
    observed: list[tuple[int, int]] = []

    def response(_method, _params):
        observed.append(
            (
                kernel._runtime_map()["alpha"].worker_pid,
                kernel._runtime_map()["beta"].worker_pid,
            )
        )
        return "done"

    old_alpha = _Client("alpha", 101)
    old_beta = _Client("beta", 102)
    new_alpha = _Client("alpha", 201, generation=_Generation("b"), responder=response)
    new_beta = _Client("beta", 202, generation=_Generation("b"), responder=response)
    alpha = _handle(kernel, old_alpha)
    beta = _handle(kernel, old_beta)
    kernel.runtimes.extend((alpha, beta))
    await alpha.begin_cutover()
    await beta.begin_cutover()
    alpha_waiter = asyncio.create_task(alpha.enqueue_api_text("a"))
    beta_waiter = asyncio.create_task(beta.enqueue_api_text("b"))
    await asyncio.sleep(0)
    supervisor = FunctionWorkerSupervisor(kernel)

    await supervisor.commit_handles_atomically(
        {
            alpha: (new_alpha, _metadata("alpha", 201)),
            beta: (new_beta, _metadata("beta", 202)),
        }
    )
    await asyncio.gather(alpha_waiter, beta_waiter)

    assert observed == [(201, 202), (201, 202)]


@pytest.mark.asyncio
async def test_worker_crash_recovery_reuses_verified_generation_and_swaps_route(
    monkeypatch,
):
    kernel = _Kernel()
    generation = _Generation("c")
    failed = _Client("alpha", 101, generation=generation, alive=False)
    replacement = _Client("alpha", 201, generation=generation)
    handle = _handle(kernel, failed)
    kernel.runtimes.append(handle)
    supervisor = FunctionWorkerSupervisor(kernel)
    prepared: list[tuple[str, object, Path]] = []

    async def prepare(name, candidate_generation, *, generation_root):
        prepared.append((name, candidate_generation, generation_root))
        return replacement

    async def activate(client):
        return _metadata(client.agent_name, client.pid)

    async def no_broadcast():
        return None

    monkeypatch.setattr(supervisor, "prepare_worker", prepare)
    monkeypatch.setattr(supervisor, "activate_new_worker", activate)
    monkeypatch.setattr(supervisor, "broadcast_topology", no_broadcast)

    await supervisor._recover_active_worker(handle, failed)

    assert handle.client is replacement
    assert prepared == [("alpha", generation, failed.generation_root)]
    assert handle._offline_error is None


@pytest.mark.asyncio
async def test_recovery_exhaustion_fails_closed_instead_of_routing_to_dead_worker(
    monkeypatch,
):
    kernel = _Kernel()
    failed = _Client("alpha", 101, alive=False)
    handle = _handle(kernel, failed)
    kernel.runtimes.append(handle)
    supervisor = FunctionWorkerSupervisor(kernel)
    attempts = 0

    async def reject(*_args, **_kwargs):
        nonlocal attempts
        attempts += 1
        raise FunctionWorkerError("candidate cannot start")

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(supervisor, "prepare_worker", reject)
    monkeypatch.setattr("orchestrator.function_worker_supervisor.asyncio.sleep", no_sleep)
    monkeypatch.setattr(
        "orchestrator.function_worker_supervisor.WORKER_RECOVERY_ATTEMPTS",
        2,
    )

    await supervisor._recover_active_worker(handle, failed)

    assert attempts == 2
    assert handle.metadata["worker_phase"] == "FAILED"
    assert handle._offline_error is not None
    with pytest.raises(FunctionWorkerDisconnected, match="could not be recovered"):
        await handle.enqueue_api_text("must not reach dead process")


@pytest.mark.asyncio
async def test_cancelled_gate_acquisition_reopens_the_route():
    kernel = _Kernel()
    handle = _handle(kernel, _Client("alpha", 101))
    handle._route_inflight = 1
    cutover = asyncio.create_task(handle.begin_cutover())
    await asyncio.sleep(0)
    assert handle._cutover
    cutover.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cutover
    assert not handle._cutover


@pytest.mark.asyncio
async def test_cancelled_worker_preparation_retires_unready_process(
    tmp_path, monkeypatch
):
    kernel = _Kernel()
    kernel.paths = SimpleNamespace(code_root=tmp_path, bridge_home=tmp_path)
    kernel.runtime_fingerprint = SimpleNamespace(to_dict=lambda: {})
    supervisor = FunctionWorkerSupervisor(kernel)
    monkeypatch.setattr(supervisor, "topology_snapshot", lambda **kwargs: {})
    process = SimpleNamespace(start=lambda: None)
    connection = SimpleNamespace(close=lambda: None)
    process_args = []

    def build_process(**kwargs):
        process_args.append(kwargs["args"])
        return process

    context = SimpleNamespace(
        Pipe=lambda **kwargs: (connection, connection), Process=build_process
    )
    monkeypatch.setattr(
        "orchestrator.function_worker_supervisor.multiprocessing.get_context",
        lambda *args: context,
    )
    ready_wait = asyncio.Event()
    retired = []

    class Candidate:
        def __init__(self, **kwargs):
            pass

        def start(self):
            pass

        async def wait_ready(self):
            ready_wait.set()
            await asyncio.Event().wait()

        async def shutdown(self, **kwargs):
            retired.append(self)

    monkeypatch.setattr(
        "orchestrator.function_worker_supervisor.FunctionWorkerClient", Candidate
    )
    generation = SimpleNamespace(
        manifest=SimpleNamespace(generation_id="sha256:" + "a" * 64, to_dict=lambda: {})
    )
    task = asyncio.create_task(
        supervisor.prepare_worker("alpha", generation, generation_root=tmp_path)
    )
    await asyncio.wait_for(ready_wait.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(retired) == 1 and not supervisor._candidates
    assert process_args[0][1]["protocol_features"] == [WORKER_LOG_RELAY_FEATURE]
