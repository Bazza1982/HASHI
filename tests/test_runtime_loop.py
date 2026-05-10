from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestrator import runtime_loop


class _Message:
    def __init__(self, text: str):
        self.text = text


def _update(text="/loop"):
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=123),
        effective_chat=SimpleNamespace(id=456),
        message=_Message(text),
    )


def _context(*args):
    return SimpleNamespace(args=list(args))


def _runtime(skill_manager=None):
    replies = []
    queued = []
    runtime = SimpleNamespace(
        _is_authorized_user=lambda user_id: True,
        skill_manager=skill_manager,
        name="sunny",
    )

    async def _reply_text(update, text, **kwargs):
        replies.append((text, kwargs))

    async def enqueue_request(chat_id, prompt, source, summary):
        queued.append((chat_id, prompt, source, summary))

    runtime._reply_text = _reply_text
    runtime.enqueue_request = enqueue_request
    return runtime, replies, queued


@pytest.mark.asyncio
async def test_cmd_loop_shows_help_without_args():
    runtime, replies, queued = _runtime()

    await runtime_loop.cmd_loop(runtime, _update("/loop"), _context())

    assert "<b>Loop — Recurring Task Manager</b>" in replies[-1][0]
    assert replies[-1][1]["parse_mode"] == "HTML"


@pytest.mark.asyncio
async def test_cmd_loop_lists_jobs():
    class _SkillManager:
        def list_jobs(self, kind, agent_name):
            if kind == "heartbeat":
                return [{"id": "job-1", "enabled": True, "interval_seconds": 600, "loop_meta": {"count": 1, "max": 3, "task_summary": "task"}}]
            return []

    runtime, replies, queued = _runtime(skill_manager=_SkillManager())

    await runtime_loop.cmd_loop(runtime, _update("/loop list"), _context("list"))

    assert "<code>job-1</code>" in replies[-1][0]
    assert replies[-1][1]["parse_mode"] == "HTML"


@pytest.mark.asyncio
async def test_cmd_loop_stops_matching_jobs():
    calls = []

    class _SkillManager:
        def list_jobs(self, kind, agent_name):
            if kind == "heartbeat":
                return [{"id": "job-1", "enabled": True, "loop_meta": {"count": 1}}]
            return []

        def set_job_enabled(self, kind, job_id, enabled):
            calls.append((kind, job_id, enabled))

    runtime, replies, queued = _runtime(skill_manager=_SkillManager())

    await runtime_loop.cmd_loop(runtime, _update("/loop stop job"), _context("stop", "job"))

    assert calls == [("heartbeat", "job-1", False)]
    assert replies[-1][0] == "⏹ Stopped: job-1"


@pytest.mark.asyncio
async def test_cmd_loop_enqueues_loop_setup_prompt():
    skill_manager = SimpleNamespace(tasks_path=Path("/tmp/tasks.json"))
    runtime, replies, queued = _runtime(skill_manager=skill_manager)

    await runtime_loop.cmd_loop(runtime, _update("/loop check every 10 min"), _context("check", "every", "10", "min"))

    assert queued
    assert queued[-1][0] == 456
    assert queued[-1][2:] == ("loop_skill", "Loop setup")
    assert "--- USER REQUEST ---\ncheck every 10 min" in queued[-1][1]
    assert replies[-1][0] == "🔄 收到！正在理解任务并设置循环…"
