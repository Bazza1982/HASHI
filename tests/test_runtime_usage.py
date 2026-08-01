from __future__ import annotations

import sys
import types
from types import SimpleNamespace

import pytest

from orchestrator import runtime_usage


def _runtime(tmp_path):
    replies = []

    async def reply(_update, text, **kwargs):
        replies.append((text, kwargs))

    runtime = SimpleNamespace(
        name="lin_yueru",
        workspace_dir=tmp_path,
        session_id_dt="session",
        orchestrator=None,
        _is_authorized_user=lambda user_id: user_id == 1,
        _reply_text=reply,
    )
    return runtime, replies


@pytest.mark.asyncio
async def test_usage_summary_is_owned_by_runtime_usage_module(tmp_path, monkeypatch):
    tracker = types.ModuleType("tools.token_tracker")
    tracker.get_summary = lambda *_args, **_kwargs: {"all_time": {"requests": 1}}
    tracker.format_summary_text = lambda summary, agent_name: f"{agent_name}:1"
    monkeypatch.setitem(sys.modules, "tools.token_tracker", tracker)
    runtime, replies = _runtime(tmp_path)
    update = SimpleNamespace(effective_user=SimpleNamespace(id=1))

    await runtime_usage.cmd_usage(runtime, update, SimpleNamespace(args=[]))

    assert replies == [("lin_yueru:1", {"parse_mode": "HTML"})]


@pytest.mark.asyncio
async def test_token_summary_handles_no_recorded_usage(tmp_path, monkeypatch):
    tracker = types.ModuleType("tools.token_tracker")
    tracker.fmt_tokens = str
    tracker.get_summary_extended = lambda *_args, **_kwargs: {
        "all_time": {"requests": 0}
    }
    monkeypatch.setitem(sys.modules, "tools.token_tracker", tracker)
    runtime, replies = _runtime(tmp_path)
    runtime.orchestrator = SimpleNamespace(
        runtimes=[
            SimpleNamespace(
                workspace_dir=tmp_path,
                session_id_dt="session",
            )
        ]
    )
    update = SimpleNamespace(effective_user=SimpleNamespace(id=1))

    await runtime_usage.cmd_token(runtime, update, SimpleNamespace(args=[]))

    assert replies == [("📊 No token usage recorded yet.", {})]
