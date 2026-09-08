from __future__ import annotations

from types import SimpleNamespace
import asyncio
import json

import pytest

from orchestrator.function_worker_supervisor import (
    AgentRuntimeHandle,
    FunctionWorkerError,
    FunctionWorkerSupervisor,
)
from orchestrator.reboot_manager import RebootManager, _resolve_restart_targets


class _Process:
    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.alive = True

    def is_alive(self) -> bool:
        return self.alive


class _Generation:
    def __init__(
        self,
        marker: str,
        *,
        verify_error: Exception | None = None,
    ) -> None:
        self.manifest = SimpleNamespace(
            generation_id="sha256:" + marker * 64,
            entries=(object(), object()),
        )
        self.receipt = SimpleNamespace(
            probe_pid=9001,
            runtime=SimpleNamespace(runtime_id="core-runtime"),
        )
        self.verify_error = verify_error
        self.verify_calls = 0

    def verify(self, _runtime_fingerprint):
        self.verify_calls += 1
        if self.verify_error is not None:
            raise self.verify_error


class _Client:
    def __init__(
        self,
        name: str,
        pid: int,
        generation: _Generation,
        events: list[str],
        *,
        telegram: bool = True,
        quiesce_error: Exception | None = None,
        activation_error: Exception | None = None,
    ) -> None:
        self.agent_name = name
        self.process = _Process(pid)
        self.generation = generation
        self.events = events
        self.telegram = telegram
        self.quiesce_error = quiesce_error
        self.activation_error = activation_error
        self.metadata = _metadata(name, pid, generation, telegram=telegram)
        self.shutdown_calls = 0

    @property
    def pid(self) -> int:
        return self.process.pid

    @property
    def generation_id(self) -> str:
        return self.generation.manifest.generation_id

    async def call(self, method, params=None, **_kwargs):
        self.events.append(f"{self.agent_name}:{self.pid}:{method}")
        if method == "worker.metadata":
            return dict(self.metadata)
        if method == "worker.quiesce" and self.quiesce_error is not None:
            raise self.quiesce_error
        if method == "worker.quiesce":
            self.metadata.update(worker_phase="QUIESCED", worker_accepting=False)
        if method == "worker.resume":
            if getattr(self, "resume_error", None) is not None:
                raise self.resume_error
            self.metadata["worker_phase"] = "ACTIVE"
            self.metadata["worker_accepting"] = True
        return {"ok": True, "params": params}

    async def shutdown(self, *, force=True):
        assert force is True
        self.events.append(f"{self.agent_name}:{self.pid}:shutdown")
        self.shutdown_calls += 1
        self.process.alive = False


def _metadata(
    name: str,
    pid: int,
    generation: _Generation,
    *,
    telegram: bool,
) -> dict:
    return {
        "name": name,
        "worker_pid": pid,
        "generation_id": generation.manifest.generation_id,
        "generation_module_count": len(generation.manifest.entries),
        "runtime_id": "core-runtime",
        "worker_phase": "ACTIVE",
        "worker_accepting": True,
        "startup_success": True,
        "backend_ready": True,
        "online": True,
        "is_generating": False,
        "queue_depth": 0,
        "telegram_connected": telegram,
    }


class _FunctionWorkers:
    def __init__(self, kernel, events: list[str]) -> None:
        self.kernel = kernel
        self.events = events
        self.rounds: list[tuple[_Generation, dict[str, _Client]]] = []
        self.current: dict[str, _Client] = {}
        self.qualify_error: Exception | None = None
        self.remembered: list[_Generation] = []
        self.prepared: list[str] = []
        self.published = 0
        self.broadcasts = 0

    def queue_round(self, generation: _Generation, candidates: dict[str, _Client]):
        self.rounds.append((generation, candidates))

    def qualify_generation(self):
        self.events.append("qualify")
        if self.qualify_error is not None:
            raise self.qualify_error
        generation, self.current = self.rounds.pop(0)
        return generation

    def remember_generation(self, generation):
        self.remembered.append(generation)

    async def prepare_worker(self, name, generation):
        self.events.append(f"{name}:prepare")
        self.prepared.append(name)
        candidate = self.current[name]
        assert candidate.generation is generation
        return candidate

    async def activate_new_worker(self, candidate):
        self.events.append(f"{candidate.agent_name}:{candidate.pid}:activate")
        if candidate.activation_error is not None:
            raise candidate.activation_error
        candidate.metadata = _metadata(
            candidate.agent_name,
            candidate.pid,
            candidate.generation,
            telegram=candidate.telegram,
        )
        return dict(candidate.metadata)

    async def commit_handles_atomically(self, assignments):
        self.events.append("commit")
        await FunctionWorkerSupervisor.commit_handles_atomically(self, assignments)

    def publish_generation_state(self):
        self.published += 1
        ids = {handle.generation_id for handle in self.kernel.runtimes}
        self.kernel.function_generation = {
            "generation_id": next(iter(ids)) if len(ids) == 1 else "mixed"
        }

    async def broadcast_topology(self):
        self.broadcasts += 1


class _Kernel:
    def __init__(self, names=("zelda", "sunny")) -> None:
        self.events: list[str] = []
        self.runtime_fingerprint = SimpleNamespace(runtime_id="core-runtime")
        self.workbench_api = object()
        self.api_gateway = object()
        self.scheduler = object()
        self.background_job_manager = object()
        self.runtimes: list[AgentRuntimeHandle] = []
        self.function_generation = {"generation_id": "old"}
        self.function_workers = _FunctionWorkers(self, self.events)
        old_generation = _Generation("0")
        for index, name in enumerate(names, start=1):
            client = _Client(
                name,
                100 + index,
                old_generation,
                self.events,
                telegram=True,
            )
            self.runtimes.append(
                AgentRuntimeHandle(
                    self,
                    client,
                    _metadata(name, client.pid, old_generation, telegram=True),
                )
            )

    def _runtime_map(self):
        return {runtime.name: runtime for runtime in self.runtimes}

    def configured_agent_names(self):
        return ["zelda", "sunny", "offline"]

    def queue_generation(
        self,
        marker: str,
        *,
        names: tuple[str, ...] | None = None,
        verify_error: Exception | None = None,
        quiesce_error_for: str | None = None,
        activation_error_for: str | None = None,
        telegram_for: dict[str, bool] | None = None,
    ) -> dict[str, _Client]:
        generation = _Generation(marker, verify_error=verify_error)
        selected = names or tuple(runtime.name for runtime in self.runtimes)
        telegram_for = telegram_for or {}
        candidates = {
            name: _Client(
                name,
                200 + index,
                generation,
                self.events,
                telegram=telegram_for.get(name, True),
                activation_error=(
                    RuntimeError("activation failed")
                    if name == activation_error_for
                    else None
                ),
            )
            for index, name in enumerate(selected, start=1)
        }
        if quiesce_error_for is not None:
            self._runtime_map()[quiesce_error_for].client.quiesce_error = TimeoutError(
                "old Worker still busy"
            )
        self.function_workers.queue_round(generation, candidates)
        return candidates


def test_in_process_reload_api_is_retired():
    manager = RebootManager(kernel=object(), console_handler=None)

    with pytest.raises(FunctionWorkerError, match="replaces Function Workers"):
        manager.reload_project_modules(["orchestrator.runtime_pipeline"])


@pytest.mark.asyncio
@pytest.mark.parametrize("reject", [False, True])
async def test_reboot_persists_truthful_final_receipt_before_notification(
    tmp_path, reject
):
    kernel = _Kernel()
    kernel.paths = SimpleNamespace(bridge_home=tmp_path)
    kernel.queue_generation("a", names=("zelda",))
    if reject:
        kernel.function_workers.qualify_error = RuntimeError("candidate rejected")
    manager = RebootManager(kernel, None)
    ok = await manager.hot_restart({"mode": "min", "agent_name": "zelda"})
    assert ok is (not reject)
    data = json.loads((tmp_path / "state/instance/reboot-receipts.json").read_text())
    record = data["records"][-1]
    assert record["status"] == ("rejected" if reject else "succeeded")
    assert record["targets"] == ["zelda"]
    assert record["committed"] is (not reject)
    assert record["delivery"]["status"] != "sent"


def _notice_manager(tmp_path):
    kernel = _Kernel()
    kernel.paths = SimpleNamespace(bridge_home=tmp_path)
    kernel.shutdown_event = asyncio.Event()
    kernel._restart_request = None
    manager = RebootManager(kernel, None)
    kernel.reboot_manager = manager
    request = {
        "mode": "min",
        "agent_name": "zelda",
        "request_key": "update-1",
        "origin": {"chat_id": 42, "actor_id": "42", "thread_id": 7},
        "locale": "zh-CN",
    }
    return kernel, manager, request


@pytest.mark.asyncio
async def test_admission_freezes_target_and_repeated_requests_do_not_overwrite(
    tmp_path,
):
    kernel, manager, request = _notice_manager(tmp_path)
    request.update(mode="number", agent_number=1)
    first = manager.submit(request)
    assert first["accepted"] and kernel.shutdown_event.is_set()
    assert manager.receipts.get(first["record"]["id"])["status"] == "accepted"
    assert manager.submit(request)["record"]["id"] == first["record"]["id"]
    assert manager.submit({**request, "request_key": "update-2"}) == {
        "accepted": False,
        "reason": "busy",
    }
    scheduled = kernel._restart_request
    # Configuration ordering changes after acceptance must not retarget #1.
    kernel.configured_agent_names = lambda: ["sunny", "zelda", "offline"]
    kernel.queue_generation("a", names=("zelda",))
    assert await manager.hot_restart(scheduled)
    assert kernel.function_workers.prepared == ["zelda"]
    assert len(manager.receipts.records()) == 1


@pytest.mark.asyncio
async def test_failed_restore_is_reported_as_unavailable_not_restored(
    tmp_path, monkeypatch
):
    kernel, manager, request = _notice_manager(tmp_path)
    notices = []

    async def send(_kernel, **kwargs):
        notices.append(kwargs["render_text"]("zelda", "Zelda"))
        return {"sent": True, "sender": "zelda", "message_id": len(notices)}

    monkeypatch.setattr("orchestrator.reboot_manager.send_runtime_notice", send)
    kernel.queue_generation("a", names=("zelda",), activation_error_for="zelda")
    kernel._runtime_map()["zelda"].client.resume_error = RuntimeError("cannot resume")
    manager.submit(request)
    assert not await manager.hot_restart(kernel._restart_request)
    record = manager.receipts.records()[-1]
    assert record["status"] == "failed" and record["restored"] is False
    assert record["online"] == {"zelda": False}
    assert "暂未恢复在线" in notices[-1]
    assert "已恢复原状态" not in notices[-1]


@pytest.mark.asyncio
async def test_receipt_delivery_survives_origin_worker_loss_and_does_not_repeat_reboot(
    tmp_path, monkeypatch
):
    from orchestrator.reboot_receipts import RebootReceipts

    kernel, manager, request = _notice_manager(tmp_path)
    kernel._runtime_map()["zelda"].metadata["display_name"] = "显示<&>名称"
    calls = []

    async def send(_kernel, **kwargs):
        record = RebootReceipts(tmp_path).records()[-1]
        calls.append(
            (
                record["status"],
                kwargs["render_text"]("sunny", "备用名称"),
                kwargs["chat_id"],
                kwargs["thread_id"],
            )
        )
        return {
            "sent": record["status"] == "running",
            "sender": "sunny",
            "message_id": 1,
        }

    monkeypatch.setattr("orchestrator.reboot_manager.send_runtime_notice", send)
    kernel.queue_generation("a", names=("zelda",))
    manager.submit(request)
    assert await manager.hot_restart(kernel._restart_request)
    assert calls[-1][0] == "succeeded"  # persisted before sending
    assert "显示&lt;&amp;&gt;名称" in calls[-1][1]
    assert "备用名称" in calls[-1][1] and calls[-1][2:] == (42, 7)
    assert manager.receipts.records()[-1]["delivery"]["status"] == "pending"
    old_events = list(kernel.events)
    kernel.runtimes.clear()
    recovered = RebootManager(kernel, None)
    recovered.receipts.recover()

    async def recovered_send(_kernel, **kwargs):
        assert "补发" in kwargs["render_text"]("sunny", "备用名称")
        return {"sent": True, "sender": "sunny", "message_id": 2}

    monkeypatch.setattr(
        "orchestrator.reboot_manager.send_runtime_notice", recovered_send
    )
    await recovered.send_pending(now=10**12)
    await recovered.send_pending(now=10**12)
    assert kernel.events == old_events
    final = RebootReceipts(tmp_path).records()[-1]
    assert final["status"] == "succeeded" and final["delivery"]["status"] == "sent"
    assert final["delivery"]["attempts"] == 2


@pytest.mark.parametrize(
    ("restart", "expected"),
    [
        ({"mode": "min", "agent_name": "zelda"}, ("zelda",)),
        ({"mode": "number", "agent_number": 3}, ("offline",)),
        ({"mode": "same"}, ("zelda", "sunny")),
        ({"mode": "max"}, ("zelda", "sunny")),
    ],
)
def test_restart_scope_is_explicit_and_immutable(restart, expected):
    kernel = SimpleNamespace(
        runtimes=[SimpleNamespace(name="zelda"), SimpleNamespace(name="sunny")],
        configured_agent_names=lambda: ["zelda", "sunny", "offline"],
    )

    assert _resolve_restart_targets(kernel, restart) == expected


@pytest.mark.parametrize(
    "restart",
    [
        {"mode": "min"},
        {"mode": "number", "agent_number": 0},
        {"mode": "number", "agent_number": 4},
        {"mode": "number", "agent_number": "2"},
        {"mode": "number", "agent_number": True},
        {"mode": "unexpected", "agent_name": "zelda"},
    ],
)
def test_invalid_restart_scope_never_falls_back_to_all_agents(restart):
    kernel = SimpleNamespace(
        runtimes=[SimpleNamespace(name="zelda"), SimpleNamespace(name="sunny")],
        configured_agent_names=lambda: ["zelda", "sunny", "offline"],
    )

    with pytest.raises(ValueError):
        _resolve_restart_targets(kernel, restart)


@pytest.mark.asyncio
async def test_candidate_rejection_does_not_gate_or_touch_active_workers(capsys):
    kernel = _Kernel(names=("zelda",))
    old = kernel.runtimes[0]
    kernel.function_workers.qualify_error = RuntimeError("isolated import failed")
    manager = RebootManager(kernel, None)

    result = await manager.hot_restart({"mode": "min", "agent_name": "zelda"})

    assert result is False
    assert kernel.runtimes == [old]
    assert old._cutover is False
    assert old.client.process.is_alive()
    assert all("quiesce" not in event for event in kernel.events)
    assert "active Workers were not touched" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_targeted_reboot_switches_only_selected_worker_in_multi_agent_core():
    kernel = _Kernel()
    core_identity = id(kernel)
    fingerprint_identity = id(kernel.runtime_fingerprint)
    core_service_identities = (
        id(kernel.workbench_api),
        id(kernel.api_gateway),
        id(kernel.scheduler),
        id(kernel.background_job_manager),
    )
    old_zelda = kernel._runtime_map()["zelda"].client
    old_sunny = kernel._runtime_map()["sunny"].client
    candidates = kernel.queue_generation("a", names=("zelda",))
    manager = RebootManager(kernel, None)

    result = await manager.hot_restart({"mode": "min", "agent_name": "zelda"})

    handles = kernel._runtime_map()
    assert result is True
    assert handles["zelda"].client is candidates["zelda"]
    assert handles["sunny"].client is old_sunny
    assert old_zelda.shutdown_calls == 1
    assert old_sunny.shutdown_calls == 0
    assert all("sunny" not in event for event in kernel.events if "worker." in event)
    assert id(kernel) == core_identity
    assert id(kernel.runtime_fingerprint) == fingerprint_identity
    assert (
        id(kernel.workbench_api),
        id(kernel.api_gateway),
        id(kernel.scheduler),
        id(kernel.background_job_manager),
    ) == core_service_identities


@pytest.mark.asyncio
async def test_old_worker_drain_failure_discards_candidate_and_reopens_route():
    kernel = _Kernel(names=("zelda",))
    old = kernel.runtimes[0]
    candidates = kernel.queue_generation(
        "a",
        names=("zelda",),
        quiesce_error_for="zelda",
    )
    manager = RebootManager(kernel, None)

    result = await manager.hot_restart({"mode": "min", "agent_name": "zelda"})

    assert result is False
    assert old.client is not candidates["zelda"]
    assert old.client.process.is_alive()
    assert old._cutover is False
    assert old._offline_error is None
    assert candidates["zelda"].shutdown_calls == 1
    record = manager.receipts.records()[-1]
    assert record["reason"] == "drain_failed"
    assert record["restored"] is True


@pytest.mark.asyncio
async def test_source_change_after_drain_resumes_previous_worker():
    kernel = _Kernel(names=("zelda",))
    old = kernel.runtimes[0]
    candidates = kernel.queue_generation(
        "a",
        names=("zelda",),
        verify_error=RuntimeError("source changed after probe"),
    )
    manager = RebootManager(kernel, None)

    result = await manager.hot_restart({"mode": "min", "agent_name": "zelda"})

    assert result is False
    assert old.client.process.is_alive()
    assert old._cutover is False
    assert any(event.endswith("worker.resume") for event in kernel.events)
    assert candidates["zelda"].shutdown_calls == 1


@pytest.mark.asyncio
async def test_candidate_cannot_silently_drop_a_working_telegram_transport():
    kernel = _Kernel(names=("zelda",))
    old = kernel.runtimes[0]
    candidates = kernel.queue_generation(
        "a",
        names=("zelda",),
        telegram_for={"zelda": False},
    )
    manager = RebootManager(kernel, None)

    result = await manager.hot_restart({"mode": "min", "agent_name": "zelda"})

    assert result is False
    assert kernel.runtimes[0] is old
    assert old.client.process.is_alive()
    assert any(event.endswith("worker.resume") for event in kernel.events)
    assert candidates["zelda"].shutdown_calls == 1


@pytest.mark.asyncio
async def test_broad_activation_failure_rolls_back_every_route():
    kernel = _Kernel()
    old = {name: handle.client for name, handle in kernel._runtime_map().items()}
    candidates = kernel.queue_generation("b", activation_error_for="sunny")
    manager = RebootManager(kernel, None)

    result = await manager.hot_restart({"mode": "max"})

    assert result is False
    assert {
        name: handle.client for name, handle in kernel._runtime_map().items()
    } == old
    assert all(client.process.is_alive() for client in old.values())
    assert all(client.shutdown_calls == 1 for client in candidates.values())
    assert sum(event.endswith("worker.resume") for event in kernel.events) == 2


@pytest.mark.asyncio
async def test_broad_success_commits_every_route_and_then_retires_old_workers():
    kernel = _Kernel()
    old = {name: handle.client for name, handle in kernel._runtime_map().items()}
    candidates = kernel.queue_generation("c")
    manager = RebootManager(kernel, None)

    result = await manager.hot_restart({"mode": "same"})

    assert result is True
    assert {
        name: handle.client for name, handle in kernel._runtime_map().items()
    } == candidates
    assert all(client.shutdown_calls == 1 for client in old.values())
    assert all(client.shutdown_calls == 0 for client in candidates.values())
    assert kernel.events.index("commit") < min(
        kernel.events.index(f"{name}:{client.pid}:shutdown")
        for name, client in old.items()
    )


@pytest.mark.asyncio
async def test_repeated_targeted_reboot_keeps_core_identity_and_advances_generation():
    kernel = _Kernel(names=("zelda",))
    core_identity = id(kernel)
    fingerprint_identity = id(kernel.runtime_fingerprint)
    first = kernel.queue_generation("d", names=("zelda",))["zelda"]
    second = kernel.queue_generation("e", names=("zelda",))["zelda"]
    manager = RebootManager(kernel, None)

    assert await manager.hot_restart({"mode": "min", "agent_name": "zelda"})
    assert kernel.runtimes[0].client is first
    assert await manager.hot_restart({"mode": "min", "agent_name": "zelda"})

    assert kernel.runtimes[0].client is second
    assert first.shutdown_calls == 1
    assert id(kernel) == core_identity
    assert id(kernel.runtime_fingerprint) == fingerprint_identity


@pytest.mark.asyncio
async def test_route_gate_timeout_discards_candidate_and_releases_route(
    tmp_path, monkeypatch
):
    kernel, manager, request = _notice_manager(tmp_path)
    handle = kernel.runtimes[0]
    handle._route_inflight = 1
    candidates = kernel.queue_generation("a", names=("zelda",))
    monkeypatch.setattr(
        "orchestrator.reboot_manager.REBOOT_DRAIN_TIMEOUT_SECONDS", 0.01
    )
    manager.submit(request)
    assert not await asyncio.wait_for(manager.hot_restart(kernel._restart_request), 1)
    assert not handle._cutover and handle.client.process.is_alive()
    assert candidates["zelda"].shutdown_calls == 1
    assert manager.receipts.records()[-1]["restored"] is True
    assert manager.receipts.records()[-1]["reason"] == "route_busy"


@pytest.mark.asyncio
async def test_pending_capacity_recovery_and_bounded_delivery_never_rerun(
    tmp_path, monkeypatch
):
    from orchestrator import reboot_receipts

    monkeypatch.setattr(reboot_receipts, "MAX_RECORDS", 2)
    kernel, manager, request = _notice_manager(tmp_path)
    for key in ("one", "two"):
        assert manager.submit({**request, "request_key": key})["accepted"]
        kernel._restart_request = None  # simulate loss before execution
    assert manager.submit({**request, "request_key": "three"}) == {
        "accepted": False,
        "reason": "storage",
    }
    assert len(manager.receipts.records()) == 2 and not kernel.events
    recovered = RebootManager(kernel, None)
    recovered.receipts.recover()
    attempts = []

    async def failed_send(_kernel, **kwargs):
        attempts.append(kwargs["chat_id"])
        return {"sent": False, "retry_after": 30}

    monkeypatch.setattr("orchestrator.reboot_manager.send_runtime_notice", failed_send)
    for now in (
        10**12,
        10**12 + 1,
        10**12 + 100,
        10**12 + 200,
        10**12 + 300,
        10**12 + 400,
    ):
        await recovered.send_pending(now=now)
    assert len(attempts) == 8
    records = recovered.receipts.records()
    assert all(
        r["status"] == "unconfirmed" and r["delivery"]["status"] == "exhausted"
        for r in records
    )
    assert not kernel.events
    assert recovered.submit({**request, "request_key": "three"})["accepted"]
    assert [r["request_key"] for r in recovered.receipts.records()] == ["two", "three"]


@pytest.mark.asyncio
async def test_corrupt_receipts_disable_reboot_without_breaking_service_start(tmp_path):
    kernel, manager, request = _notice_manager(tmp_path)
    path = manager.receipts.path
    path.parent.mkdir(parents=True)
    path.write_text('{"schema": 1, "records": [null]}')
    manager.start_delivery()
    assert manager.delivery_task is None
    assert manager.submit(request) == {"accepted": False, "reason": "storage"}
    assert kernel._restart_request is None and not kernel.shutdown_event.is_set()
    assert not kernel.events and kernel.runtimes[0].client.process.is_alive()


@pytest.mark.asyncio
@pytest.mark.parametrize("ready", [True, False])
async def test_post_commit_health_decides_receipt_without_false_rollback(
    tmp_path, ready
):
    kernel, manager, request = _notice_manager(tmp_path)
    old = kernel.runtimes[0].client
    candidate = kernel.queue_generation("a", names=("zelda",))["zelda"]

    async def post_commit():
        candidate.metadata["backend_ready"] = ready
        raise RuntimeError("diagnostic publication failed")

    kernel.function_workers.broadcast_topology = post_commit
    manager.submit(request)
    assert await manager.hot_restart(kernel._restart_request)
    record = manager.receipts.records()[-1]
    assert record["status"] == ("succeeded" if ready else "unconfirmed")
    assert record["committed"] and record["online"] == {"zelda": ready}
    assert kernel.runtimes[0].client is candidate and candidate.process.is_alive()
    assert not old.process.is_alive()


@pytest.mark.asyncio
async def test_post_commit_receipt_disk_error_never_leaks_old_worker_or_claims_rollback(
    tmp_path, monkeypatch
):
    kernel, manager, request = _notice_manager(tmp_path)
    old = kernel.runtimes[0].client
    candidate = kernel.queue_generation("a", names=("zelda",))["zelda"]
    from orchestrator.reboot_receipts import write_record

    def persist(path, payload):
        if payload["records"][-1].get("committed"):
            raise OSError("disk unavailable")
        write_record(path, payload)

    monkeypatch.setattr("orchestrator.reboot_receipts.write_record", persist)
    manager.submit(request)
    assert await manager.hot_restart(kernel._restart_request)
    assert kernel.runtimes[0].client is candidate and not old.process.is_alive()
    assert manager.latest(**request["origin"])["status"] == "unconfirmed"
    assert manager.receipts.records()[-1]["delivery"]["status"] != "sent"
    assert manager.submit({**request, "request_key": "two"})["reason"] == "storage"


@pytest.mark.asyncio
async def test_commands_callbacks_and_rpc_acknowledge_real_transaction_and_scoped_status(
    tmp_path,
):
    from orchestrator import runtime_reboot, ui_language
    from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime
    from orchestrator.function_worker_host import WorkerKernelFacade
    from orchestrator.function_worker_protocol import FunctionWorkerProtocolError

    kernel, manager, _request = _notice_manager(tmp_path)
    active = kernel.runtimes[0].client
    supervisor = SimpleNamespace(kernel=kernel)

    async def rpc(method, params):
        return await FunctionWorkerSupervisor.handle_worker_request(
            supervisor, active, method, params
        )

    facade = object.__new__(WorkerKernelFacade)
    facade.peer = SimpleNamespace(request=rpc)
    messages = []

    async def reply(_update, text, **kwargs):
        messages.append((text, kwargs))

    runtime = SimpleNamespace(
        name="zelda",
        orchestrator=facade,
        global_config=SimpleNamespace(project_root=tmp_path, ui_language="zh-CN"),
        _is_authorized_user=lambda value: value == 42,
        _reply_text=reply,
    )
    update = SimpleNamespace(
        update_id=123,
        effective_user=SimpleNamespace(id=42),
        effective_chat=SimpleNamespace(id=42),
        effective_message=SimpleNamespace(message_thread_id=7),
    )
    ui_language.set_preferred_locale(runtime, "zh-CN", update)
    with ui_language.language_scope(runtime, update):
        await FlexibleAgentRuntime.cmd_reboot(
            runtime, update, SimpleNamespace(args=["min"])
        )
    assert not messages  # acceptance is not a premature success notification
    record = manager.receipts.records()[-1]
    assert record["locale"] == "zh-CN" and record["origin"]["thread_id"] == 7
    assert record["targets"] == ["zelda"] and record["status"] == "accepted"
    kernel.queue_generation("a", names=("zelda",))
    assert await manager.hot_restart(kernel._restart_request)
    active = kernel._runtime_map()["sunny"].client  # status through another Agent
    runtime.name = "sunny"
    with ui_language.language_scope(runtime, update):
        await runtime_reboot.command(runtime, update, SimpleNamespace(args=["status"]))
    assert messages[-1][0].count("✅") == 1 and messages[-1][1]["parse_mode"] == "HTML"
    assert await facade.reboot_status(actor_id=99, chat_id=42, thread_id=7) is None
    assert await facade.reboot_status(actor_id=42, chat_id=42, thread_id=8) is None
    active = kernel._runtime_map()["zelda"].client
    old = active
    kernel._runtime_map()["zelda"]._client = kernel._runtime_map()["sunny"].client
    with pytest.raises(FunctionWorkerProtocolError, match="inactive"):
        await rpc("core.reboot.submit", {"mode": "min"})
    kernel._runtime_map()["zelda"]._client = old


@pytest.mark.asyncio
async def test_group_button_reboots_one_exact_set_with_one_receipt(tmp_path):
    from orchestrator import runtime_groups

    kernel, manager, _request = _notice_manager(tmp_path)
    kernel.configured_agent_names = lambda: ["zelda", "sunny"]

    async def submit(**request):
        return manager.submit(request)

    replies = []

    async def edit(text, **_kwargs):
        replies.append(text)

    async def answer():
        pass

    update = SimpleNamespace(
        update_id=222,
        effective_user=SimpleNamespace(id=42),
        effective_chat=SimpleNamespace(id=42),
        callback_query=SimpleNamespace(
            id="button-1",
            data="group:reboot:team",
            from_user=SimpleNamespace(id=42),
            answer=answer,
            edit_message_text=edit,
        ),
    )
    runtime = SimpleNamespace(
        name="initiator",
        _is_authorized_user=lambda _id: True,
        agent_directory=SimpleNamespace(
            resolve_group=lambda *args, **kwargs: ["zelda", "sunny"]
        ),
        orchestrator=SimpleNamespace(request_reboot=submit),
        global_config=SimpleNamespace(project_root=tmp_path),
    )
    await runtime_groups.callback_group(runtime, update, None)
    await runtime_groups.callback_group(
        runtime, update, None
    )  # duplicate button delivery
    assert len(manager.receipts.records()) == 1
    assert kernel._restart_request["targets"] == ["zelda", "sunny"]
    kernel.queue_generation("a")
    assert await manager.hot_restart(kernel._restart_request)
    assert kernel.function_workers.prepared == ["zelda", "sunny"]
    assert manager.receipts.records()[-1]["status"] == "succeeded"
    assert len(replies) == 1  # duplicate shows status, never enqueues again


@pytest.mark.asyncio
async def test_receipt_watcher_start_does_not_misclassify_newly_accepted_request(
    tmp_path,
):
    kernel, manager, request = _notice_manager(tmp_path)
    assert manager.submit(request)["accepted"]
    manager.start_delivery()
    try:
        assert manager.receipts.records()[-1]["status"] == "accepted"
        kernel.queue_generation("a", names=("zelda",))
        assert await manager.hot_restart(kernel._restart_request)
    finally:
        await manager.stop_delivery()


@pytest.mark.asyncio
async def test_cancelled_candidate_preparation_cleans_candidates_without_touching_active_workers(
    tmp_path,
):
    kernel, manager, request = _notice_manager(tmp_path)
    request["mode"] = "same"
    candidates = kernel.queue_generation("a")
    pending = asyncio.Event()

    async def prepare(name, _generation):
        if name == "sunny":
            pending.set()
            await asyncio.Event().wait()
        return candidates[name]

    kernel.function_workers.prepare_worker = prepare
    manager.submit(request)
    operation = asyncio.create_task(manager.hot_restart(kernel._restart_request))
    await asyncio.wait_for(pending.wait(), 1)
    operation.cancel()
    with pytest.raises(asyncio.CancelledError):
        await operation
    assert candidates["zelda"].shutdown_calls == 1
    assert all(
        handle.client.process.is_alive() and not handle._cutover
        for handle in kernel.runtimes
    )
    assert manager.receipts.records()[-1]["status"] == "unconfirmed"


@pytest.mark.asyncio
async def test_delivery_receipt_write_failure_cannot_send_unbounded_duplicates(
    tmp_path, monkeypatch
):
    from orchestrator.reboot_receipts import write_record

    kernel, manager, request = _notice_manager(tmp_path)
    manager.submit(request)
    record = manager.receipts.records()[-1]
    manager.receipts.update(record["id"], status="unconfirmed")
    sent = []

    async def send(_kernel, **_kwargs):
        sent.append(True)
        return {"sent": True, "sender": "zelda", "message_id": len(sent)}

    def persist(path, payload):
        if payload["records"][-1]["delivery"]["status"] == "sent":
            raise OSError("lost disk write after Telegram accepted message")
        write_record(path, payload)

    monkeypatch.setattr("orchestrator.reboot_manager.send_runtime_notice", send)
    monkeypatch.setattr("orchestrator.reboot_receipts.write_record", persist)
    for i in range(6):
        try:
            await manager.send_pending(now=10**12 + i * 1000)
        except OSError:
            pass
    assert len(sent) == 4
    assert manager.receipts.records()[-1]["delivery"]["status"] == "exhausted"
    assert not kernel.events


@pytest.mark.asyncio
async def test_other_frontend_destination_is_never_reinterpreted_as_telegram_chat(
    tmp_path, monkeypatch
):
    from orchestrator.runtime_reboot import origin_from_update

    kernel, manager, request = _notice_manager(tmp_path)
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=42),
        effective_chat=SimpleNamespace(id=12345),
        _hashi_session_surface="whatsapp",
    )
    origin = origin_from_update(None, update)
    request["origin"] = origin
    admitted = manager.submit(request)
    assert admitted["accepted"]
    assert admitted["record"]["delivery"]["status"] == "not_requested"
    sends = []

    async def forbidden_send(*args, **kwargs):
        sends.append(kwargs)
        return {"sent": False}

    monkeypatch.setattr(
        "orchestrator.reboot_manager.send_runtime_notice", forbidden_send
    )
    kernel.queue_generation("a", names=("zelda",))
    assert await manager.hot_restart(kernel._restart_request)
    assert not sends
    assert manager.latest(**origin)["status"] == "succeeded"
    assert manager.latest(actor_id="42", chat_id=12345) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("activity", [{"is_generating": True}, {"queue_depth": 2}])
async def test_busy_target_rejected_before_preparation_then_idle_retry(tmp_path, activity):
    kernel = _Kernel()
    kernel.paths = SimpleNamespace(bridge_home=tmp_path)
    kernel.queue_generation("a", names=("zelda",))
    handle = kernel._runtime_map()["zelda"]
    old = handle.client
    # Cached proxy metadata remains idle; the live RPC must decide.
    old.metadata.update(activity)
    manager = RebootManager(kernel, None)
    request = {"mode": "min", "agent_name": "zelda"}
    assert not await manager.hot_restart(request)
    record = manager.receipts.records()[-1]
    assert (record["status"], record["reason"]) == ("rejected", "target_busy")
    assert handle.client is old and old.process.alive
    assert all(old.metadata[key] == value for key, value in activity.items())
    assert not any("worker.quiesce" in event or "shutdown" in event for event in kernel.events)
    assert len(kernel.function_workers.rounds) == 1
    old.metadata.update(is_generating=False, queue_depth=0)
    assert await manager.hot_restart(request)
    assert handle.client is not old
    assert manager.receipts.records()[-1]["status"] == "succeeded"


@pytest.mark.asyncio
@pytest.mark.parametrize("state", [None, {"is_generating": False}, {"is_generating": False, "queue_depth": -1}, TimeoutError("metadata timeout"), ConnectionError("worker disconnected")])
async def test_unreadable_activity_retains_worker_and_actionable_receipt(tmp_path, state):
    from orchestrator.reboot_ui import render_status
    kernel = _Kernel(names=("zelda",))
    kernel.paths = SimpleNamespace(bridge_home=tmp_path)
    old = kernel.runtimes[0].client
    async def metadata(*args, **kwargs):
        if isinstance(state, Exception):
            raise state
        return state
    old.call = metadata
    manager = RebootManager(kernel, None)
    assert not await manager.hot_restart({"mode": "min", "agent_name": "zelda"})
    record = RebootManager(kernel, None).receipts.records()[-1]
    assert record["reason"] == "activity_unavailable"
    assert old.process.alive and not kernel.events
    assert "/reboot status" in render_status(record, locale="en")
