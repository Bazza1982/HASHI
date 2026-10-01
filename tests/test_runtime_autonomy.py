from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime
from orchestrator.runtime_autonomy import admission_snapshot, pause, status
from orchestrator.scheduler import TaskScheduler
from orchestrator.workspace_state import WorkspaceStateStore


def _runtime(tmp_path):
    return SimpleNamespace(
        workspace_dir=tmp_path,
        backend_manager=SimpleNamespace(state_store=WorkspaceStateStore(tmp_path)),
    )


def test_stop_fence_persists_and_blocks_autonomous_wakeups_until_user_request(tmp_path):
    first = _runtime(tmp_path)
    paused = pause(first)
    assert paused["paused"] is True
    assert paused["generation"] == 1

    replacement = _runtime(tmp_path)
    assert admission_snapshot(replacement, "scheduler", {}) == (False, 1)
    assert admission_snapshot(replacement, "background-job-event", {}) == (False, 1)
    assert admission_snapshot(replacement, "hchat-exchange", {}) == (False, 1)
    assert admission_snapshot(
        replacement, "text", {"_hashi_autonomous_wakeup": "delayed"}
    ) == (False, 1)
    assert admission_snapshot(replacement, "api", {}) == (True, 1)
    assert status(_runtime(tmp_path))["paused"] is False


def test_new_stop_generation_invalidates_inflight_admission(tmp_path):
    runtime = _runtime(tmp_path)
    allowed, old_generation = admission_snapshot(runtime, "api", {})
    assert allowed is True
    pause(runtime)
    assert status(runtime)["generation"] != old_generation


def test_failed_stop_publication_still_blocks_current_worker(tmp_path, monkeypatch):
    runtime = _runtime(tmp_path)

    def fail(_mutator):
        raise OSError("disk unavailable")

    monkeypatch.setattr(runtime.backend_manager.state_store, "update", fail)
    with pytest.raises(OSError):
        pause(runtime)

    assert status(runtime)["paused"] is True
    assert admission_snapshot(runtime, "scheduler", {})[0] is False


@pytest.mark.asyncio
async def test_runtime_declines_background_event_before_session_or_queue_admission(tmp_path):
    runtime = FlexibleAgentRuntime.__new__(FlexibleAgentRuntime)
    runtime.workspace_dir = tmp_path
    runtime.logger = SimpleNamespace(warning=lambda *_args, **_kwargs: None)
    pause(runtime)

    request_id = await runtime.enqueue_request(
        1, "Background job finished", "background-job-event", "finished"
    )

    assert request_id is None


@pytest.mark.asyncio
async def test_scheduler_does_not_run_direct_cron_side_effect_after_stop(tmp_path):
    scheduler = TaskScheduler(
        tasks_path=tmp_path / "tasks.json",
        state_path=tmp_path / "scheduler_state.json",
        runtimes=[],
        authorized_id=1,
    )
    runtime = _runtime(tmp_path)
    runtime.export_daily_transcript = Mock(return_value=True)
    pause(runtime)
    cron = {"id": "export", "agent": "zelda", "action": "export_transcript"}

    ran = await scheduler._fire_cron_job(
        cron,
        runtime_map={"zelda": runtime},
        tasks={"crons": [cron]},
        now_dt=datetime(2026, 10, 2),
    )

    assert ran is False
    runtime.export_daily_transcript.assert_not_called()


@pytest.mark.asyncio
async def test_parked_topic_reminder_does_not_send_after_stop(tmp_path):
    runtime = FlexibleAgentRuntime.__new__(FlexibleAgentRuntime)
    runtime.workspace_dir = tmp_path
    pause(runtime)

    await runtime.process_parked_topic_followups()
