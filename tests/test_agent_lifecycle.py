from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from orchestrator.agent_lifecycle import AgentLifecycleManager
from orchestrator.function_worker_supervisor import AgentRuntimeHandle


class _Process:
    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.alive = True

    def is_alive(self) -> bool:
        return self.alive


class _Client:
    def __init__(
        self,
        name: str,
        pid: int,
        *,
        generation: str = "sha256:" + "a" * 64,
        quiesce_error: Exception | None = None,
    ) -> None:
        self.agent_name = name
        self.process = _Process(pid)
        self.generation = SimpleNamespace(
            manifest=SimpleNamespace(generation_id=generation)
        )
        self.quiesce_error = quiesce_error
        self.calls: list[tuple[str, dict | None]] = []
        self.shutdown_calls = 0

    @property
    def pid(self) -> int:
        return self.process.pid

    @property
    def generation_id(self) -> str:
        return self.generation.manifest.generation_id

    async def call(self, method, params=None, **_kwargs):
        self.calls.append((method, params))
        if method == "worker.quiesce" and self.quiesce_error is not None:
            raise self.quiesce_error
        return {"ok": True}

    async def shutdown(self, *, force=True):
        assert force is True
        self.shutdown_calls += 1
        self.process.alive = False


def _metadata(name: str, pid: int, *, telegram: bool = False) -> dict:
    return {
        "name": name,
        "display_name": name.title(),
        "worker_pid": pid,
        "worker_phase": "ACTIVE",
        "worker_accepting": True,
        "startup_success": True,
        "backend_ready": True,
        "telegram_connected": telegram,
    }


class _FunctionWorkers:
    def __init__(self, kernel) -> None:
        self.kernel = kernel
        self.next_handle: AgentRuntimeHandle | None = None
        self.creation_error: Exception | None = None
        self.published = 0
        self.broadcasts = 0
        self.shutdown_all_calls = 0
        self.telegram_ingress: set[str] = set()

    async def create_active_handle(self, name):
        if self.creation_error is not None:
            raise self.creation_error
        assert self.next_handle is not None
        assert self.next_handle.name == name
        return self.next_handle

    def publish_generation_state(self):
        self.published += 1

    async def broadcast_topology(self):
        self.broadcasts += 1

    async def start_telegram_ingress(
        self,
        name,
        token,
        *,
        drop_pending_updates,
    ):
        assert token == f"token-{name}"
        assert drop_pending_updates is True
        self.telegram_ingress.add(name)
        return True

    async def stop_telegram_ingress(self, name):
        self.telegram_ingress.discard(name)

    def telegram_ingress_running(self, name):
        return name in self.telegram_ingress

    async def set_worker_telegram_status(self, name, connected):
        handle = self.kernel._runtime_map().get(name)
        if handle is not None:
            handle.metadata["telegram_connected"] = bool(connected)

    async def shutdown_all(self):
        self.shutdown_all_calls += 1
        for runtime in self.kernel.runtimes:
            if isinstance(runtime, AgentRuntimeHandle):
                await runtime.client.shutdown(force=True)


class _Kernel:
    def __init__(self, names=("alpha", "beta")) -> None:
        self.runtimes: list[object] = []
        self._startup_tasks: dict[str, asyncio.Task] = {}
        self._lifecycle_lock = asyncio.Lock()
        self._agent_locks: dict[str, asyncio.Lock] = {}
        self._configured_names = tuple(names)
        self.whatsapp = None
        self.whatsapp_notifications: list[str] = []
        self.function_workers = _FunctionWorkers(self)
        self.global_cfg = SimpleNamespace(authorized_id=42)
        self.runtime_fingerprint = SimpleNamespace(
            runtime_id="core",
            python="3.12.13",
            platform_abi="cp312",
            core_api=2,
            function_api=2,
            dependency_digest="deps",
            core_source_digest="core-source",
        )

    def _runtime_map(self):
        return {runtime.name: runtime for runtime in self.runtimes}

    def _agent_lock(self, name):
        return self._agent_locks.setdefault(name, asyncio.Lock())

    def _load_config_bundle(self):
        return (
            self.global_cfg,
            [
                SimpleNamespace(name=name, telegram_token_key=name)
                for name in self._configured_names
            ],
            {name: f"token-{name}" for name in self._configured_names},
        )

    async def _send_whatsapp_startup_notification(self, runtime):
        self.whatsapp_notifications.append(runtime.name)


def _handle(
    kernel: _Kernel,
    name: str,
    pid: int,
    *,
    telegram: bool = False,
    quiesce_error: Exception | None = None,
) -> AgentRuntimeHandle:
    client = _Client(name, pid, quiesce_error=quiesce_error)
    return AgentRuntimeHandle(
        kernel,
        client,
        _metadata(name, pid, telegram=telegram),
    )


@pytest.mark.asyncio
async def test_start_agent_registers_one_isolated_handle_and_publishes_topology():
    kernel = _Kernel(names=("alpha",))
    handle = _handle(kernel, "alpha", 101, telegram=True)
    kernel.function_workers.next_handle = handle
    bootstrap_calls: list[int] = []

    async def bootstrap(chat_id):
        bootstrap_calls.append(chat_id)
        return True

    handle.enqueue_startup_bootstrap = bootstrap
    manager = AgentLifecycleManager(kernel)

    ok, message = await manager.start_agent("alpha")

    assert (ok, message) == (True, "Started agent 'alpha'.")
    assert kernel.runtimes == [handle]
    assert bootstrap_calls == [42]
    assert kernel.function_workers.published == 1
    assert kernel.function_workers.broadcasts == 1
    assert kernel.function_workers.telegram_ingress == {"alpha"}
    assert kernel._startup_tasks == {}


@pytest.mark.asyncio
async def test_start_candidate_failure_never_registers_a_partial_runtime(monkeypatch):
    kernel = _Kernel(names=("alpha",))
    kernel.function_workers.creation_error = RuntimeError("candidate rejected")
    manager = AgentLifecycleManager(kernel)
    bridge_messages: list[str] = []
    monkeypatch.setattr(
        "orchestrator.agent_lifecycle.bridge_logger.exception",
        lambda message: bridge_messages.append(message),
    )

    ok, message = await manager.start_agent("alpha")

    assert ok is False
    assert "candidate rejected" in message
    assert kernel.runtimes == []
    assert kernel._startup_tasks == {}
    assert bridge_messages == [message]


@pytest.mark.asyncio
async def test_stop_agent_drains_worker_and_preserves_runtimes_list_identity():
    kernel = _Kernel()
    alpha = _handle(kernel, "alpha", 101)
    beta = _handle(kernel, "beta", 202)
    runtimes = kernel.runtimes
    runtimes.extend((alpha, beta))
    kernel.function_workers.telegram_ingress.add("alpha")
    external_holder = SimpleNamespace(runtimes=runtimes)
    manager = AgentLifecycleManager(kernel)

    ok, message = await manager.stop_agent("alpha")

    assert (ok, message) == (True, "Stopped agent 'alpha'.")
    assert kernel.runtimes is runtimes
    assert external_holder.runtimes is runtimes
    assert kernel.runtimes == [beta]
    assert alpha.client.calls[0][0] == "worker.quiesce"
    assert alpha.client.shutdown_calls == 1
    assert alpha.client.process.is_alive() is False
    assert alpha._offline_error == "Agent 'alpha' was stopped"
    assert kernel.function_workers.published == 1
    assert kernel.function_workers.broadcasts == 1
    assert "alpha" not in kernel.function_workers.telegram_ingress


@pytest.mark.asyncio
async def test_quiesce_failure_leaves_old_worker_registered_and_route_open():
    kernel = _Kernel(names=("alpha",))
    old = _handle(
        kernel,
        "alpha",
        101,
        quiesce_error=TimeoutError("still serving a request"),
    )
    kernel.runtimes.append(old)
    kernel.function_workers.telegram_ingress.add("alpha")
    manager = AgentLifecycleManager(kernel)

    ok, message = await manager.stop_agent("alpha")

    assert ok is False
    assert "remains registered" in message
    assert kernel.runtimes == [old]
    assert old.client.shutdown_calls == 0
    assert old._cutover is False
    assert old._offline_error is None
    assert kernel.function_workers.telegram_ingress == {"alpha"}


@pytest.mark.asyncio
async def test_stop_rejects_legacy_in_process_runtime_without_touching_it():
    kernel = _Kernel(names=("alpha",))
    legacy = SimpleNamespace(name="alpha")
    kernel.runtimes.append(legacy)
    manager = AgentLifecycleManager(kernel)

    ok, message = await manager.stop_agent("alpha")

    assert ok is False
    assert "not running in an isolated Function Worker" in message
    assert kernel.runtimes == [legacy]


@pytest.mark.asyncio
async def test_shutdown_all_closes_routes_and_clears_registry():
    kernel = _Kernel()
    alpha = _handle(kernel, "alpha", 101)
    beta = _handle(kernel, "beta", 202)
    kernel.runtimes.extend((alpha, beta))
    manager = AgentLifecycleManager(kernel)

    await manager.shutdown_all_agents(timeout=1.0)

    assert kernel.function_workers.shutdown_all_calls == 1
    assert kernel.runtimes == []
    assert alpha.client.process.is_alive() is False
    assert beta.client.process.is_alive() is False
    assert alpha._offline_error is not None
    assert beta._offline_error is not None
