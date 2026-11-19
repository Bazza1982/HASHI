from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from orchestrator import runtime_jobs
from orchestrator.job_ownership import ownership_mismatch_label, resource_owner_mismatches
from orchestrator.runtime_jobs import _build_jobs_text, _build_jobs_with_buttons
from orchestrator.skill_manager import SkillManager


def test_resource_owner_mismatch_detects_cross_agent_workspace_path():
    job = {
        "id": "zelda-loop-9ada33",
        "agent": "zelda",
        "prompt": "Read /home/user/projects/hashi/workspaces/lily/wiki_state.sqlite",
    }

    assert resource_owner_mismatches(job) == ["lily"]
    assert ownership_mismatch_label(job) == "resource owner mismatch: workspaces/lily"


def test_resource_owner_mismatch_allows_own_workspace_path():
    job = {
        "id": "lily-loop-9ada33",
        "agent": "lily",
        "prompt": "Read /home/user/projects/hashi/workspaces/lily/wiki_state.sqlite",
    }

    assert resource_owner_mismatches(job) == []
    assert ownership_mismatch_label(job) is None


def test_resource_owner_mismatch_detects_windows_workspace_path():
    job = {
        "id": "zelda-loop-9ada33",
        "agent": "zelda",
        "prompt": r"Read C:\Users\sampleuser\projects\hashi\workspaces\lily\wiki_state.sqlite",
    }

    assert resource_owner_mismatches(job) == ["lily"]


def test_skill_manager_allows_enable_with_other_workspace_mentioned(tmp_path):
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
                        "prompt": "Check /home/user/projects/hashi/workspaces/lily/wiki_state.sqlite",
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

    assert ok is True
    assert "now ON" in message
    saved = json.loads(tasks_path.read_text(encoding="utf-8"))
    assert saved["heartbeats"][0]["enabled"] is True


def test_skill_manager_import_preserves_enabled_state_with_workspace_mention(tmp_path):
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
            "prompt": "Check /home/user/projects/hashi/workspaces/lily/wiki_state.sqlite",
        },
    )

    assert ok is True
    assert "Imported job" in message
    saved = json.loads(tasks_path.read_text(encoding="utf-8"))
    imported = saved["heartbeats"][0]
    assert imported["enabled"] is True
    assert "note" not in imported


def test_skill_manager_transfer_keeps_source_note_without_false_mismatch(tmp_path):
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
                        "prompt": "Check /home/user/projects/hashi/workspaces/lily/wiki_state.sqlite",
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
    assert "transferred from lily" in new_job["note"]
    assert "resource owner mismatch" not in new_job["note"]


def test_jobs_text_does_not_claim_mismatch_from_task_words(tmp_path):
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
                        "prompt": "Check /home/user/projects/hashi/workspaces/lily/wiki_state.sqlite",
                    }
                ],
                "crons": [],
            }
        ),
        encoding="utf-8",
    )
    skill_manager = SkillManager(tmp_path, tasks_path)

    text = _build_jobs_text("zelda", skill_manager)

    assert "resource owner mismatch" not in text


def test_jobs_with_buttons_do_not_claim_mismatch_from_task_words(tmp_path):
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
                        "prompt": "Check /home/user/projects/hashi/workspaces/lily/wiki_state.sqlite",
                    }
                ],
                "crons": [],
            }
        ),
        encoding="utf-8",
    )
    runtime = SimpleNamespace(_ui_callback_tokens={})
    skill_manager = SkillManager(tmp_path, tasks_path)

    text, markup = _build_jobs_with_buttons(runtime, "zelda", skill_manager, filter_agent="zelda")

    assert "resource owner mismatch" not in text
    assert "HERV3 model effort" in text
    assert "inherit current model effort" in text
    assert markup is not None


@pytest.mark.asyncio
async def test_jobs_run_callback_accepts_workspace_mention():
    class _SkillManager:
        def get_job(self, kind, task_id):
            return {
                "id": task_id,
                "agent": "zelda",
                "prompt": "Check /home/user/projects/hashi/workspaces/lily/wiki_state.sqlite",
            }

    class _Query:
        def __init__(self):
            self.answers = []

        async def answer(self, text=None, **kwargs):
            self.answers.append({"text": text, **kwargs})

    runtime = SimpleNamespace(skill_manager=_SkillManager(), _run_job_now=AsyncMock())
    query = _Query()

    handled = await runtime_jobs.handle_skill_job_callback(
        runtime,
        query,
        "skilljob:heartbeat:run:zelda-loop-9ada33:go",
    )

    assert handled is True
    assert not query.answers[-1].get("show_alert", False)
    assert runtime._run_job_now.await_count == 1
