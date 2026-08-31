from __future__ import annotations

import sys
import types
from types import SimpleNamespace

import pytest

from orchestrator import runtime_usage, ui_language


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


@pytest.mark.asyncio
async def test_token_summary_includes_localized_cache_savings_statistics(
    tmp_path, monkeypatch
):
    tracker = types.ModuleType("tools.token_tracker")
    tracker.fmt_tokens = lambda value: (
        f"{value / 1_000_000:.3f}M"
        if value >= 1_000_000
        else f"{value / 1_000:.1f}K"
    )
    rich = {
        "input": 2_915_598,
        "output": 52_184,
        "thinking": 38_112,
        "cost_usd": 0.106091,
        "requests": 3,
        "provider_requests": 40,
        "provider_metrics_records": 1,
        "prompt_cache_hit_tokens": 2_683_136,
        "prompt_cache_miss_tokens": 232_462,
        "cache_observed_input_tokens": 2_915_598,
        "cache_metrics_records": 1,
        "no_cache_cost_usd": 1.096531,
        "no_cache_cost_known_records": 1,
        "cache_savings_usd": 0.990440,
        "cache_savings_known_records": 1,
        "pricing_revisions": ["2026-08-23.v1"],
        "thinking_in_output_tokens": 38_112,
        "separate_thinking_tokens": 0,
    }
    empty = {"input": 0, "output": 0, "thinking": 0, "cost_usd": 0.0, "requests": 0}
    tracker.get_summary_extended = lambda *_args, **_kwargs: {
        "all_time": dict(rich),
        "session": dict(rich),
        "weekly": dict(empty),
        "monthly": dict(empty),
    }
    monkeypatch.setitem(sys.modules, "tools.token_tracker", tracker)
    runtime, replies = _runtime(tmp_path)
    runtime.orchestrator = SimpleNamespace(
        runtimes=[
            SimpleNamespace(
                name="lily",
                workspace_dir=tmp_path,
                session_id_dt="session",
                backend_manager=SimpleNamespace(active_backend="her-v2"),
                config=SimpleNamespace(active_backend="her-v2"),
                get_current_model=lambda: "deepseek-v4-flash",
            )
        ]
    )
    update = SimpleNamespace(effective_user=SimpleNamespace(id=1))

    with ui_language.language_scope(SimpleNamespace(), locale="zh-CN"):
        await runtime_usage.cmd_token(runtime, update, SimpleNamespace(args=[]))

    text, kwargs = replies[0]
    assert kwargs == {"parse_mode": "HTML"}
    assert "Provider 请求 40" in text
    assert "输出:52.2K（其中推理 38.1K）" in text
    assert "缓存命中 2.683M/2.916M（92.0%）" in text
    assert "无缓存约 US$1.0965" in text
    assert "缓存节省约 US$0.9904（90.3%）" in text
    assert "价目表 2026-08-23.v1" in text
    assert "详细统计覆盖 1/3 条记录" in text
