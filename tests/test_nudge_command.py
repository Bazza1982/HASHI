from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from orchestrator.runtime_jobs import CALLBACK_DATA_LIMIT, mint_callback_token
from orchestrator.runtime_nudge import build_nudge_with_buttons, handle_nudge_callback, parse_nudge_create_args
from orchestrator import scheduler as scheduler_module
from orchestrator.scheduler import TaskScheduler, _should_fire
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


def test_should_fire_returns_missed_seconds_for_due_cron():
    now = datetime(2026, 5, 18, 12, 0)
    last_run = (now - timedelta(days=1)).timestamp()

    missed_by = _should_fire("0 12 * * *", last_run, now)

    assert missed_by == 0.0


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
    assert payload["source"] == "scheduler"
    assert request_id in runtime.listeners
    data = json.loads((tmp_path / "tasks.json").read_text(encoding="utf-8"))
    assert data["nudges"][0]["nudge_meta"]["count"] == 1


@pytest.mark.asyncio
async def test_scheduler_skips_stale_missed_cron_and_notifies(tmp_path):
    tasks_path = tmp_path / "tasks.json"
    tasks_path.write_text(
        json.dumps(
            {
                "heartbeats": [],
                "nudges": [],
                "crons": [
                    {
                        "id": "daily-old",
                        "agent": "zelda",
                        "enabled": True,
                        "schedule": "0 12 * * *",
                        "prompt": "run stale task",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    runtime = FakeRuntime(busy=False)
    scheduler = TaskScheduler(
        tasks_path=tasks_path,
        state_path=tmp_path / "scheduler_state.json",
        runtimes=[runtime],
        authorized_id=123,
    )
    scheduler.state["crons"]["daily-old"] = time.time() - 7200
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(scheduler_module, "_should_fire", lambda schedule, last_run_ts, now_dt: 7200.0)

    try:
        await _run_one_scheduler_pass(scheduler)
    finally:
        monkeypatch.undo()

    assert len(runtime.enqueued) == 1
    _request_id, payload = runtime.enqueued[0]
    assert payload["summary"] == "Missed Cron [daily-old]"
    assert "已跳过自动补发" in payload["prompt"]
    assert scheduler.state["missed_crons"]["daily-old"]["agent"] == "zelda"


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


@pytest.mark.asyncio
async def test_manual_nudge_trigger_enqueues_scheduler_source(tmp_path):
    manager = SkillManager(project_root=tmp_path, tasks_path=tmp_path / "tasks.json")
    job = manager.create_nudge_job(
        agent_name="zelda",
        interval_minutes=1,
        exit_condition="until complete",
    )

    class Runtime:
        name = "zelda"
        skill_manager = manager

        def __init__(self):
            self.enqueued = []

        async def enqueue_request(self, **kwargs):
            self.enqueued.append(kwargs)
            return "req-1"

        def _primary_chat_id(self):
            return 123

    class Query:
        message = SimpleNamespace(chat_id=456)

        def __init__(self):
            self.answers = []

        async def answer(self, text=None, **kwargs):
            self.answers.append((text, kwargs))

    runtime = Runtime()
    query = Query()

    handled = await handle_nudge_callback(runtime, query, f"nudgejob:trigger:{job['id']}:now")

    assert handled is True
    assert query.answers[0][0] == "Triggering nudge now…"
    assert runtime.enqueued == [
        {
            "chat_id": 456,
            "prompt": job["prompt"],
            "source": "scheduler",
            "summary": f"Nudge Manual Trigger [{job['id']}]",
        }
    ]


def test_build_nudge_with_buttons_uses_short_callbacks_for_long_ids():
    long_id = "lin_yueru-nudge-until-hashi-remote-watchdog-stays-healthy-for-seven-days"

    class SkillManager:
        def list_jobs(self, kind, agent_name=None):
            return [
                {
                    "id": long_id,
                    "agent": "zelda",
                    "enabled": True,
                    "interval_seconds": 300,
                    "exit_condition": "until healthy",
                    "nudge_meta": {"count": 1, "max": 100},
                }
            ]

    runtime = SimpleNamespace()
    text, markup = build_nudge_with_buttons(SkillManager(), "zelda", runtime=runtime)

    assert long_id in text
    callbacks = [
        button.callback_data
        for row in markup.inline_keyboard
        for button in row
        if button.callback_data and button.callback_data != "noop"
    ]
    assert callbacks
    assert all(len(callback_data) <= CALLBACK_DATA_LIMIT for callback_data in callbacks)
    assert any(callback_data.startswith("nudgejob:key:") for callback_data in callbacks)


@pytest.mark.asyncio
async def test_tokenized_nudge_toggle_callback():
    class SkillManager:
        def __init__(self):
            self.toggles = []

        def set_job_enabled(self, kind, task_id, enabled=False):
            self.toggles.append((kind, task_id, enabled))
            return True, "ok"

        def list_jobs(self, kind, agent_name=None):
            return []

    class Runtime:
        name = "zelda"

        def __init__(self):
            self.skill_manager = SkillManager()

    class Query:
        def __init__(self):
            self.answers = []
            self.edits = []

        async def answer(self, text=None, **kwargs):
            self.answers.append((text, kwargs))

        async def edit_message_text(self, text, **kwargs):
            self.edits.append((text, kwargs))

    runtime = Runtime()
    token = mint_callback_token(
        runtime,
        "nudgejob_action",
        {"task_id": "nudge-123", "action": "toggle", "value": "off"},
        prefix="nj",
    )
    query = Query()

    handled = await handle_nudge_callback(runtime, query, f"nudgejob:key:{token}:toggle")

    assert handled is True
    assert runtime.skill_manager.toggles == [("nudge", "nudge-123", False)]
    assert query.answers[-1][0] == "ok"


@pytest.mark.asyncio
async def test_tokenized_nudge_delete_callback():
    class SkillManager:
        def __init__(self):
            self.deleted = []

        def delete_job(self, kind, task_id):
            self.deleted.append((kind, task_id))
            return True, "deleted"

        def list_jobs(self, kind, agent_name=None):
            return []

    class Runtime:
        name = "zelda"

        def __init__(self):
            self.skill_manager = SkillManager()

    class Query:
        def __init__(self):
            self.answers = []
            self.edits = []

        async def answer(self, text=None, **kwargs):
            self.answers.append((text, kwargs))

        async def edit_message_text(self, text, **kwargs):
            self.edits.append((text, kwargs))

    runtime = Runtime()
    token = mint_callback_token(
        runtime,
        "nudgejob_action",
        {"task_id": "nudge-123", "action": "delete"},
        prefix="nj",
    )
    query = Query()

    handled = await handle_nudge_callback(runtime, query, f"nudgejob:key:{token}:delete")

    assert handled is True
    assert runtime.skill_manager.deleted == [("nudge", "nudge-123")]
    assert query.answers[-1][0] == "deleted"
    assert query.edits


@pytest.mark.asyncio
async def test_expired_nudge_token_shows_alert():
    class Runtime:
        def __init__(self):
            self.skill_manager = SimpleNamespace()

    class Query:
        def __init__(self):
            self.answers = []

        async def answer(self, text=None, **kwargs):
            self.answers.append((text, kwargs))

    runtime = Runtime()
    query = Query()

    handled = await handle_nudge_callback(runtime, query, "nudgejob:key:njdead:trigger")

    assert handled is True
    assert query.answers[-1][0] == "This nudge action expired. Open /nudge again."
    assert query.answers[-1][1]["show_alert"] is True
