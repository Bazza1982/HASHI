from __future__ import annotations

import asyncio
import json

import pytest

from orchestrator import scheduler as scheduler_module
from orchestrator.scheduler import TaskScheduler


def test_unchanged_task_files_are_parsed_once(tmp_path, monkeypatch):
    tasks_path = tmp_path / "tasks.json"
    state_path = tmp_path / "scheduler_state.json"
    tasks_path.write_text(
        json.dumps({"heartbeats": [], "crons": [], "nudges": []}),
        encoding="utf-8",
    )
    reads = 0
    original = scheduler_module.read_config_json

    def counted_read(path):
        nonlocal reads
        if path == tasks_path:
            reads += 1
        return original(path)

    monkeypatch.setattr(scheduler_module, "read_config_json", counted_read)
    owner = TaskScheduler(tasks_path, state_path, [], 0)
    for _ in range(20):
        owner._load_tasks()

    assert reads == 1


@pytest.mark.asyncio
async def test_idle_scheduler_does_not_repeat_global_agent_or_superloop_work(
    tmp_path, monkeypatch
):
    tasks_path = tmp_path / "tasks.json"
    state_path = tmp_path / "scheduler_state.json"
    tasks_path.write_text(
        json.dumps({"heartbeats": [], "crons": [], "nudges": []}),
        encoding="utf-8",
    )
    parked_calls = 0
    superloop_calls = 0

    class Runtime:
        name = "momo"
        startup_success = True

        async def process_parked_topic_followups(self, _now):
            nonlocal parked_calls
            parked_calls += 1

    class Orchestrator:
        _handoff_draining = False
        runtimes = [Runtime()]

    def counted_superloops(*_args, **_kwargs):
        nonlocal superloop_calls
        superloop_calls += 1
        return {"loops_checked": 0, "waits_satisfied": 0, "loops_advanced": 0}

    monkeypatch.setattr(scheduler_module, "advance_superloops_once", counted_superloops)
    monkeypatch.setattr(scheduler_module, "SCHEDULER_EXTERNAL_RECHECK_SECONDS", 0.02)
    monkeypatch.setattr(scheduler_module, "SCHEDULER_AUXILIARY_RECHECK_SECONDS", 1.0)
    owner = TaskScheduler(tasks_path, state_path, [], 0, orchestrator=Orchestrator())
    task = asyncio.create_task(owner.run())
    try:
        await asyncio.sleep(0.12)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    assert parked_calls == 1
    assert superloop_calls == 1


@pytest.mark.asyncio
async def test_idle_auxiliary_pass_keeps_enterprise_scheduler_lease(
    tmp_path, monkeypatch
):
    tasks_path = tmp_path / "tasks.json"
    state_path = tmp_path / "scheduler_state.json"
    tasks_path.write_text(
        json.dumps({"heartbeats": [], "crons": [], "nudges": []}),
        encoding="utf-8",
    )
    lease_events = []

    class Orchestrator:
        _handoff_draining = False
        runtimes = []

    monkeypatch.setattr(scheduler_module, "SCHEDULER_EXTERNAL_RECHECK_SECONDS", 1.0)
    monkeypatch.setattr(scheduler_module, "SCHEDULER_AUXILIARY_RECHECK_SECONDS", 1.0)
    owner = TaskScheduler(
        tasks_path,
        state_path,
        [],
        0,
        orchestrator=Orchestrator(),
        enterprise_lease_store=object(),
    )
    monkeypatch.setattr(
        owner,
        "_acquire_enterprise_scheduler_lease",
        lambda: lease_events.append("acquire") or True,
    )
    monkeypatch.setattr(
        owner,
        "_release_enterprise_scheduler_lease",
        lambda: lease_events.append("release"),
    )

    task = asyncio.create_task(owner.run())
    try:
        await asyncio.sleep(0.05)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    assert lease_events == ["acquire", "release"]
