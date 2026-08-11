from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from orchestrator import scheduler as scheduler_module
from orchestrator.scheduler import TaskScheduler


class _HourlyCroniter:
    def __init__(self, schedule: str, base: datetime):
        assert schedule == "8 * * * *"
        self.base = base

    def get_next(self, _type):
        candidate = self.base.replace(minute=8, second=0, microsecond=0)
        if candidate <= self.base:
            candidate += timedelta(hours=1)
        self.base = candidate
        return candidate

    def get_prev(self, _type):
        candidate = self.base.replace(minute=8, second=0, microsecond=0)
        if candidate >= self.base:
            candidate -= timedelta(hours=1)
        self.base = candidate
        return candidate


class _FakeRuntime:
    name = "zelda"
    startup_success = True

    def __init__(self):
        self.enqueued: list[tuple[str, dict]] = []
        self.notices: list[dict] = []
        self.queue = SimpleNamespace(empty=lambda: True)
        self.is_generating = False

    async def enqueue_request(self, **kwargs):
        request_id = f"req-{len(self.enqueued) + 1}"
        self.enqueued.append((request_id, kwargs))
        return request_id

    async def send_long_message(self, **kwargs):
        self.notices.append(kwargs)
        return 0.01, 1


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
async def test_startup_groups_one_hundred_missed_crons_and_heartbeats_into_one_direct_notice(
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

    assert runtime.enqueued == []
    assert len(runtime.notices) == 1
    notice = runtime.notices[0]["text"]
    assert "100 个任务共错过" in notice
    assert notice.count("\n• ") == 100
    assert "1. 全部补跑" in notice
    assert "2. 部分补跑" in notice
    assert "3. 全部跳过" in notice
    assert len(scheduler.state["missed_crons"]) == 50
    assert len(scheduler.state["missed_heartbeats"]) == 50
    assert len(scheduler.state["recovery_batches"]) == 1

    await _run_one_scheduler_pass(scheduler)
    assert len(runtime.notices) == 1


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


def test_hourly_cron_occurrence_capture_counts_all_seven_missed_turns():
    last_run = datetime(2026, 8, 8, 22, 8, 32).timestamp()
    now_dt = datetime(2026, 8, 9, 5, 45, 40)

    captured = scheduler_module.scheduler_recovery.collect_cron_occurrences(
        "8 * * * *",
        last_run,
        now_dt,
        croniter_cls=_HourlyCroniter,
    )

    assert captured["missed_count"] == 7
    assert [datetime.fromtimestamp(value).strftime("%H:%M") for value in captured["due_at"]] == [
        "23:08",
        "00:08",
        "01:08",
        "02:08",
        "03:08",
        "04:08",
        "05:08",
    ]


def test_recovery_replay_is_safe_by_default_and_questions_are_not_actions():
    item = {
        "task_id": "hourly-hello",
        "kind": "cron",
        "missed_count": 7,
        "replay_limit": 1,
        "due_at": list(range(7)),
    }
    batch = {"batch_id": "batch-1", "status": "pending", "items": [item]}

    assert scheduler_module.scheduler_recovery.replayable_count(item) == 1
    assert scheduler_module.scheduler_recovery.parse_reply("How many were missed?", [batch]) is None
    assert scheduler_module.scheduler_recovery.parse_reply("全部补跑", [batch]) == {"action": "all"}
    assert scheduler_module.scheduler_recovery.parse_reply("补跑 3 次", [batch]) == {
        "action": "partial",
        "counts": {"hourly-hello": 3},
    }


def test_recent_legacy_notice_migrates_to_pending_seven_occurrence_batch(tmp_path, monkeypatch):
    monkeypatch.setattr(scheduler_module, "HAS_CRONITER", True)
    monkeypatch.setattr(scheduler_module, "croniter", _HourlyCroniter, raising=False)
    noticed_at = datetime(2026, 8, 9, 5, 45, 40).timestamp()
    cron = {
        "id": "hourly-hello",
        "agent": "zelda",
        "enabled": True,
        "schedule": "8 * * * *",
        "prompt": "say hello",
        "note": "Send one short hello",
        "recovery": {"max_replay": 24},
    }
    tasks_path = _write_tasks(tmp_path, heartbeats=[], crons=[cron])
    state_path = tmp_path / "scheduler_state.json"
    state_path.write_text(
        json.dumps(
            {
                "heartbeats": {},
                "crons": {"hourly-hello": noticed_at},
                "nudges": {},
                "missed_crons": {
                    "hourly-hello": {
                        "agent": "zelda",
                        "schedule": "8 * * * *",
                        "missed_by_seconds": 23860,
                        "noticed_at": noticed_at,
                    }
                },
                "missed_heartbeats": {},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(scheduler_module.time, "time", lambda: noticed_at + 60)

    scheduler = TaskScheduler(
        tasks_path=tasks_path,
        state_path=state_path,
        runtimes=[_FakeRuntime()],
        authorized_id=123,
    )

    batches = list(scheduler.state["recovery_batches"].values())
    assert len(batches) == 1
    assert batches[0]["legacy_migrated"] is True
    assert batches[0]["notice_status"] == "sent"
    assert batches[0]["items"][0]["missed_count"] == 7
    assert batches[0]["items"][0]["replay_limit"] == 24
    assert "missed_count=7" in scheduler.build_recovery_context("zelda")


@pytest.mark.asyncio
async def test_recovery_reply_replays_latest_selected_occurrences_and_keeps_context(tmp_path, monkeypatch):
    monkeypatch.setattr(scheduler_module, "HAS_CRONITER", True)
    monkeypatch.setattr(scheduler_module, "croniter", _HourlyCroniter, raising=False)
    cron = {
        "id": "hourly-hello",
        "agent": "zelda",
        "enabled": True,
        "schedule": "8 * * * *",
        "prompt": "say hello",
        "note": "Send one short hello",
        "recovery": {"max_replay": 24},
    }
    runtime = _FakeRuntime()
    scheduler = TaskScheduler(
        tasks_path=_write_tasks(tmp_path, heartbeats=[], crons=[cron]),
        state_path=tmp_path / "scheduler_state.json",
        runtimes=[runtime],
        authorized_id=123,
    )
    occurrences = scheduler_module.scheduler_recovery.collect_cron_occurrences(
        cron["schedule"],
        datetime(2026, 8, 8, 22, 8, 32).timestamp(),
        datetime(2026, 8, 9, 5, 45, 40),
        croniter_cls=_HourlyCroniter,
    )
    batch = scheduler._create_recovery_batch(
        agent_name="zelda",
        items=[{"job": cron, "kind": "cron", **occurrences}],
        now_ts=datetime(2026, 8, 9, 5, 45, 40).timestamp(),
    )
    batch["notice_status"] = "sent"

    result = await scheduler.handle_recovery_reply(
        agent_name="zelda",
        text="补跑 3 次",
        runtime_map={"zelda": runtime},
    )

    assert "补跑 3 次" in result
    assert len(runtime.enqueued) == 3
    assert [
        payload["prompt"].split("originally due at ", 1)[1].split(".", 1)[0]
        for _request_id, payload in runtime.enqueued
    ] == [
        "2026-08-09T03:08+10:00",
        "2026-08-09T04:08+10:00",
        "2026-08-09T05:08+10:00",
    ]
    assert batch["status"] == "resolved"
    context = scheduler.build_recovery_context("zelda")
    assert "RECENTLY RESOLVED RECOVERY BATCHES" in context
    assert "executed=3" in context
    assert "missed=7" in context
