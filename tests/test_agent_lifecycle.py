import asyncio
from contextlib import suppress
from types import SimpleNamespace

import pytest

from orchestrator.agent_lifecycle import (
    LOCAL_ONLY_TELEGRAM_TOKEN,
    AgentLifecycleManager,
)
from orchestrator.request_activity import RequestActivityStore
from orchestrator.session_store import SessionStore


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
async def test_local_only_token_skips_telegram_preflight(monkeypatch):
    runtime = SimpleNamespace(
        name="portable",
        token=LOCAL_ONLY_TELEGRAM_TOKEN,
        telegram_connected=None,
    )
    manager = AgentLifecycleManager(DummyKernel([runtime]))
    preflight_calls = []

    async def unexpected_preflight(*args, **kwargs):
        preflight_calls.append((args, kwargs))
        return True

    monkeypatch.setattr(manager, "telegram_preflight", unexpected_preflight)

    connected = await manager.try_telegram_connect(runtime)

    assert connected is False
    assert runtime.telegram_connected is False
    assert preflight_calls == []


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
    assert "retry /reboot" in message
    await asyncio.sleep(0)
    assert shutdown_cancelled.is_set()
    assert kernel.runtimes is runtimes
    assert kernel.runtimes == [runtime]
    release_shutdown.set()
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_stop_agent_terminalizes_durable_run_and_live_activity(tmp_path):
    runtime = DummyRuntime("alpha")
    runtime.session_store = SessionStore(
        tmp_path / "sessions.sqlite3",
        instance_id="HASHI1",
    )
    runtime.request_activity = RequestActivityStore()
    session = runtime.session_store.ensure_default_session(
        owner_id="user:7",
        agent_id=runtime.name,
    )
    accepted = runtime.session_store.accept_run(
        session_id=session["session_id"],
        owner_id="user:7",
        agent_id=runtime.name,
        request_id="req-active-at-stop",
        text="active work",
        source="api",
        idempotency_key="active-at-stop",
    )
    runtime.session_store.mark_request_running(
        accepted.request_id,
        worker_id="runtime-before-stop",
    )
    runtime.request_activity.start(accepted.request_id, source="api")
    runtime.request_activity.mark_running(accepted.request_id)
    kernel = DummyKernel([runtime])
    manager = AgentLifecycleManager(kernel)

    ok, _message = await manager.stop_agent(runtime.name, reason="hot-restart:min")

    assert ok is True
    run = runtime.session_store.get_run(accepted.run_id, owner_id="user:7")
    assert run["state"] == "interrupted"
    assert run["error_code"] == "runtime_restart_interrupted"
    activity = runtime.request_activity.poll(accepted.request_id)
    assert activity["terminal"] is True
    assert activity["success"] is False
    assert kernel.runtimes == []


@pytest.mark.asyncio
async def test_start_agent_reconciles_stale_run_before_replacement_accepts_work(
    tmp_path,
    monkeypatch,
):
    class StartKernel(DummyKernel):
        def __init__(self):
            super().__init__([])
            self._agent_locks = {}
            self.whatsapp = None

        def _agent_lock(self, name):
            return self._agent_locks.setdefault(name, asyncio.Lock())

        def _load_config_bundle(self):
            return (
                SimpleNamespace(),
                [SimpleNamespace(name="alpha")],
                {},
            )

    class ReplacementRuntime(DummyRuntime):
        def __init__(self, store):
            super().__init__("alpha")
            self.session_store = store
            self.request_activity = RequestActivityStore()
            self.telegram_connected = False

        async def process_queue(self):
            await asyncio.Event().wait()

    store = SessionStore(tmp_path / "sessions.sqlite3", instance_id="HASHI1")
    session = store.ensure_default_session(owner_id="user:7", agent_id="alpha")
    accepted = store.accept_run(
        session_id=session["session_id"],
        owner_id="user:7",
        agent_id="alpha",
        request_id="req-stale-before-start",
        text="lost with old runtime",
        source="api",
        idempotency_key="stale-before-start",
    )
    store.mark_request_running(
        accepted.request_id,
        worker_id="old-runtime",
    )
    runtime = ReplacementRuntime(store)
    kernel = StartKernel()
    manager = AgentLifecycleManager(kernel)
    monkeypatch.setattr(manager, "build_runtime", lambda *_args: runtime)

    async def start_runtime(_runtime):
        return True, "Started agent 'alpha'."

    monkeypatch.setattr(manager, "start_runtime", start_runtime)

    ok, _message = await manager.start_agent("alpha")

    assert ok is True
    assert store.get_run(accepted.run_id, owner_id="user:7")["state"] == "interrupted"
    assert kernel.runtimes == [runtime]
    runtime.process_task.cancel()
    with suppress(asyncio.CancelledError):
        await runtime.process_task


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
