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
async def test_initial_startup_prepares_once_runs_small_fleet_in_one_wave():
    kernel = _Kernel()
    handler = logging.NullHandler()
    manager = StartupManager(kernel, handler)
    names = [f"agent-{index}" for index in range(6)]

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
