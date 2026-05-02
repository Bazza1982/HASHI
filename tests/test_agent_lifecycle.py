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
