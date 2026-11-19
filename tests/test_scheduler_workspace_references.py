"""Task text is not an authorization check for the resources it mentions."""
from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from orchestrator import scheduler as scheduler_module
from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime
from orchestrator.scheduler import TaskScheduler
from orchestrator.skill_manager import SkillManager
from tests.test_scheduler_missed_recovery import _FakeRuntime
from tools.registry import ToolRegistry


PROMPT = "Summarize the note mentioning workspaces/lily/wiki_state.sqlite."


async def _run_until_dispatched(scheduler, runtime):
    task = asyncio.create_task(scheduler.run())
    try:
        async def wait_for_request():
            while not runtime.enqueued:
                await asyncio.sleep(0.01)
        await asyncio.wait_for(wait_for_request(), timeout=5)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def test_import_and_enable_do_not_change_due_to_workspace_words(tmp_path):
    path = tmp_path / "tasks.json"
    path.write_text(json.dumps({"heartbeats": [], "crons": [], "nudges": []}), encoding="utf-8")
    manager = SkillManager(tmp_path, path)
    job = {"id": "reference", "agent": "zelda", "enabled": True,
           "interval_seconds": 600, "prompt": PROMPT}
    ok, _ = manager.import_job("heartbeat", job)
    assert ok
    assert manager.get_job("heartbeat", "reference")["enabled"] is True
    assert manager.set_job_enabled("heartbeat", "reference", False)[0]
    assert manager.set_job_enabled("heartbeat", "reference", True)[0]
    ok, _, updated = manager.update_owned_job(
        "heartbeat", "reference", agent_name="zelda", updates={"prompt": PROMPT},
    )
    assert ok and updated["prompt"] == PROMPT
    ok, _, _ = manager.update_owned_job(
        "heartbeat", "reference", agent_name="lily", updates={"prompt": "wrong owner"},
    )
    assert not ok
    assert manager.get_job("heartbeat", "reference")["prompt"] == PROMPT


@pytest.mark.asyncio
async def test_manual_run_accepts_workspace_words_without_accessing_them():
    runtime = SimpleNamespace(
        _primary_chat_id=lambda: 7, enqueue_request=AsyncMock(return_value="req"),
        send_long_message=AsyncMock(),
    )
    job = {"id": "reference", "agent": "zelda", "prompt": PROMPT}
    ok, _message = await FlexibleAgentRuntime._run_job_now(runtime, job, kind="heartbeat")
    assert ok
    assert runtime.enqueue_request.await_args.kwargs["prompt"] == PROMPT


@pytest.mark.asyncio
async def test_due_heartbeat_is_dispatched_despite_workspace_words(tmp_path):
    job = {"id": "reference", "agent": "zelda", "enabled": True,
           "interval_seconds": 600, "prompt": PROMPT}
    path = tmp_path / "tasks.json"
    path.write_text(json.dumps({"heartbeats": [job], "crons": [], "nudges": []}), encoding="utf-8")
    runtime = _FakeRuntime()
    scheduler = TaskScheduler(path, tmp_path / "scheduler_state.json",
                              runtimes=[runtime], authorized_id=7)
    await _run_until_dispatched(scheduler, runtime)
    assert [entry[1]["prompt"] for entry in runtime.enqueued] == [PROMPT]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["nudge", "cron"])
async def test_other_due_tasks_are_dispatched_despite_workspace_words(tmp_path, monkeypatch, kind):
    job = {"id": "reference", "agent": "zelda", "enabled": True, "prompt": PROMPT}
    key = "nudges" if kind == "nudge" else "crons"
    if kind == "nudge":
        job["interval_seconds"] = 60
    else:
        job["schedule"] = "* * * * *"
        monkeypatch.setattr(scheduler_module, "_should_fire", lambda *_args: 1)
    tasks = {"heartbeats": [], "crons": [], "nudges": []}
    tasks[key] = [job]
    path = tmp_path / "tasks.json"
    path.write_text(json.dumps(tasks), encoding="utf-8")
    runtime = _FakeRuntime()
    scheduler = TaskScheduler(path, tmp_path / "scheduler_state.json",
                              runtimes=[runtime], authorized_id=7)
    scheduler._startup_recovery_pending = False
    scheduler.state[key]["reference"] = time.time() - 60
    scheduler._agent_has_managed_background_work = AsyncMock(return_value=False)
    await _run_until_dispatched(scheduler, runtime)
    assert [entry[1]["prompt"] for entry in runtime.enqueued] == [PROMPT]


@pytest.mark.asyncio
async def test_ordinary_file_tool_still_refuses_outside_its_access_roots(tmp_path):
    allowed, other = tmp_path / "allowed", tmp_path / "other"
    allowed.mkdir()
    other.mkdir()
    (other / "private.txt").write_text("fixture", encoding="utf-8")
    registry = ToolRegistry(allowed_tools=["file_read"], access_root=allowed,
                            workspace_dir=allowed, secrets={})
    result = await registry.execute("file_read", {"path": str(other / "private.txt")})
    assert result.is_error


@pytest.mark.asyncio
@pytest.mark.parametrize("duplicate", [False, True])
async def test_recovery_does_not_run_a_reassigned_or_ambiguous_task(tmp_path, duplicate):
    original = {"id": "reference", "agent": "zelda", "enabled": True,
                "interval_seconds": 600, "prompt": PROMPT}
    moved = {**original, "agent": "lily", "prompt": "a different owner's task"}
    jobs = [moved, original] if duplicate else [moved]
    path = tmp_path / "tasks.json"
    path.write_text(json.dumps({"heartbeats": jobs, "crons": [], "nudges": []}), encoding="utf-8")
    zelda, lily = _FakeRuntime(), _FakeRuntime()
    lily.name = "lily"
    scheduler = TaskScheduler(path, tmp_path / "scheduler_state.json",
                              runtimes=[zelda, lily], authorized_id=7)
    due = time.time() - 60
    batch = scheduler._create_recovery_batch(
        agent_name="zelda",
        items=[{"job": original, "kind": "heartbeat", "missed_count": 1,
                "first_due_at": due, "last_due_at": due, "due_at": [due]}],
    )
    result = await scheduler.resolve_recovery_batch(
        agent_name="zelda", batch_id=batch["batch_id"], action="rerun_all",
        counts=None, runtime_map={"zelda": zelda, "lily": lily},
    )
    assert result["resolution"]["executed_total"] == 0
    assert result["resolution"]["failed_total"] == 1
    assert zelda.enqueued == []
    assert lily.enqueued == []


@pytest.mark.asyncio
async def test_recovery_runs_same_agent_task_with_workspace_words(tmp_path):
    job = {"id": "reference", "agent": "zelda", "enabled": True,
           "interval_seconds": 600, "prompt": PROMPT}
    path = tmp_path / "tasks.json"
    path.write_text(json.dumps({"heartbeats": [job], "crons": [], "nudges": []}), encoding="utf-8")
    runtime = _FakeRuntime()
    scheduler = TaskScheduler(path, tmp_path / "scheduler_state.json",
                              runtimes=[runtime], authorized_id=7)
    due = time.time() - 60
    batch = scheduler._create_recovery_batch(
        agent_name="zelda",
        items=[{"job": job, "kind": "heartbeat", "missed_count": 1,
                "first_due_at": due, "last_due_at": due, "due_at": [due]}],
    )
    result = await scheduler.resolve_recovery_batch(
        agent_name="zelda", batch_id=batch["batch_id"], action="rerun_all",
        counts=None, runtime_map={"zelda": runtime},
    )
    assert result["resolution"]["executed_total"] == 1
    assert PROMPT in runtime.enqueued[0][1]["prompt"]
