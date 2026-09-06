from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestrator.startup_manager import StartupManager


class _FunctionWorkers:
    def __init__(self) -> None:
        self.prepare_calls = 0
        self.generation = SimpleNamespace(
            manifest=SimpleNamespace(generation_id="sha256:" + "a" * 64)
        )
        self.generation_root = Path("prepared-generation")

    async def prepare_generation(self):
        self.prepare_calls += 1
        await asyncio.sleep(0)
        return self.generation, self.generation_root


class _Kernel:
    def __init__(self) -> None:
        self.function_workers = _FunctionWorkers()
        self.enable_api_gateway = True
        self._startup_started_monotonic = time.monotonic()
        self.startup_status = {}
        self.active_starts = 0
        self.max_active_starts = 0
        self.received: list[tuple[str, object, Path]] = []

    async def start_agent(self, name, *, generation, generation_root):
        self.received.append((name, generation, generation_root))
        self.active_starts += 1
        self.max_active_starts = max(self.max_active_starts, self.active_starts)
        await asyncio.sleep(0.02)
        self.active_starts -= 1
        return True, f"Started agent '{name}'."


@pytest.mark.asyncio
async def test_initial_startup_prepares_once_runs_small_fleet_in_one_wave(monkeypatch):
    kernel = _Kernel()
    handler = logging.NullHandler()
    manager = StartupManager(kernel, handler)
    names = [f"agent-{index}" for index in range(6)]
    info_messages = []
    monkeypatch.setattr(
        "orchestrator.startup_manager.bridge_logger.info",
        lambda message, *args: info_messages.append(message % args if args else message),
    )

    await manager._run_startup_banner(
        names,
        SimpleNamespace(workbench_port=18802),
        {},
        [],
        [],
    )

    assert kernel.function_workers.prepare_calls == 1
    assert kernel.max_active_starts == 6
    assert [item[0] for item in kernel.received] == names
    assert all(
        generation is kernel.function_workers.generation
        and root == kernel.function_workers.generation_root
        for _name, generation, root in kernel.received
    )
    assert kernel.startup_status["phase"] == "agents_ready"
    assert kernel.startup_status["completed"] == 6
    assert kernel.startup_status["ready_agents"] == 6
    assert kernel.startup_status["agent_percent"] == 100
    assert kernel.startup_status["percent"] == 90
    assert not any(
        message.startswith("Startup progress:") for message in info_messages
    )


@pytest.mark.asyncio
async def test_remote_supervisor_failure_is_actionable_and_marks_startup_degraded(
    monkeypatch,
    caplog,
    tmp_path,
):
    kernel = _Kernel()
    manager = StartupManager(kernel, logging.NullHandler())
    settings = SimpleNamespace(
        enabled=True,
        supervised=True,
        port=8767,
    )
    supervisor = SimpleNamespace(service_name="hashi-remote-hashi2.service")

    async def ensure_remote_started(_root):
        return {
            "ok": False,
            "action": "supervisor_unavailable",
            "reason": "per-instance supervisor is not registered",
            "settings": settings,
            "supervisor": supervisor,
            "service_name": supervisor.service_name,
        }

    monkeypatch.setattr(
        "orchestrator.startup_manager.importlib.import_module",
        lambda _name: SimpleNamespace(ensure_remote_started=ensure_remote_started),
    )
    global_config = SimpleNamespace(
        project_root=tmp_path,
        instance_id="HASHI2",
    )

    await manager._ensure_remote_lifecycle(global_config)
    with caplog.at_level(logging.WARNING, logger="BridgeU.Orchestrator"):
        manager._publish_startup_issues(kernel.startup_status["issues"])

    issue = kernel.startup_status["issues"][0]
    assert kernel.startup_status["degraded"] is True
    assert issue["code"] == "remote_supervisor_unavailable"
    assert issue["details"] == {
        "lifecycle_action": "supervisor_unavailable",
        "service_name": "hashi-remote-hashi2.service",
        "port": 8767,
    }
    assert issue["automatic_retry"] is False
    assert kernel.remote_lifecycle_status["available"] is False
    messages = [
        record.message
        for record in caplog.records
        if record.name == "BridgeU.Orchestrator"
    ]
    assert len(messages) == 1
    assert "HASHI2 Remote/HChat is unavailable" in messages[0]
    assert "Hashi Remote is included with HASHI" in messages[0]
    assert "Use /remote on to activate Hashi Remote" in messages[0]
    assert "bin/hashi-remote-ctl.sh enable" in messages[0]
    assert "install Hashi Remote" not in messages[0]
    assert "Diagnostic code: remote_supervisor_unavailable" in messages[0]


@pytest.mark.asyncio
async def test_remote_child_fallback_is_available_without_degrading_startup(
    monkeypatch,
    caplog,
    tmp_path,
):
    kernel = _Kernel()
    manager = StartupManager(kernel, logging.NullHandler())
    settings = SimpleNamespace(enabled=True, supervised=True, port=8767)
    process = SimpleNamespace(pid=123)

    async def ensure_remote_started(_root):
        return {
            "ok": True,
            "action": "started_child_fallback",
            "settings": settings,
            "process": process,
            "service_name": "hashi-remote-hashi2.service",
            "supervisor_fallback": {
                "reason": "systemd user service is unavailable",
            },
        }

    monkeypatch.setattr(
        "orchestrator.startup_manager.importlib.import_module",
        lambda _name: SimpleNamespace(ensure_remote_started=ensure_remote_started),
    )

    with caplog.at_level(logging.WARNING, logger="BridgeU.Bridge"):
        await manager._ensure_remote_lifecycle(
            SimpleNamespace(project_root=tmp_path, instance_id="HASHI2")
        )

    assert kernel.remote_lifecycle_status["available"] is True
    assert kernel.remote_lifecycle_status["supervised"] is False
    assert kernel.remote_lifecycle_status["supervisor_requested"] is True
    assert kernel.startup_status.get("degraded") is not True
    assert kernel._remote_lifecycle_process is process
    assert "Hashi Remote is active for HASHI2" in caplog.text


def test_command_registry_notices_are_deduplicated_across_workers(caplog):
    kernel = _Kernel()
    duplicate_notices = [
        {
            "code": "protected_private_command_override",
            "command": "queue",
            "module": "queue_buttons.py",
            "callbacks_ignored": True,
        },
        {
            "code": "protected_private_command_override",
            "command": "wiki",
            "module": "wiki.py",
            "callbacks_ignored": False,
        },
    ]
    kernel.runtimes = [
        SimpleNamespace(metadata={"command_registry_notices": duplicate_notices}),
        SimpleNamespace(metadata={"command_registry_notices": duplicate_notices}),
    ]
    manager = StartupManager(kernel, logging.NullHandler())

    notices = manager._command_registry_notices()
    with caplog.at_level(logging.INFO, logger="BridgeU.Orchestrator"):
        manager._publish_command_registry_notice(notices)

    assert [(item["command"], item["module"]) for item in notices] == [
        ("queue", "queue_buttons.py"),
        ("wiki", "wiki.py"),
    ]
    messages = [
        record.message
        for record in caplog.records
        if record.name == "BridgeU.Orchestrator"
    ]
    assert len(messages) == 1
    assert "ignored 2 protected override(s)" in messages[0]
    assert "/queue (queue_buttons.py)" in messages[0]
    assert "/wiki (wiki.py)" in messages[0]
