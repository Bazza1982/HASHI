from __future__ import annotations

import sys
import types
from types import SimpleNamespace

import pytest

sys.modules.setdefault("edge_tts", types.ModuleType("edge_tts"))

from orchestrator import runtime_logo


def _update():
    return SimpleNamespace(effective_user=SimpleNamespace(id=123))


def _context(*args):
    return SimpleNamespace(args=list(args))


def _runtime():
    replies = []
    runtime = SimpleNamespace(_is_authorized_user=lambda user_id: True)

    async def _reply_text(update, text, **kwargs):
        replies.append((text, kwargs))

    runtime._reply_text = _reply_text
    return runtime, replies


@pytest.mark.asyncio
async def test_cmd_logo_runs_animation_and_reports_success(monkeypatch):
    runtime, replies = _runtime()
    calls = []

    async def _run_in_executor(executor, func):
        calls.append(func)
        func()

    from orchestrator import agent_runtime

    monkeypatch.setattr(agent_runtime, "_show_logo_animation", lambda: calls.append("played"))
    monkeypatch.setattr(runtime_logo.asyncio, "get_running_loop", lambda: SimpleNamespace(run_in_executor=_run_in_executor))

    await runtime_logo.cmd_logo(runtime, _update(), _context())

    assert calls == [agent_runtime._show_logo_animation, "played"]
    assert replies[-1][0] == "Logo displayed in console."
