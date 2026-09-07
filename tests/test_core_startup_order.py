from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from orchestrator import runtime_app as main_module


class _LifecycleState:
    state_path = None

    @staticmethod
    def mark_started(_pid):
        return {}, False


@pytest.mark.asyncio
@pytest.mark.parametrize("deferred", [False, True])
async def test_core_publishes_workbench_before_starting_function_workers(
    tmp_path,
    monkeypatch,
    deferred,
):
    events: list[str] = []

    class _StartupManager:
        async def start_initial_agents(self, _global_cfg, _agent_configs, _secrets):
            events.append("workers")
            return True, {}

        def show_startup_status(self):
            events.append("status")

    class _ServiceManager:
        async def start_workbench_api(self, _global_cfg, _secrets):
            events.append("workbench")

        async def start_runtime_services(self, _global_cfg, _secrets):
            events.append("remaining-services")

        async def stop_workbench_api(self):
            events.append("stop-workbench")

    class _ShutdownManager:
        async def full_shutdown(self):
            events.append("shutdown")

    global_cfg = SimpleNamespace(base_logs_dir=tmp_path)
    runtime_fingerprint = SimpleNamespace(
        runtime_id="test-runtime",
        platform_abi="test-abi",
        dependency_digest="test-dependencies",
    )
    kernel = object.__new__(main_module.UniversalOrchestrator)
    kernel.paths = SimpleNamespace(
        code_root=tmp_path,
        bridge_home=tmp_path,
        config_path=tmp_path / "agents.json",
    )
    kernel.runtime_fingerprint = runtime_fingerprint
    kernel._handoff_draining = deferred
    kernel.lifecycle_state = _LifecycleState()
    kernel.startup_manager = _StartupManager()
    kernel.service_manager = _ServiceManager()
    kernel.shutdown_manager = _ShutdownManager()
    kernel.shutdown_event = asyncio.Event()
    kernel.shutdown_event.set()
    kernel._restart_request = None
    kernel._load_config_bundle = lambda: (global_cfg, [], {})
    kernel._install_signal_handlers = lambda: None
    kernel._load_whatsapp_cfg = lambda: ({}, {})

    monkeypatch.setattr(main_module, "setup_bridge_file_logging", lambda *_args: None)

    await main_module.UniversalOrchestrator.run(kernel)

    assert events == [
        "workbench",
        "workers",
        "remaining-services",
        *([] if deferred else ["status"]),
        "shutdown",
    ]
    assert kernel.startup_status["ready"] is not deferred
    assert kernel.startup_status["phase"] == ("connecting" if deferred else "ready")


@pytest.mark.asyncio
async def test_core_closes_prestarted_workbench_when_no_worker_can_start(
    tmp_path,
    monkeypatch,
):
    events: list[str] = []

    class _StartupManager:
        async def start_initial_agents(self, _global_cfg, _agent_configs, _secrets):
            events.append("workers")
            return False, {}

    class _ServiceManager:
        async def start_workbench_api(self, _global_cfg, _secrets):
            events.append("workbench")

        async def stop_workbench_api(self):
            events.append("stop-workbench")

    global_cfg = SimpleNamespace(base_logs_dir=tmp_path)
    kernel = object.__new__(main_module.UniversalOrchestrator)
    kernel.paths = SimpleNamespace(
        code_root=tmp_path,
        bridge_home=tmp_path,
        config_path=tmp_path / "agents.json",
    )
    kernel.runtime_fingerprint = SimpleNamespace(
        runtime_id="test-runtime",
        platform_abi="test-abi",
        dependency_digest="test-dependencies",
    )
    kernel.lifecycle_state = _LifecycleState()
    kernel.startup_manager = _StartupManager()
    kernel.service_manager = _ServiceManager()
    kernel._load_config_bundle = lambda: (global_cfg, [], {})
    kernel._install_signal_handlers = lambda: None

    monkeypatch.setattr(main_module, "setup_bridge_file_logging", lambda *_args: None)

    await main_module.UniversalOrchestrator.run(kernel)

    assert events == ["workbench", "workers", "stop-workbench"]
