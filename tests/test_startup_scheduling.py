"""Functions scheduling must bound work without weakening Worker admission."""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestrator.banner import StartupAnimationResult
from orchestrator.startup_manager import StartupManager


@pytest.mark.asyncio
@pytest.mark.parametrize("limit,count", [(1, 4), (3, 9), (8, 12), (12, 12), (16, 5)])
async def test_startup_honours_bounded_concurrency_and_prepares_every_agent(monkeypatch, limit, count):
    active = 0
    peak = 0
    prepared = []
    generation = SimpleNamespace(manifest=SimpleNamespace(generation_id="sha256:" + "a" * 64))

    async def prepare_generation():
        return generation, Path("prepared")

    async def start_agent(name, *, generation: object, generation_root):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        try:
            await asyncio.sleep(0.01)
            prepared.append((name, generation, generation_root))
            if name == "agent-1":
                raise RuntimeError("one Agent failed")
            return True, "ready"
        finally:
            active -= 1

    kernel = SimpleNamespace(
        function_workers=SimpleNamespace(
            prepare_generation=prepare_generation,
            telegram_ingress_snapshot=lambda _: {"running": True, "connected": True},
        ),
        paths=SimpleNamespace(instance_id="HASHI3", bridge_home=Path("instance")),
        enable_api_gateway=True,
        _startup_started_monotonic=time.monotonic(),
        startup_status={},
        start_agent=start_agent,
    )
    monkeypatch.setattr(
        "orchestrator.banner.show_startup_banner",
        lambda *a, **k: StartupAnimationResult(
            completed=True, attempted=True, reason="test", sink="stdout"),
    )
    await StartupManager(kernel, logging.NullHandler())._run_startup_banner(
        [f"agent-{i}" for i in range(count)],
        SimpleNamespace(workbench_port=18804, startup_agent_concurrency=limit),
        {}, [], [],
    )

    assert peak == min(limit, count)
    assert active == 0
    assert {n for n, _, _ in prepared} == {f"agent-{i}" for i in range(count)}
    assert all(g is generation and r == Path("prepared") for _, g, r in prepared)
    assert kernel.startup_status["completed"] == count
    assert kernel.startup_status["ready_agents"] == count - 1
    assert kernel.startup_status["failed_agents"] == 1
