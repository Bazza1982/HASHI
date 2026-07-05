from __future__ import annotations

import asyncio
from contextlib import suppress
import sys
import types
from types import SimpleNamespace

import pytest

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
    assert "external.module" not in reloaded


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
