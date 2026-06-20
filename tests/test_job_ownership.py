from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from orchestrator import runtime_jobs
from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime
from orchestrator.job_ownership import ownership_mismatch_label, resource_owner_mismatches
from orchestrator.runtime_jobs import _build_jobs_text, _build_jobs_with_buttons
from orchestrator.scheduler import TaskScheduler
from orchestrator.skill_manager import SkillManager


def test_resource_owner_mismatch_detects_cross_agent_workspace_path():
    job = {
        "id": "zelda-loop-9ada33",
        "agent": "zelda",
        "prompt": "Read /home/lily/projects/hashi/workspaces/lily/wiki_state.sqlite",
    }

    assert resource_owner_mismatches(job) == ["lily"]
    assert ownership_mismatch_label(job) == "resource owner mismatch: workspaces/lily"


def test_resource_owner_mismatch_allows_own_workspace_path():
    job = {
        "id": "lily-loop-9ada33",
        "agent": "lily",
        "prompt": "Read /home/lily/projects/hashi/workspaces/lily/wiki_state.sqlite",
    }

    assert resource_owner_mismatches(job) == []
    assert ownership_mismatch_label(job) is None


def test_resource_owner_mismatch_detects_windows_workspace_path():
    job = {
        "id": "zelda-loop-9ada33",
        "agent": "zelda",
        "prompt": r"Read C:\Users\lily\projects\hashi\workspaces\lily\wiki_state.sqlite",
    }

    assert resource_owner_mismatches(job) == ["lily"]


def test_scheduler_blocks_owner_mismatch_before_enqueue(tmp_path, caplog):
    scheduler = TaskScheduler(
        tmp_path / "tasks.json",
        tmp_path / "scheduler_state.json",
        runtimes=[],
        authorized_id=1,
    )
    job = {
        "id": "zelda-loop-9ada33",
        "agent": "zelda",
        "prompt": "Check /home/lily/projects/hashi/workspaces/lily/wiki_state.sqlite",
    }

    label = scheduler._job_owner_mismatch(
        job,
        task_kind="Heartbeat",
        task_id=job["id"],
        agent_name=job["agent"],
    )

    assert label == "resource owner mismatch: workspaces/lily"
    assert "Blocking Heartbeat zelda-loop-9ada33 for zelda" in caplog.text


def test_skill_manager_refuses_to_enable_owner_mismatch(tmp_path):
    tasks_path = tmp_path / "tasks.json"
    tasks_path.write_text(
        json.dumps(
            {
                "heartbeats": [
                    {
                        "id": "zelda-loop-9ada33",
                        "agent": "zelda",
                        "enabled": False,
                        "interval_seconds": 600,
                        "prompt": "Check /home/lily/projects/hashi/workspaces/lily/wiki_state.sqlite",
                    }
                ],
                "crons": [],
                "nudges": [],
            }
        ),
        encoding="utf-8",
    )
    manager = SkillManager(tmp_path, tasks_path)

    ok, message = manager.set_job_enabled("heartbeat", "zelda-loop-9ada33", True)

    assert ok is False
    assert "resource owner mismatch: workspaces/lily" in message
    saved = json.loads(tasks_path.read_text(encoding="utf-8"))
    assert saved["heartbeats"][0]["enabled"] is False


def test_skill_manager_imports_owner_mismatch_disabled(tmp_path):
    tasks_path = tmp_path / "tasks.json"
    tasks_path.write_text(json.dumps({"heartbeats": [], "crons": [], "nudges": []}), encoding="utf-8")
    manager = SkillManager(tmp_path, tasks_path)

    ok, message = manager.import_job(
        "heartbeat",
        {
            "id": "zelda-loop-9ada33",
            "agent": "zelda",
            "enabled": True,
            "interval_seconds": 600,
            "prompt": "Check /home/lily/projects/hashi/workspaces/lily/wiki_state.sqlite",
        },
    )

    assert ok is True
    assert "Imported job" in message
    saved = json.loads(tasks_path.read_text(encoding="utf-8"))
    imported = saved["heartbeats"][0]
    assert imported["enabled"] is False
    assert "resource owner mismatch: workspaces/lily" in imported["note"]


def test_skill_manager_transfer_marks_mismatch_for_review(tmp_path):
    tasks_path = tmp_path / "tasks.json"
    tasks_path.write_text(
        json.dumps(
            {
                "heartbeats": [
                    {
                        "id": "lily-loop-9ada33",
                        "agent": "lily",
                        "enabled": True,
                        "interval_seconds": 600,
                        "prompt": "Check /home/lily/projects/hashi/workspaces/lily/wiki_state.sqlite",
                    }
                ],
                "crons": [],
                "nudges": [],
            }
        ),
        encoding="utf-8",
    )
    manager = SkillManager(tmp_path, tasks_path)

    ok, message, new_job = manager.transfer_job("heartbeat", "lily-loop-9ada33", "zelda")

    assert ok is True
    assert "Transferred to zelda" in message
    assert new_job is not None
    assert new_job["enabled"] is False
    assert "resource owner mismatch: workspaces/lily" in new_job["note"]


def test_jobs_text_displays_owner_mismatch(tmp_path):
    tasks_path = tmp_path / "tasks.json"
    tasks_path.write_text(
        json.dumps(
            {
                "heartbeats": [
                    {
                        "id": "zelda-loop-9ada33",
                        "agent": "zelda",
                        "enabled": False,
                        "interval_seconds": 600,
                        "prompt": "Check /home/lily/projects/hashi/workspaces/lily/wiki_state.sqlite",
                    }
                ],
                "crons": [],
            }
        ),
        encoding="utf-8",
    )
    skill_manager = SimpleNamespace(tasks_path=tasks_path)

    text = _build_jobs_text("zelda", skill_manager)

    assert "resource owner mismatch: workspaces/lily" in text


def test_jobs_with_buttons_displays_owner_mismatch(tmp_path):
    tasks_path = tmp_path / "tasks.json"
    tasks_path.write_text(
        json.dumps(
            {
                "heartbeats": [
                    {
                        "id": "zelda-loop-9ada33",
                        "agent": "zelda",
                        "enabled": False,
                        "interval_seconds": 600,
                        "prompt": "Check /home/lily/projects/hashi/workspaces/lily/wiki_state.sqlite",
                    }
                ],
                "crons": [],
            }
        ),
        encoding="utf-8",
    )
    runtime = SimpleNamespace(_ui_callback_tokens={})
    skill_manager = SimpleNamespace(tasks_path=tasks_path)

    text, markup = _build_jobs_with_buttons(runtime, "zelda", skill_manager, filter_agent="zelda")

    assert "resource owner mismatch: workspaces/lily" in text
    assert markup is not None


@pytest.mark.asyncio
async def test_runtime_run_job_now_refuses_owner_mismatch():
    runtime = object.__new__(FlexibleAgentRuntime)
    runtime.sent = []
    runtime._primary_chat_id = lambda: 1

    async def _send_long_message(**kwargs):
        runtime.sent.append(kwargs)

    runtime.send_long_message = _send_long_message
    job = {
        "id": "zelda-loop-9ada33",
        "agent": "zelda",
        "prompt": "Check /home/lily/projects/hashi/workspaces/lily/wiki_state.sqlite",
    }

    ok, message = await FlexibleAgentRuntime._run_job_now(runtime, job)

    assert ok is False
    assert "resource owner mismatch: workspaces/lily" in message
    assert "Refusing to run job zelda-loop-9ada33" in runtime.sent[0]["text"]


@pytest.mark.asyncio
async def test_jobs_run_callback_refuses_owner_mismatch():
    class _SkillManager:
        def get_job(self, kind, task_id):
            return {
                "id": task_id,
                "agent": "zelda",
                "prompt": "Check /home/lily/projects/hashi/workspaces/lily/wiki_state.sqlite",
            }

    class _Query:
        def __init__(self):
            self.answers = []

        async def answer(self, text=None, **kwargs):
            self.answers.append({"text": text, **kwargs})

    runtime = SimpleNamespace(skill_manager=_SkillManager())
    query = _Query()

    handled = await runtime_jobs.handle_skill_job_callback(
        runtime,
        query,
        "skilljob:heartbeat:run:zelda-loop-9ada33:go",
    )

    assert handled is True
    assert query.answers[-1]["show_alert"] is True
    assert "resource owner mismatch: workspaces/lily" in query.answers[-1]["text"]
