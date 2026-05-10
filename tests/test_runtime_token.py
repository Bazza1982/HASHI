from __future__ import annotations

import sys
import types
from types import SimpleNamespace

import pytest

from orchestrator import runtime_token


def _update():
    return SimpleNamespace(effective_user=SimpleNamespace(id=123))


def _context(*args):
    return SimpleNamespace(args=list(args))


def _runtime():
    replies = []
    runtime = SimpleNamespace(
        _is_authorized_user=lambda user_id: True,
        orchestrator=None,
    )

    async def _reply_text(update, text, **kwargs):
        replies.append((text, kwargs))

    runtime._reply_text = _reply_text
    return runtime, replies


def _install_token_tracker(monkeypatch, summary_fn, fmt_fn):
    token_tracker_module = types.ModuleType("tools.token_tracker")
    token_tracker_module.get_summary_extended = summary_fn
    token_tracker_module.fmt_tokens = fmt_fn
    monkeypatch.setitem(sys.modules, "tools.token_tracker", token_tracker_module)


@pytest.mark.asyncio
async def test_cmd_token_reports_missing_tracker(monkeypatch):
    runtime, replies = _runtime()
    monkeypatch.setattr(runtime_token.importlib, "import_module", lambda name: (_ for _ in ()).throw(ImportError()))

    await runtime_token.cmd_token(runtime, _update(), _context())

    assert replies[-1][0] == "❌ token_tracker not available."


@pytest.mark.asyncio
async def test_cmd_token_reports_missing_orchestrator(monkeypatch):
    runtime, replies = _runtime()
    _install_token_tracker(monkeypatch, lambda workspace_dir, session_id: {}, lambda value: f"{value}")

    await runtime_token.cmd_token(runtime, _update(), _context())

    assert replies[-1][0] == "❌ Orchestrator unavailable."


@pytest.mark.asyncio
async def test_cmd_token_reports_no_usage(monkeypatch):
    runtime, replies = _runtime()
    runtime.orchestrator = SimpleNamespace(
        runtimes=[SimpleNamespace(name="agent-a", workspace_dir="/tmp/a", session_id_dt="sess-a")]
    )

    _install_token_tracker(
        monkeypatch,
        lambda workspace_dir, session_id: {
            "all_time": {"requests": 0, "input": 0, "output": 0, "thinking": 0, "cost_usd": 0.0},
            "session": {},
            "weekly": {},
            "monthly": {},
        },
        lambda value: f"{value}",
    )

    await runtime_token.cmd_token(runtime, _update(), _context())

    assert replies[-1][0] == "📊 No token usage recorded yet."


@pytest.mark.asyncio
async def test_cmd_token_renders_grouped_summary(monkeypatch):
    runtime, replies = _runtime()

    class _Runtime(SimpleNamespace):
        def get_current_model(self):
            return self.model

    runtime.orchestrator = SimpleNamespace(
        runtimes=[
            _Runtime(
                name="alpha",
                workspace_dir="/tmp/a",
                session_id_dt="sess-a",
                backend_manager=SimpleNamespace(active_backend="codex-cli"),
                config=SimpleNamespace(active_backend=None),
                model="gpt-5.4",
            ),
            _Runtime(
                name="beta",
                workspace_dir="/tmp/b",
                session_id_dt="sess-b",
                backend_manager=SimpleNamespace(active_backend="openai-api"),
                config=SimpleNamespace(active_backend=None),
                model="gpt-5.4-mini",
            ),
        ]
    )

    def _get_summary_extended(workspace_dir, session_id):
        if workspace_dir.endswith("/a"):
            return {
                "all_time": {"requests": 2, "input": 1000, "output": 500, "thinking": 100, "cost_usd": 1.25},
                "session": {"requests": 1, "input": 100, "output": 50, "thinking": 10, "cost_usd": 0.25},
                "weekly": {"requests": 1, "input": 300, "output": 200, "thinking": 0, "cost_usd": 0.50},
                "monthly": {"requests": 2, "input": 1000, "output": 500, "thinking": 100, "cost_usd": 1.25},
            }
        return {
            "all_time": {"requests": 1, "input": 2000, "output": 800, "thinking": 0, "cost_usd": 2.50},
            "session": {"requests": 0, "input": 0, "output": 0, "thinking": 0, "cost_usd": 0.0},
            "weekly": {"requests": 1, "input": 500, "output": 200, "thinking": 0, "cost_usd": 0.75},
            "monthly": {"requests": 1, "input": 2000, "output": 800, "thinking": 0, "cost_usd": 2.50},
        }

    _install_token_tracker(monkeypatch, _get_summary_extended, lambda value: f"{value}")

    await runtime_token.cmd_token(runtime, _update(), _context())

    text = replies[-1][0]
    assert "<b>📊 Token Summary — All Agents</b>" in text
    assert "<b>🖥️ CLI Backends</b>" in text
    assert "<b>🌐 API Backends</b>" in text
    assert "alpha" in text and "beta" in text
    assert "<b>All-time</b>  2 agents  in:3000  out:1300  💭100  <b>$3.7500</b>  (3 req)" in text
    assert "<b>Session</b>    in:100  out:50  💭10  <b>$0.2500</b>" in text
    assert replies[-1][1]["parse_mode"] == "HTML"
