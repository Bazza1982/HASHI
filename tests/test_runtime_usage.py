from __future__ import annotations

import sys
import types
from types import SimpleNamespace

import pytest

from orchestrator import runtime_usage


def _update():
    return SimpleNamespace(effective_user=SimpleNamespace(id=123))


def _context(*args):
    return SimpleNamespace(args=list(args))


def _runtime():
    replies = []
    runtime = SimpleNamespace(
        _is_authorized_user=lambda user_id: True,
        workspace_dir="/tmp/agent-a",
        session_id_dt="sess-a",
        name="agent-a",
        orchestrator=None,
    )

    async def _reply_text(update, text, **kwargs):
        replies.append((text, kwargs))

    runtime._reply_text = _reply_text
    return runtime, replies


def _install_token_tracker(monkeypatch, summary_fn, format_fn):
    token_tracker_module = types.ModuleType("tools.token_tracker")
    token_tracker_module.get_summary = summary_fn
    token_tracker_module.format_summary_text = format_fn
    monkeypatch.setitem(sys.modules, "tools.token_tracker", token_tracker_module)


@pytest.mark.asyncio
async def test_cmd_usage_reports_missing_tracker(monkeypatch):
    runtime, replies = _runtime()
    monkeypatch.setattr(runtime_usage.importlib, "import_module", lambda name: (_ for _ in ()).throw(ImportError()))

    await runtime_usage.cmd_usage(runtime, _update(), _context())

    assert replies[-1][0] == "❌ token_tracker not available."


@pytest.mark.asyncio
async def test_cmd_usage_reports_single_agent_summary(monkeypatch):
    runtime, replies = _runtime()

    def _get_summary(workspace_dir, session_id):
        assert workspace_dir == "/tmp/agent-a"
        assert session_id == "sess-a"
        return {"ok": True}

    def _format_summary_text(summary, agent_name):
        assert summary == {"ok": True}
        assert agent_name == "agent-a"
        return "<b>summary</b>"

    _install_token_tracker(monkeypatch, _get_summary, _format_summary_text)

    await runtime_usage.cmd_usage(runtime, _update(), _context())

    assert replies[-1][0] == "<b>summary</b>"
    assert replies[-1][1]["parse_mode"] == "HTML"


@pytest.mark.asyncio
async def test_cmd_usage_reports_missing_orchestrator_for_all_view(monkeypatch):
    runtime, replies = _runtime()
    _install_token_tracker(monkeypatch, lambda workspace_dir, session_id: {}, lambda summary, agent_name: "")

    await runtime_usage.cmd_usage(runtime, _update(), _context("all"))

    assert replies[-1][0] == "❌ Orchestrator unavailable for all-agents view."


@pytest.mark.asyncio
async def test_cmd_usage_reports_all_agents_summary(monkeypatch):
    runtime, replies = _runtime()
    runtime.orchestrator = SimpleNamespace(
        runtimes=[
            SimpleNamespace(name="agent-a", workspace_dir="/tmp/agent-a", session_id_dt="sess-a"),
            SimpleNamespace(name="agent-b", workspace_dir="/tmp/agent-b", session_id_dt="sess-b"),
        ]
    )

    def _get_summary(workspace_dir, session_id):
        if workspace_dir.endswith("agent-a"):
            return {
                "all_time": {"requests": 2, "input": 1200, "output": 800, "cost_usd": 1.5},
                "session": {"requests": 1, "input": 400, "output": 600, "cost_usd": 0.7},
            }
        return {
            "all_time": {"requests": 0, "input": 0, "output": 0, "cost_usd": 0.0},
            "session": {},
        }

    _install_token_tracker(monkeypatch, _get_summary, lambda summary, agent_name: "")

    await runtime_usage.cmd_usage(runtime, _update(), _context("all"))

    text = replies[-1][0]
    assert "<b>📊 Token Usage — All Agents</b>" in text
    assert "<b>agent-a</b>  2K tokens  $1.5000  (session 1K $0.7000)" in text
    assert "<b>Total: $1.5000</b>" in text
