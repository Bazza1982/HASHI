from __future__ import annotations

import asyncio
import sys
import types
from contextlib import suppress
from types import SimpleNamespace

import pytest

from adapters import claw_cli
from orchestrator.hot_reload import HotReloadError
from orchestrator.reboot_manager import RebootManager
from orchestrator.service_manager import ServiceManager


def test_reload_project_modules_includes_tools(monkeypatch):
    manager = RebootManager(kernel=object(), console_handler=None)
    module_names = [
        "adapters.sample_adapter",
        "tools.hchat_send",
        "orchestrator.hchat_delivery",
        "orchestrator.runtime_pipeline",
        "orchestrator.runtime_status",
        "orchestrator.telegram_delivery_failover",
        "orchestrator.telegram_stream_policy",
        "external.module",
    ]
    modules = {name: types.ModuleType(name) for name in module_names}
    reloaded = []

    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)

    def fake_reload(module):
        reloaded.append(module.__name__)
        return module

    monkeypatch.setattr("orchestrator.reboot_manager.importlib.reload", fake_reload)

    manager.reload_project_modules()

    assert "adapters.sample_adapter" in reloaded
    assert "tools.hchat_send" in reloaded
    assert "orchestrator.hchat_delivery" in reloaded
    assert "orchestrator.runtime_pipeline" in reloaded
    assert "orchestrator.runtime_status" in reloaded
    assert "orchestrator.telegram_delivery_failover" in reloaded
    assert "orchestrator.telegram_stream_policy" in reloaded
    assert "external.module" not in reloaded


def test_reload_project_modules_loads_model_foundations_before_consumers(monkeypatch):
    manager = RebootManager(kernel=object(), console_handler=None)
    module_names = [
        "orchestrator.flexible_agent_runtime",
        "adapters.codex_cli",
        "orchestrator.flexible_backend_registry",
        "orchestrator.model_catalog",
        "orchestrator.flexible_backend_manager",
    ]
    reloaded = []

    for name in module_names:
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))

    def fake_reload(module):
        reloaded.append(module.__name__)
        return module

    monkeypatch.setattr("orchestrator.reboot_manager.importlib.reload", fake_reload)

    manager.reload_project_modules()

    catalog_idx = reloaded.index("orchestrator.model_catalog")
    registry_idx = reloaded.index("orchestrator.flexible_backend_registry")
    adapter_idx = reloaded.index("adapters.codex_cli")
    manager_idx = reloaded.index("orchestrator.flexible_backend_manager")
    runtime_idx = reloaded.index("orchestrator.flexible_agent_runtime")

    assert registry_idx < catalog_idx < adapter_idx < manager_idx < runtime_idx


def test_reload_project_modules_loads_runtime_defaults_before_consumers(monkeypatch):
    manager = RebootManager(kernel=object(), console_handler=None)
    module_names = [
        "orchestrator.flexible_agent_runtime",
        "orchestrator.runtime_defaults",
        "orchestrator.remote_lifecycle",
    ]
    reloaded = []

    for name in module_names:
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))

    def fake_reload(module):
        reloaded.append(module.__name__)
        return module

    monkeypatch.setattr("orchestrator.reboot_manager.importlib.reload", fake_reload)

    manager.reload_project_modules()

    defaults_idx = reloaded.index("orchestrator.runtime_defaults")
    lifecycle_idx = reloaded.index("orchestrator.remote_lifecycle")
    runtime_idx = reloaded.index("orchestrator.flexible_agent_runtime")
    assert defaults_idx < lifecycle_idx < runtime_idx


def test_reload_project_modules_loads_instance_provider_before_consumers(monkeypatch):
    manager = RebootManager(kernel=object(), console_handler=None)
    module_names = [
        "orchestrator.habits",
        "orchestrator.private_wol",
        "orchestrator.ticket_manager",
    ]
    reloaded = []

    for name in module_names:
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))

    def fake_reload(module):
        reloaded.append(module.__name__)
        return module

    monkeypatch.setattr("orchestrator.reboot_manager.importlib.reload", fake_reload)

    manager.reload_project_modules()

    provider_idx = reloaded.index("orchestrator.ticket_manager")
    assert provider_idx < reloaded.index("orchestrator.habits")
    assert provider_idx < reloaded.index("orchestrator.private_wol")


def test_reload_project_modules_loads_stream_policy_before_flexible_runtime(monkeypatch):
    manager = RebootManager(kernel=object(), console_handler=None)
    module_names = [
        "orchestrator.flexible_agent_runtime",
        "orchestrator.runtime_pipeline",
        "orchestrator.runtime_status",
        "orchestrator.telegram_stream_policy",
    ]
    reloaded = []

    for name in module_names:
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))

    def fake_reload(module):
        reloaded.append(module.__name__)
        return module

    monkeypatch.setattr("orchestrator.reboot_manager.importlib.reload", fake_reload)

    manager.reload_project_modules()

    policy_idx = reloaded.index("orchestrator.telegram_stream_policy")
    pipeline_idx = reloaded.index("orchestrator.runtime_pipeline")
    status_idx = reloaded.index("orchestrator.runtime_status")
    runtime_idx = reloaded.index("orchestrator.flexible_agent_runtime")
    assert policy_idx < runtime_idx
    assert pipeline_idx < runtime_idx
    assert status_idx < runtime_idx


def test_validate_agent_runtime_contract_accepts_current_modules():
    manager = RebootManager(kernel=object(), console_handler=None)

    manager.validate_agent_runtime_contract()


def test_validate_agent_runtime_contract_rejects_stale_claw_constant(monkeypatch):
    manager = RebootManager(kernel=object(), console_handler=None)

    monkeypatch.setattr(claw_cli, "KIND_ACKNOWLEDGEMENT", "stale")

    with pytest.raises(HotReloadError, match="retained a stale"):
        manager.validate_agent_runtime_contract()


def test_reload_project_modules_fails_fast_instead_of_continuing(monkeypatch):
    manager = RebootManager(kernel=object(), console_handler=None)
    first = types.ModuleType("orchestrator.first")
    broken = types.ModuleType("orchestrator.broken")
    after = types.ModuleType("orchestrator.after")
    for module in (first, broken, after):
        monkeypatch.setitem(sys.modules, module.__name__, module)
    calls = []

    def fake_reload(module):
        calls.append(module.__name__)
        if module is broken:
            raise RuntimeError("boom")
        return module

    monkeypatch.setattr("orchestrator.reboot_manager.importlib.reload", fake_reload)

    with pytest.raises(HotReloadError, match="orchestrator.broken"):
        manager.reload_project_modules(
            ["orchestrator.first", "orchestrator.broken", "orchestrator.after"]
        )

    assert calls == ["orchestrator.first", "orchestrator.broken"]


@pytest.mark.asyncio
async def test_hot_restart_fails_when_target_does_not_restart_even_if_others_run(
    monkeypatch,
):
    class ConsoleHandler:
        def addFilter(self, _filter):
            pass

        def removeFilter(self, _filter):
            pass

    class HotServices:
        def __init__(self):
            self.refreshed = False

        async def refresh_hot_services(self):
            self.refreshed = True

    class Kernel:
        def __init__(self):
            self.runtimes = [
                SimpleNamespace(name=f"other-{index}")
                for index in range(19)
            ]
            self.whatsapp = None
            self.global_cfg = SimpleNamespace(workbench_port=18800)
            self.api_gateway = None
            self.service_manager = HotServices()

        async def stop_agent(self, _name, reason):
            assert reason == "hot-restart:min"
            return True, "stopped"

        async def start_agent(self, name):
            assert name == "lily"
            return False, "detect_instance() takes 1 positional argument but 2 were given"

        def _load_config_bundle(self):
            return None, [SimpleNamespace(name="lily")], None

    kernel = Kernel()
    manager = RebootManager(kernel=kernel, console_handler=ConsoleHandler())
    monkeypatch.setattr(manager, "preflight_project_modules", lambda: [])
    monkeypatch.setattr(manager, "reload_project_modules", lambda _names: None)
    monkeypatch.setattr(manager, "rebuild_hot_managers", lambda: None)
    monkeypatch.setattr(
        "orchestrator.banner.show_startup_banner",
        lambda **_kwargs: None,
    )

    result = await manager.hot_restart(
        {"mode": "min", "agent_name": "lily", "agent_number": None}
    )

    assert result is False
    assert len(kernel.runtimes) == 19
    assert kernel.service_manager.refreshed is True


@pytest.mark.asyncio
async def test_restart_delivery_health_watcher_replaces_existing_task(monkeypatch):
    started = []

    async def fake_watcher(kernel):
        started.append(kernel)
        await asyncio.Event().wait()

    monkeypatch.setattr(
        "orchestrator.service_manager.delivery_health_watcher",
        fake_watcher,
    )
    kernel = SimpleNamespace(delivery_health_task=None)
    manager = ServiceManager(kernel)

    manager.start_delivery_health_watcher()
    first_task = kernel.delivery_health_task
    await asyncio.sleep(0)

    assert started == [kernel]
    assert first_task is not None
    assert not first_task.done()

    await manager.restart_delivery_health_watcher()
    second_task = kernel.delivery_health_task
    await asyncio.sleep(0)

    assert second_task is not first_task
    assert first_task.done()
    assert len(started) == 2

    await manager.stop_delivery_health_watcher()
    with suppress(asyncio.CancelledError):
        await second_task
