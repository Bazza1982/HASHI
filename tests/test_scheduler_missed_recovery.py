from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace

import pytest

from orchestrator import scheduler as scheduler_module
from orchestrator.scheduler import TaskScheduler


class _FakeRuntime:
    name = "zelda"
    startup_success = True

    def __init__(self):
        self.enqueued: list[tuple[str, dict]] = []
        self.queue = SimpleNamespace(empty=lambda: True)
        self.is_generating = False

    async def enqueue_request(self, **kwargs):
        request_id = f"req-{len(self.enqueued) + 1}"
        self.enqueued.append((request_id, kwargs))
        return request_id


async def _run_one_scheduler_pass(scheduler: TaskScheduler) -> None:
    task = asyncio.create_task(scheduler.run())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def _write_tasks(tmp_path, *, heartbeats: list[dict], crons: list[dict]):
    tasks_path = tmp_path / "tasks.json"
    tasks_path.write_text(
        json.dumps({"heartbeats": heartbeats, "crons": crons, "nudges": []}),
        encoding="utf-8",
    )
    return tasks_path


@pytest.mark.asyncio
async def test_startup_groups_one_hundred_missed_crons_and_heartbeats_into_one_prompt(
    tmp_path,
    monkeypatch,
):
    heartbeats = [
        {
            "id": f"heartbeat-{index:03d}",
            "agent": "zelda",
            "enabled": True,
            "interval_seconds": 300,
            "prompt": f"run heartbeat {index}",
        }
        for index in range(50)
    ]
    crons = [
        {
            "id": f"cron-{index:03d}",
            "agent": "zelda",
            "enabled": True,
            "schedule": "0 12 * * *",
            "prompt": f"run cron {index}",
        }
        for index in range(50)
    ]
    runtime = _FakeRuntime()
    scheduler = TaskScheduler(
        tasks_path=_write_tasks(tmp_path, heartbeats=heartbeats, crons=crons),
        state_path=tmp_path / "scheduler_state.json",
        runtimes=[runtime],
        authorized_id=123,
    )
    old_run = time.time() - 7200
    scheduler.state["heartbeats"].update({job["id"]: old_run for job in heartbeats})
    scheduler.state["crons"].update({job["id"]: old_run for job in crons})
    stale_before = time.time() - 3600
    monkeypatch.setattr(
        scheduler_module,
        "_should_fire",
        lambda schedule, last_run_ts, now_dt: 7200.0 if last_run_ts < stale_before else None,
    )

    await _run_one_scheduler_pass(scheduler)

    assert len(runtime.enqueued) == 1
    _request_id, payload = runtime.enqueued[0]
    assert payload["summary"].startswith("Missed Scheduler Jobs x100")
    assert "cron 50，heartbeat 50" in payload["prompt"]
    assert payload["prompt"].count("\n- `") == 100
    assert "**全部执行**" in payload["prompt"]
    assert "**只执行部分**" in payload["prompt"]
    assert "**全部跳过**" in payload["prompt"]
    assert len(scheduler.state["missed_crons"]) == 50
    assert len(scheduler.state["missed_heartbeats"]) == 50

    await _run_one_scheduler_pass(scheduler)
    assert len(runtime.enqueued) == 1


@pytest.mark.asyncio
async def test_simultaneous_heartbeats_after_startup_run_normally_instead_of_grouping(tmp_path):
    heartbeats = [
        {
            "id": f"heartbeat-{index}",
            "agent": "zelda",
            "enabled": True,
            "interval_seconds": 60,
            "prompt": f"run heartbeat {index}",
        }
        for index in range(2)
    ]
    runtime = _FakeRuntime()
    scheduler = TaskScheduler(
        tasks_path=_write_tasks(tmp_path, heartbeats=heartbeats, crons=[]),
        state_path=tmp_path / "scheduler_state.json",
        runtimes=[runtime],
        authorized_id=123,
    )
    scheduler.state["heartbeats"].update({job["id"]: time.time() for job in heartbeats})

    await _run_one_scheduler_pass(scheduler)
    assert runtime.enqueued == []
    assert scheduler._startup_recovery_pending is False

    scheduler.state["heartbeats"].update(
        {job["id"]: time.time() - 120 for job in heartbeats}
    )
    await _run_one_scheduler_pass(scheduler)

    assert len(runtime.enqueued) == 2
    summaries = [payload["summary"] for _request_id, payload in runtime.enqueued]
    assert summaries == [
        "Heartbeat Task [heartbeat-0]",
        "Heartbeat Task [heartbeat-1]",
    ]
    assert scheduler.state["missed_heartbeats"] == {}
