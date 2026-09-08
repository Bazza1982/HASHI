from __future__ import annotations

import sys
import types
from types import SimpleNamespace

import pytest

from orchestrator import runtime_usage, ui_language
from tools.token_tracker import format_status_line, get_summary, record_usage


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
    tracker.format_usage_cost = lambda data: f"${data.get('cost_usd', 0.0):.4f}"
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
    tracker.format_usage_cost = lambda data: f"${data.get('cost_usd', 0.0):.4f}"
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
    assert "服务提供方请求 40" in text
    assert "输出:52.2K（其中推理 38.1K）" in text
    assert "缓存命中 2.683M/2.916M（92.0%）" in text
    assert "无缓存约 US$1.0965" in text
    assert "缓存节省约 US$0.9904（90.3%）" in text
    assert "价目表 2026-08-23.v1" in text
    assert "详细统计覆盖 1/3 条记录" in text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("locale", "unknown_text"),
    [("en", "Cost unknown"), ("zh-CN", "成本未知")],
)
async def test_real_usage_and_status_keep_fully_unknown_cost_unknown(
    tmp_path, locale, unknown_text
):
    receipt = record_usage(
        tmp_path,
        model="unlisted-maintenance-synthetic-model",
        backend="hashi-api",
        engine="hashi-api",
        input_tokens=100,
        output_tokens=10,
        session_id="session",
        cost_usd=None,
        token_source="provider",
    )
    runtime, replies = _runtime(tmp_path)
    update = SimpleNamespace(effective_user=SimpleNamespace(id=1))

    with ui_language.language_scope(SimpleNamespace(), locale=locale):
        await runtime_usage.cmd_usage(runtime, update, SimpleNamespace(args=[]))
        summary = get_summary(tmp_path, session_id="session")
        status = format_status_line(summary)

    usage_text = replies[0][0]
    assert receipt.cost_usd is None
    assert summary["all_time"]["cost_usd"] == 0.0
    assert summary["all_time"]["unknown_cost_requests"] == 1
    assert unknown_text in usage_text
    assert unknown_text in status
    assert "$0.0000" not in usage_text
    assert "$0.0000" not in status


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("locale", "subtotal_text", "missing_text"),
    [
        ("en", "Known subtotal $0.0123", "unknown for 1 request"),
        ("zh-CN", "已知小计 $0.0123", "1 次请求成本未知"),
    ],
)
async def test_real_usage_and_status_mark_partially_known_cost(
    tmp_path, locale, subtotal_text, missing_text
):
    record_usage(
        tmp_path,
        model="mixed-cost-model",
        backend="hashi-api",
        engine="hashi-api",
        input_tokens=80,
        output_tokens=8,
        session_id="session",
        cost_usd=0.012345,
        token_source="provider",
    )
    record_usage(
        tmp_path,
        model="mixed-cost-model",
        backend="hashi-api",
        engine="hashi-api",
        input_tokens=20,
        output_tokens=2,
        session_id="session",
        cost_usd=None,
        token_source="provider",
    )
    runtime, replies = _runtime(tmp_path)
    update = SimpleNamespace(effective_user=SimpleNamespace(id=1))

    with ui_language.language_scope(SimpleNamespace(), locale=locale):
        await runtime_usage.cmd_usage(runtime, update, SimpleNamespace(args=[]))
        summary = get_summary(tmp_path, session_id="session")
        status = format_status_line(summary)

    usage_text = replies[0][0]
    assert summary["all_time"]["cost_usd"] == 0.012345
    assert summary["all_time"]["unknown_cost_requests"] == 1
    assert subtotal_text in usage_text
    assert missing_text in usage_text
    assert subtotal_text in status
    assert missing_text in status


@pytest.mark.asyncio
async def test_real_usage_keeps_provider_local_and_all_known_zero_exact(tmp_path):
    provider_dir = tmp_path / "provider"
    local_dir = tmp_path / "local"
    all_known_dir = tmp_path / "all-known"
    provider_dir.mkdir()
    local_dir.mkdir()
    all_known_dir.mkdir()

    provider_receipt = record_usage(
        provider_dir,
        model="provider-free-tier",
        backend="openrouter",
        engine="openrouter",
        input_tokens=10,
        output_tokens=1,
        session_id="session",
        cost_usd=0.0,
        token_source="provider",
    )
    local_receipt = record_usage(
        local_dir,
        model="llama3.1:8b",
        backend="ollama",
        engine="ollama",
        input_tokens=10,
        output_tokens=1,
        session_id="session",
        cost_usd=None,
    )
    record_usage(
        all_known_dir,
        model="provider-known",
        backend="openrouter",
        engine="openrouter",
        input_tokens=10,
        output_tokens=1,
        session_id="session",
        cost_usd=0.012345,
        token_source="provider",
    )
    record_usage(
        all_known_dir,
        model="local-known-zero",
        backend="ollama",
        engine="ollama",
        input_tokens=10,
        output_tokens=1,
        session_id="session",
        cost_usd=None,
    )

    assert provider_receipt.cost_usd == 0.0
    assert local_receipt.cost_usd == 0.0
    for workspace, expected in (
        (provider_dir, "$0.0000"),
        (local_dir, "$0.0000"),
        (all_known_dir, "$0.0123"),
    ):
        runtime, replies = _runtime(workspace)
        update = SimpleNamespace(effective_user=SimpleNamespace(id=1))
        with ui_language.language_scope(SimpleNamespace(), locale="en"):
            await runtime_usage.cmd_usage(runtime, update, SimpleNamespace(args=[]))
            summary = get_summary(workspace, session_id="session")
            status = format_status_line(summary)
        text = replies[0][0]
        assert summary["all_time"]["unknown_cost_requests"] == 0
        assert expected in text
        assert expected in status
        assert "Cost unknown" not in text
        assert "Cost unknown" not in status
        assert "Known subtotal" not in text
        assert "Known subtotal" not in status


@pytest.mark.asyncio
async def test_token_aggregate_keeps_unknown_cost_unknown(tmp_path):
    record_usage(
        tmp_path,
        model="unlisted-maintenance-synthetic-model",
        backend="hashi-api",
        engine="hashi-api",
        input_tokens=100,
        output_tokens=10,
        session_id="session",
        cost_usd=None,
        token_source="provider",
    )
    runtime, replies = _runtime(tmp_path)
    runtime.orchestrator = SimpleNamespace(
        runtimes=[
            SimpleNamespace(
                name="lily",
                workspace_dir=tmp_path,
                session_id_dt="session",
                backend_manager=SimpleNamespace(active_backend="hashi-api"),
                config=SimpleNamespace(active_backend="hashi-api"),
                get_current_model=lambda: "unlisted-maintenance-synthetic-model",
            )
        ]
    )
    update = SimpleNamespace(effective_user=SimpleNamespace(id=1))

    with ui_language.language_scope(SimpleNamespace(), locale="zh-CN"):
        await runtime_usage.cmd_token(runtime, update, SimpleNamespace(args=[]))

    text = replies[0][0]
    assert "成本未知" in text
    assert "$0.0000" not in text


@pytest.mark.asyncio
async def test_usage_all_aggregates_unknown_cost_without_zero_total(tmp_path):
    record_usage(
        tmp_path,
        model="unlisted-maintenance-synthetic-model",
        backend="hashi-api",
        engine="hashi-api",
        input_tokens=100,
        output_tokens=10,
        session_id="session",
        cost_usd=None,
        token_source="provider",
    )
    runtime, replies = _runtime(tmp_path)
    runtime.orchestrator = SimpleNamespace(
        runtimes=[
            SimpleNamespace(
                name="lily",
                workspace_dir=tmp_path,
                session_id_dt="session",
            )
        ]
    )
    update = SimpleNamespace(effective_user=SimpleNamespace(id=1))

    with ui_language.language_scope(SimpleNamespace(), locale="en"):
        await runtime_usage.cmd_usage(runtime, update, SimpleNamespace(args=["all"]))

    text = replies[0][0]
    assert text.count("Cost unknown") == 3
    assert "$0.0000" not in text
