from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace

import pytest

from orchestrator.runtime_nudge import parse_nudge_create_args
from orchestrator.scheduler import TaskScheduler
from orchestrator.skill_manager import SkillManager


def test_parse_nudge_create_args_requires_minutes_and_exit_condition():
    minutes, exit_condition = parse_nudge_create_args("5 until the scan is done")

    assert minutes == 5
    assert exit_condition == "until the scan is done"


def test_create_nudge_job_writes_prompt_and_metadata(tmp_path):
    manager = SkillManager(project_root=tmp_path, tasks_path=tmp_path / "tasks.json")

    job = manager.create_nudge_job(
        agent_name="zelda",
        interval_minutes=2,
        exit_condition="until the scan is done",
    )

    data = json.loads((tmp_path / "tasks.json").read_text(encoding="utf-8"))
    saved = data["nudges"][0]
    assert saved["id"] == job["id"]
    assert saved["agent"] == "zelda"
    assert saved["interval_seconds"] == 120
    assert saved["exit_condition"] == "until the scan is done"
    assert saved["nudge_meta"]["count"] == 0
    assert f"NUDGE_COMPLETE:{job['id']}" in saved["prompt"]


class FakeRuntime:
    name = "zelda"
    startup_success = True

    def __init__(self, *, busy: bool = False):
        self.busy = busy
        self.enqueued = []
        self.listeners = {}
        self.is_generating = False
        self.queue = SimpleNamespace(empty=lambda: not self.busy)

    def _backend_busy(self) -> bool:
        return self.busy

    async def enqueue_request(self, **kwargs):
        request_id = f"req-{len(self.enqueued) + 1}"
        self.enqueued.append((request_id, kwargs))
        return request_id

    def register_request_listener(self, request_id, callback):
        self.listeners[request_id] = callback


async def _run_one_scheduler_pass(scheduler: TaskScheduler):
    task = asyncio.create_task(scheduler.run())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_scheduler_nudge_enqueues_only_when_runtime_idle(tmp_path):
    manager = SkillManager(project_root=tmp_path, tasks_path=tmp_path / "tasks.json")
    job = manager.create_nudge_job(
        agent_name="zelda",
        interval_minutes=1,
        exit_condition="until complete",
    )
    runtime = FakeRuntime(busy=False)
    scheduler = TaskScheduler(
        tasks_path=tmp_path / "tasks.json",
        state_path=tmp_path / "scheduler_state.json",
        runtimes=[runtime],
        authorized_id=123,
    )

    await _run_one_scheduler_pass(scheduler)

    assert len(runtime.enqueued) == 1
    request_id, payload = runtime.enqueued[0]
    assert payload["summary"] == f"Nudge Task [{job['id']}]"
    assert request_id in runtime.listeners
    data = json.loads((tmp_path / "tasks.json").read_text(encoding="utf-8"))
    assert data["nudges"][0]["nudge_meta"]["count"] == 1


@pytest.mark.asyncio
async def test_scheduler_nudge_skips_when_runtime_busy(tmp_path):
    manager = SkillManager(project_root=tmp_path, tasks_path=tmp_path / "tasks.json")
    manager.create_nudge_job(
        agent_name="zelda",
        interval_minutes=1,
        exit_condition="until complete",
    )
    runtime = FakeRuntime(busy=True)
    scheduler = TaskScheduler(
        tasks_path=tmp_path / "tasks.json",
        state_path=tmp_path / "scheduler_state.json",
        runtimes=[runtime],
        authorized_id=123,
    )

    await _run_one_scheduler_pass(scheduler)

    assert runtime.enqueued == []
    assert scheduler.state["nudges"]


@pytest.mark.asyncio
async def test_scheduler_nudge_completion_marker_disables_job(tmp_path):
    manager = SkillManager(project_root=tmp_path, tasks_path=tmp_path / "tasks.json")
    job = manager.create_nudge_job(
        agent_name="zelda",
        interval_minutes=1,
        exit_condition="until complete",
    )
    runtime = FakeRuntime(busy=False)
    scheduler = TaskScheduler(
        tasks_path=tmp_path / "tasks.json",
        state_path=tmp_path / "scheduler_state.json",
        runtimes=[runtime],
        authorized_id=123,
    )

    await _run_one_scheduler_pass(scheduler)
    request_id, _ = runtime.enqueued[0]
    runtime.listeners[request_id]({"success": True, "text": f"done\nNUDGE_COMPLETE:{job['id']}"})

    data = json.loads((tmp_path / "tasks.json").read_text(encoding="utf-8"))
    assert data["nudges"][0]["enabled"] is False
    assert data["nudges"][0]["nudge_meta"]["stopped_reason"] == "exit_condition_met"
