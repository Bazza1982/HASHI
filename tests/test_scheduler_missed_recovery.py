from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from orchestrator import scheduler as scheduler_module
from orchestrator.scheduler import TaskScheduler
from orchestrator.timezone_policy import resolve_local_wall_time


SYDNEY = ZoneInfo("Australia/Sydney")


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
async def test_startup_groups_missed_jobs_into_one_canonical_agent_conversation(
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
    context_refreshes = []

    async def broadcast_topology():
        context_refreshes.append(True)

    scheduler.orchestrator = SimpleNamespace(
        runtimes=[runtime],
        function_workers=SimpleNamespace(broadcast_topology=broadcast_topology),
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

    assert runtime.notices == []
    assert len(runtime.enqueued) == 1
    request_id, request = runtime.enqueued[0]
    assert request_id == "req-1"
    assert request["source"] == scheduler_module.scheduler_recovery.RECOVERY_CONVERSATION_SOURCE
    assert request["chat_id"] == 123
    assert request["idempotency_key"].startswith("scheduler-recovery-question:")
    assert request["request_metadata"]["session_surface"] == "hashi.internal"
    assert request["request_metadata"]["session_channel_key"] == "scheduler-recovery"
    assert request["request_metadata"]["scheduler_recovery"]["batch_id"]
    assert "Ask the user" in request["prompt"]
    assert "Do not resolve" in request["prompt"]
    notice = request["request_metadata"]["session_message_text"]
    assert "100 task(s) missed" in notice
    assert notice.count("\n• ") == 100
    assert "reply in your own words" in notice
    assert "1. Run all" not in notice
    assert "2. Run some" not in notice
    assert "3. Skip all" not in notice
    assert len(scheduler.state["missed_crons"]) == 50
    assert len(scheduler.state["missed_heartbeats"]) == 50
    assert len(scheduler.state["recovery_batches"]) == 1
    assert context_refreshes == [True]

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
    assert [
        payload["scheduler_context"]
        for _request_id, payload in runtime.enqueued
    ] == [
        {
            "kind": "heartbeat",
            "task_id": "heartbeat-0",
            "trigger": "scheduled",
        },
        {
            "kind": "heartbeat",
            "task_id": "heartbeat-1",
            "trigger": "scheduled",
        },
    ]
    assert scheduler.state["missed_heartbeats"] == {}


@pytest.mark.asyncio
async def test_single_recent_startup_catchup_is_marked_as_recovery(tmp_path):
    heartbeat = {
        "id": "heartbeat-1",
        "agent": "zelda",
        "enabled": True,
        "interval_seconds": 60,
        "prompt": "run heartbeat",
    }
    runtime = _FakeRuntime()
    scheduler = TaskScheduler(
        tasks_path=_write_tasks(tmp_path, heartbeats=[heartbeat], crons=[]),
        state_path=tmp_path / "scheduler_state.json",
        runtimes=[runtime],
        authorized_id=123,
    )
    scheduler.state["heartbeats"][heartbeat["id"]] = time.time() - 120

    await _run_one_scheduler_pass(scheduler)

    assert len(runtime.enqueued) == 1
    assert runtime.enqueued[0][1]["summary"] == "Heartbeat Task [heartbeat-1]"
    assert runtime.enqueued[0][1]["scheduler_context"] == {
        "kind": "heartbeat",
        "task_id": "heartbeat-1",
        "trigger": "recovery",
    }


def test_hourly_cron_occurrence_capture_counts_all_seven_missed_turns():
    last_run = datetime(2026, 8, 8, 22, 8, 32, tzinfo=SYDNEY).timestamp()
    now_dt = datetime(2026, 8, 9, 5, 45, 40, tzinfo=SYDNEY)

    captured = scheduler_module.scheduler_recovery.collect_cron_occurrences(
        "8 * * * *",
        last_run,
        now_dt,
        croniter_cls=_HourlyCroniter,
        timezone_name="Australia/Sydney",
    )

    assert captured["missed_count"] == 7
    assert [
        datetime.fromtimestamp(value, tz=timezone.utc)
        .astimezone(SYDNEY)
        .strftime("%H:%M")
        for value in captured["due_at"]
    ] == [
        "23:08",
        "00:08",
        "01:08",
        "02:08",
        "03:08",
        "04:08",
        "05:08",
    ]


def test_recovery_context_delegates_natural_language_to_agent_and_typed_tool():
    item = {
        "task_id": "hourly-hello",
        "kind": "cron",
        "missed_count": 7,
        "replay_limit": 1,
        "due_at": list(range(7)),
    }
    batch = {"batch_id": "batch-1", "status": "pending", "items": [item]}

    assert scheduler_module.scheduler_recovery.replayable_count(item) == 1
    context = scheduler_module.scheduler_recovery.render_context(
        [batch],
        now_ts=time.time(),
    )
    assert "ordinary conversation" in context
    assert "hashi_scheduler_recovery_resolve" in context
    assert "exact phrases" not in context
    assert "Accepted direct choices" not in context


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
async def test_typed_recovery_resolution_replays_latest_occurrences(
    tmp_path,
    monkeypatch,
):
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
        "her_v2_effort": "high",
        "timezone": "Australia/Sydney",
    }
    runtime = _FakeRuntime()
    scheduler = TaskScheduler(
        tasks_path=_write_tasks(tmp_path, heartbeats=[], crons=[cron]),
        state_path=tmp_path / "scheduler_state.json",
        runtimes=[runtime],
        authorized_id=123,
    )
    context_refreshes = []

    async def broadcast_topology():
        context_refreshes.append(True)

    scheduler.orchestrator = SimpleNamespace(
        runtimes=[runtime],
        function_workers=SimpleNamespace(broadcast_topology=broadcast_topology),
    )
    occurrences = scheduler_module.scheduler_recovery.collect_cron_occurrences(
        cron["schedule"],
        datetime(2026, 8, 8, 22, 8, 32, tzinfo=SYDNEY).timestamp(),
        datetime(2026, 8, 9, 5, 45, 40, tzinfo=SYDNEY),
        croniter_cls=_HourlyCroniter,
        timezone_name="Australia/Sydney",
    )
    batch = scheduler._create_recovery_batch(
        agent_name="zelda",
        items=[{"job": cron, "kind": "cron", **occurrences}],
        now_ts=datetime(2026, 8, 9, 5, 45, 40, tzinfo=SYDNEY).timestamp(),
    )
    batch["notice_status"] = "sent"

    result = await scheduler.resolve_recovery_batch(
        agent_name="zelda",
        batch_id=batch["batch_id"],
        action="rerun_selected",
        counts={"hourly-hello": 3},
        runtime_map={"zelda": runtime},
    )

    assert result["state_changed"] is True
    assert result["resolution"]["executed_total"] == 3
    assert len(runtime.enqueued) == 3
    assert [
        payload["prompt"].split("originally due at ", 1)[1].split(".", 1)[0]
        for _request_id, payload in runtime.enqueued
    ] == [
        "2026-08-09T03:08+10:00 AEST [Australia/Sydney]",
        "2026-08-09T04:08+10:00 AEST [Australia/Sydney]",
        "2026-08-09T05:08+10:00 AEST [Australia/Sydney]",
    ]
    assert [
        payload["scheduler_context"]
        for _request_id, payload in runtime.enqueued
    ] == [
        {
            "kind": "cron",
            "task_id": "hourly-hello",
            "trigger": "recovery",
        }
    ] * 3
    assert batch["status"] == "resolved"
    context = scheduler.build_recovery_context("zelda")
    assert "RECENTLY RESOLVED RECOVERY BATCHES" in context
    assert "executed=3" in context
    assert "missed=7" in context
    assert context_refreshes == [True]

    repeated = await scheduler.resolve_recovery_batch(
        agent_name="zelda",
        batch_id=batch["batch_id"],
        action="rerun_selected",
        counts={"hourly-hello": 3},
        runtime_map={"zelda": runtime},
    )
    assert repeated["state_changed"] is False
    assert repeated["resolution"]["executed_total"] == 3
    assert len(runtime.enqueued) == 3
    assert context_refreshes == [True]


def test_scheduler_wall_time_policy_covers_aest_aedt_fold_and_gap():
    winter = resolve_local_wall_time(
        datetime(2026, 8, 9, 9, 0),
        "Australia/Sydney",
    )
    summer = resolve_local_wall_time(
        datetime(2026, 12, 9, 9, 0),
        "Australia/Sydney",
    )
    fold = resolve_local_wall_time(
        datetime(2026, 4, 5, 2, 30),
        "Australia/Sydney",
    )
    gap = resolve_local_wall_time(
        datetime(2026, 10, 4, 2, 30),
        "Australia/Sydney",
    )
    after_second_fold = scheduler_module.next_cron_occurrence(
        "30 2 * * *",
        now=datetime(2026, 4, 5, 2, 15, tzinfo=SYDNEY, fold=1),
        timezone_name="Australia/Sydney",
    )

    assert winter.utcoffset() == timedelta(hours=10)
    assert summer.utcoffset() == timedelta(hours=11)
    assert fold.fold == 0
    assert fold.utcoffset() == timedelta(hours=11)
    assert (gap.hour, gap.minute) == (3, 0)
    assert gap.utcoffset() == timedelta(hours=11)
    assert after_second_fold == datetime(2026, 4, 6, 2, 30, tzinfo=SYDNEY)


@pytest.mark.asyncio
async def test_scheduled_job_with_retired_effort_field_is_still_queued(tmp_path):
    cron = {
        "id": "broken-cron",
        "agent": "zelda",
        "enabled": True,
        "schedule": "0 12 * * *",
        "prompt": "Run",
        "her_v2_effort": "turbo",
    }
    runtime = _FakeRuntime()
    scheduler = TaskScheduler(
        tasks_path=_write_tasks(tmp_path, heartbeats=[], crons=[cron]),
        state_path=tmp_path / "scheduler_state.json",
        runtimes=[runtime],
        authorized_id=123,
    )

    ok = await scheduler._fire_cron_job(
        cron,
        runtime_map={"zelda": runtime},
        tasks={"heartbeats": [], "crons": [cron], "nudges": []},
        now_dt=datetime(2026, 8, 21, 12, 0),
    )

    assert ok is True
    assert len(runtime.enqueued) == 1
    assert runtime.enqueued[0][1]["scheduler_context"] == {
        "kind": "cron",
        "task_id": "broken-cron",
        "trigger": "scheduled",
    }
