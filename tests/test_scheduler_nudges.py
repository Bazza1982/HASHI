from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from orchestrator.scheduler import TaskScheduler


class FakeRuntime:
    name = "lin_yueru"
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


def _write_tasks(path, payload):
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


@pytest.mark.asyncio
async def test_scheduler_nudge_enqueues_only_when_runtime_idle(tmp_path):
    tasks_path = tmp_path / "tasks.json"
    _write_tasks(
        tasks_path,
        {
            "heartbeats": [],
            "crons": [],
            "nudges": [
                {
                    "id": "lin_yueru-loop-922f1f",
                    "agent": "lin_yueru",
                    "enabled": True,
                    "interval_seconds": 120,
                    "action": "enqueue_prompt",
                    "prompt": "continue until done\nNUDGE_COMPLETE:lin_yueru-loop-922f1f",
                    "nudge_meta": {"count": 0, "max": 100},
                }
            ],
        },
    )
    runtime = FakeRuntime(busy=False)
    scheduler = TaskScheduler(
        tasks_path=tasks_path,
        state_path=tmp_path / "scheduler_state.json",
        runtimes=[runtime],
        authorized_id=123,
    )

    await _run_one_scheduler_pass(scheduler)

    assert len(runtime.enqueued) == 1
    request_id, payload = runtime.enqueued[0]
    assert payload["summary"] == "Nudge Task [lin_yueru-loop-922f1f]"
    assert request_id in runtime.listeners
    data = json.loads(tasks_path.read_text(encoding="utf-8"))
    assert data["nudges"][0]["nudge_meta"]["count"] == 1


@pytest.mark.asyncio
async def test_scheduler_nudge_skips_when_runtime_busy(tmp_path):
    tasks_path = tmp_path / "tasks.json"
    _write_tasks(
        tasks_path,
        {
            "heartbeats": [],
            "crons": [],
            "nudges": [
                {
                    "id": "lin_yueru-loop-922f1f",
                    "agent": "lin_yueru",
                    "enabled": True,
                    "interval_seconds": 120,
                    "action": "enqueue_prompt",
                    "prompt": "continue until done",
                    "nudge_meta": {"count": 0, "max": 100},
                }
            ],
        },
    )
    runtime = FakeRuntime(busy=True)
    scheduler = TaskScheduler(
        tasks_path=tasks_path,
        state_path=tmp_path / "scheduler_state.json",
        runtimes=[runtime],
        authorized_id=123,
    )

    await _run_one_scheduler_pass(scheduler)

    assert runtime.enqueued == []
    assert scheduler.state["nudges"]


@pytest.mark.asyncio
async def test_scheduler_nudge_completion_marker_disables_job(tmp_path):
    tasks_path = tmp_path / "tasks.json"
    _write_tasks(
        tasks_path,
        {
            "heartbeats": [],
            "crons": [],
            "nudges": [
                {
                    "id": "lin_yueru-loop-922f1f",
                    "agent": "lin_yueru",
                    "enabled": True,
                    "interval_seconds": 120,
                    "action": "enqueue_prompt",
                    "prompt": "continue until done",
                    "nudge_meta": {"count": 0, "max": 100},
                }
            ],
        },
    )
    runtime = FakeRuntime(busy=False)
    scheduler = TaskScheduler(
        tasks_path=tasks_path,
        state_path=tmp_path / "scheduler_state.json",
        runtimes=[runtime],
        authorized_id=123,
    )

    await _run_one_scheduler_pass(scheduler)
    request_id, _ = runtime.enqueued[0]
    runtime.listeners[request_id]({"success": True, "text": "done\nNUDGE_COMPLETE:lin_yueru-loop-922f1f"})

    data = json.loads(tasks_path.read_text(encoding="utf-8"))
    assert data["nudges"][0]["enabled"] is False
    assert data["nudges"][0]["nudge_meta"]["stopped_reason"] == "exit_condition_met"
