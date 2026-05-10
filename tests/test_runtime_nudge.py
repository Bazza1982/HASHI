from __future__ import annotations

from types import SimpleNamespace

import pytest

from orchestrator import runtime_nudge


def _update():
    return SimpleNamespace(effective_user=SimpleNamespace(id=123))


def _context(*args):
    return SimpleNamespace(args=list(args))


class _SkillManager:
    def __init__(self):
        self.created = []
        self.enabled_calls = []

    def list_jobs(self, kind, agent_name=None):
        return [
            {
                "id": "lin_yueru-loop-922f1f",
                "enabled": True,
                "interval_seconds": 120,
                "exit_condition": "until done",
                "nudge_meta": {"count": 1, "max": 100},
            }
        ]

    def set_job_enabled(self, kind, task_id, enabled=False):
        self.enabled_calls.append((kind, task_id, enabled))

    def create_nudge_job(self, *, agent_name, interval_minutes, exit_condition):
        job = {
            "id": "lin_yueru-nudge-123456",
            "agent": agent_name,
            "interval_seconds": interval_minutes * 60,
            "exit_condition": exit_condition,
        }
        self.created.append((agent_name, interval_minutes, exit_condition))
        return job


def _runtime():
    replies = []
    runtime = SimpleNamespace(
        name="lin_yueru",
        skill_manager=_SkillManager(),
        _is_authorized_user=lambda user_id: True,
    )

    async def _reply_text(update, text, **kwargs):
        replies.append((text, kwargs))

    runtime._reply_text = _reply_text
    return runtime, replies


def test_parse_nudge_create_args_requires_minutes_and_exit_condition():
    minutes, exit_condition = runtime_nudge.parse_nudge_create_args("5 until the scan is done")

    assert minutes == 5
    assert exit_condition == "until the scan is done"


@pytest.mark.asyncio
async def test_cmd_nudge_shows_usage_by_default():
    runtime, replies = _runtime()

    await runtime_nudge.cmd_nudge(runtime, _update(), _context())

    assert "Idle Continuation Manager" in replies[-1][0]
    assert replies[-1][1]["parse_mode"] == "HTML"


@pytest.mark.asyncio
async def test_cmd_nudge_creates_job():
    runtime, replies = _runtime()

    await runtime_nudge.cmd_nudge(runtime, _update(), _context("2", "until", "done"))

    assert runtime.skill_manager.created == [("lin_yueru", 2, "until done")]
    assert "Nudge created" in replies[-1][0]


@pytest.mark.asyncio
async def test_cmd_nudge_lists_jobs():
    runtime, replies = _runtime()

    await runtime_nudge.cmd_nudge(runtime, _update(), _context("list"))

    assert "Nudges" in replies[-1][0]
    assert "lin_yueru-loop-922f1f" in replies[-1][0]


@pytest.mark.asyncio
async def test_cmd_nudge_stops_matching_jobs():
    runtime, replies = _runtime()

    await runtime_nudge.cmd_nudge(runtime, _update(), _context("stop", "922f1f"))

    assert runtime.skill_manager.enabled_calls == [("nudge", "lin_yueru-loop-922f1f", False)]
    assert "Stopped nudges" in replies[-1][0]
