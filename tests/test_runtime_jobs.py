from __future__ import annotations

from types import SimpleNamespace

import pytest

from orchestrator import runtime_jobs


def _update():
    return SimpleNamespace(effective_user=SimpleNamespace(id=123))


def _context(*args):
    return SimpleNamespace(args=list(args))


@pytest.mark.asyncio
async def test_cmd_jobs_uses_current_agent_name_by_default(monkeypatch):
    replies = []
    runtime = SimpleNamespace(
        name="lin_yueru",
        skill_manager=object(),
        _is_authorized_user=lambda user_id: True,
    )

    async def _reply_text(update, text, **kwargs):
        replies.append((text, kwargs))

    runtime._reply_text = _reply_text

    calls = []

    def _fake_build_jobs_with_buttons(agent_name, skill_manager, filter_agent=None):
        calls.append((agent_name, skill_manager, filter_agent))
        return "jobs text", "jobs markup"

    monkeypatch.setattr("orchestrator.agent_runtime._build_jobs_with_buttons", _fake_build_jobs_with_buttons)

    await runtime_jobs.cmd_jobs(runtime, _update(), _context())

    assert calls == [("lin_yueru", runtime.skill_manager, "lin_yueru")]
    assert replies[-1][0] == "jobs text"
    assert replies[-1][1]["parse_mode"] == "HTML"
    assert replies[-1][1]["reply_markup"] == "jobs markup"


@pytest.mark.asyncio
async def test_cmd_jobs_accepts_all_filter(monkeypatch):
    runtime = SimpleNamespace(
        name="lin_yueru",
        skill_manager=object(),
        _is_authorized_user=lambda user_id: True,
        _reply_text=lambda *args, **kwargs: None,
    )

    calls = []

    def _fake_build_jobs_with_buttons(agent_name, skill_manager, filter_agent=None):
        calls.append((agent_name, skill_manager, filter_agent))
        return "jobs text", None

    monkeypatch.setattr("orchestrator.agent_runtime._build_jobs_with_buttons", _fake_build_jobs_with_buttons)

    async def _reply_text(update, text, **kwargs):
        return None

    runtime._reply_text = _reply_text

    await runtime_jobs.cmd_jobs(runtime, _update(), _context("all"))

    assert calls == [("lin_yueru", runtime.skill_manager, None)]
