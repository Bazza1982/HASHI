import asyncio
from types import SimpleNamespace

import pytest

from orchestrator.agent_lifecycle import AgentLifecycleManager


class DummyRuntime:
    def __init__(self, name: str):
        self.name = name
        self.process_task = None
        self.shutdown_called = False

    async def shutdown(self):
        self.shutdown_called = True


class DummyKernel:
    def __init__(self, runtimes):
        self.runtimes = runtimes
        self._startup_tasks = {}
        self._lifecycle_lock = asyncio.Lock()

    def _runtime_map(self):
        return {runtime.name: runtime for runtime in self.runtimes}


@pytest.mark.asyncio
async def test_stop_agent_preserves_runtimes_list_identity():
    alpha = DummyRuntime("alpha")
    beta = DummyRuntime("beta")
    runtimes = [alpha, beta]
    kernel = DummyKernel(runtimes)
    manager = AgentLifecycleManager(kernel)

    external_holder = SimpleNamespace(runtimes=runtimes)

    ok, message = await manager.stop_agent("alpha")

    assert ok is True
    assert message == "Stopped agent 'alpha'."
    assert alpha.shutdown_called is True
    assert kernel.runtimes is runtimes
    assert external_holder.runtimes is runtimes
    assert [runtime.name for runtime in external_holder.runtimes] == ["beta"]


@pytest.mark.asyncio
async def test_stop_agent_times_out_without_removing_still_running_runtime(monkeypatch):
    release_shutdown = asyncio.Event()
    shutdown_cancelled = asyncio.Event()

    class StubbornRuntime(DummyRuntime):
        async def shutdown(self):
            self.shutdown_called = True
            while not release_shutdown.is_set():
                try:
                    await release_shutdown.wait()
                except asyncio.CancelledError:
                    shutdown_cancelled.set()

    runtime = StubbornRuntime("samantha")
    runtimes = [runtime]
    kernel = DummyKernel(runtimes)
    manager = AgentLifecycleManager(kernel)
    monkeypatch.setattr(
        "orchestrator.agent_lifecycle.RUNTIME_TEARDOWN_TIMEOUT_SECONDS",
        0.01,
    )

    ok, message = await asyncio.wait_for(
        manager.stop_agent("samantha", reason="hot-restart:min"),
        timeout=0.5,
    )

    assert ok is False
    assert "cold process restart required" in message
    await asyncio.sleep(0)
    assert shutdown_cancelled.is_set()
    assert kernel.runtimes is runtimes
    assert kernel.runtimes == [runtime]
    release_shutdown.set()
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_start_agent_runtime_build_failure_is_logged_to_bridge(monkeypatch):
    class StartKernel(DummyKernel):
        def __init__(self):
            super().__init__([])
            self._agent_locks = {}

        def _agent_lock(self, name):
            return self._agent_locks.setdefault(name, asyncio.Lock())

        def _load_config_bundle(self):
            return (
                SimpleNamespace(),
                [SimpleNamespace(name="lily")],
                {},
            )

    kernel = StartKernel()
    manager = AgentLifecycleManager(kernel)
    bridge_messages = []

    def fail_build(*_args):
        raise TypeError("detect_instance() takes 1 positional argument but 2 were given")

    monkeypatch.setattr(manager, "build_runtime", fail_build)
    monkeypatch.setattr(
        "orchestrator.agent_lifecycle.bridge_logger.exception",
        bridge_messages.append,
    )

    ok, message = await manager.start_agent("lily")

    assert ok is False
    assert message == (
        "Failed to initialize 'lily': TypeError: "
        "detect_instance() takes 1 positional argument but 2 were given"
    )
    assert bridge_messages == [message]
    assert kernel._startup_tasks == {}
