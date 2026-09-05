from __future__ import annotations

from types import SimpleNamespace

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
        if method == "worker.quiesce" and self.quiesce_error is not None:
            raise self.quiesce_error
        if method == "worker.resume":
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
            self._runtime_map()[quiesce_error_for].client.quiesce_error = (
                TimeoutError("old Worker still busy")
            )
        self.function_workers.queue_round(generation, candidates)
        return candidates


def test_in_process_reload_api_is_retired():
    manager = RebootManager(kernel=object(), console_handler=None)

    with pytest.raises(FunctionWorkerError, match="replaces Function Workers"):
        manager.reload_project_modules(["orchestrator.runtime_pipeline"])


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
