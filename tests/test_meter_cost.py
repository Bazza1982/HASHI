"""Regression tests for the /meter (per-turn cost tail) feature.

Covers the Zelda data contract: per-call cost line items, cost/token source
classification (provider / pricing_table / local_zero / unknown), the 0.0 vs
None distinction, the deterministic formatter, command registration, and the
display-preference whitelist.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from pathlib import Path

import pytest

from orchestrator.command_specs import COMMAND_SPEC_BY_NAME
from orchestrator.telegram_stream_policy import (
    DISPLAY_PREFERENCE_NAMES,
    get_display_preference,
    set_display_preference,
)
from tools.meter_cost import (
    PerCallUsageLineItem,
    UsageReceipt,
    format_cost_tail,
    format_meditation_cost_tail,
    line_item_from_dict,
)
from adapters.base import BackendResponse, TokenUsage
from adapters.her_v2_provider import HashiStageProvider
from tools.token_tracker import record_usage, resolve_cost_source, model_has_pricing


# ── Data contract: record_usage returns a structured receipt ─────────────────

def test_record_usage_returns_receipt(tmp_path: Path):
    receipt = record_usage(
        tmp_path,
        model="claude-sonnet-4-6",
        backend="claude-cli",
        input_tokens=1000,
        output_tokens=500,
        thinking_tokens=0,
        cost_usd=None,
    )
    assert isinstance(receipt, UsageReceipt)
    assert receipt.total_tokens == 1500
    assert receipt.cost_usd is not None  # pricing table resolves a known model
    assert receipt.dominant_cost_source() == "pricing_table"
    # JSONL still written (backwards compatible)
    assert (tmp_path / "token_usage.jsonl").exists()


def test_unknown_model_cost_is_none_not_zero(tmp_path: Path):
    receipt = record_usage(
        tmp_path,
        model="totally-unknown-model",
        backend="claude-cli",
        input_tokens=100,
        output_tokens=50,
        cost_usd=None,
    )
    assert receipt.cost_usd is None
    assert receipt.dominant_cost_source() == "unknown"


def test_local_engine_is_zero_cost(tmp_path: Path):
    receipt = record_usage(
        tmp_path,
        model="llama3.1:8b",
        backend="ollama",
        input_tokens=100,
        output_tokens=50,
        cost_usd=None,
    )
    assert receipt.cost_usd == 0.0
    assert receipt.has_local_only
    assert receipt.dominant_cost_source() == "local_zero"


def test_provider_cost_is_preserved(tmp_path: Path):
    receipt = record_usage(
        tmp_path,
        model="gpt-5.4",
        backend="openrouter",
        input_tokens=1000,
        output_tokens=500,
        cost_usd=0.012345,
    )
    assert receipt.cost_usd == 0.012345
    assert receipt.dominant_cost_source() == "provider"


def test_jsonl_uses_structured_receipt_cost_for_multi_model_turn(tmp_path: Path):
    line_items = [
        PerCallUsageLineItem(
            phase="execution",
            engine="openrouter",
            model="qwen/qwen3.7-flash",
            input_tokens=100,
            output_tokens=50,
            token_source="provider",
            cost_usd=0.456750,
            cost_source="provider",
        )
    ]

    receipt = record_usage(
        tmp_path,
        model="role-configured",
        backend="her-v2",
        input_tokens=100,
        output_tokens=50,
        request_id="request-multi",
        line_items=line_items,
    )

    persisted = json.loads(
        (tmp_path / "token_usage.jsonl").read_text(encoding="utf-8").splitlines()[-1]
    )
    assert receipt.cost_usd == pytest.approx(0.456750)
    assert persisted["cost_usd"] == pytest.approx(0.456750)


def test_provider_reasoning_is_output_detail_not_extra_tokens(tmp_path: Path):
    receipt = record_usage(
        tmp_path,
        model="qwen/qwen3.7-flash",
        backend="openrouter",
        input_tokens=100,
        output_tokens=60,
        thinking_tokens=40,
        token_source="provider",
    )

    assert receipt.total_tokens == 160
    assert receipt.line_items[0].thinking_in_output is True
    assert receipt.line_items[0].cost_usd == pytest.approx(0.000011)


def test_estimated_reasoning_defaults_to_separate_tokens():
    item = PerCallUsageLineItem(
        input_tokens=100,
        output_tokens=60,
        thinking_tokens=40,
        token_source="estimated",
    )

    assert item.thinking_in_output is False
    assert item.total_tokens == 200


def test_resolve_cost_source_zero_vs_none():
    assert resolve_cost_source(cost_usd=0.0, model="x", engine="claude") == (0.0, "provider")
    assert resolve_cost_source(cost_usd=None, model="unknown-x", engine="claude") == (None, "unknown")


def test_model_has_pricing():
    assert model_has_pricing("claude-sonnet-4-6") is True
    assert model_has_pricing("bogus-model-999") is False
    assert model_has_pricing("gpt-5") is False
    assert model_has_pricing("anthropic/claude-sonnet-4-6") is True


def test_her_stage_provider_preserves_per_http_call_price_tiers():
    provider = object.__new__(HashiStageProvider)
    provider.usage_line_items = []
    response = BackendResponse(
        text="done",
        duration_ms=1,
        usage=TokenUsage(input_tokens=72_000, output_tokens=2_000),
        stream_metadata={
            "meter": {
                "provider_calls": [
                    {
                        "input": 31_000,
                        "output": 1_000,
                        "thinking": 0,
                        "token_source": "provider",
                        "thinking_in_output": True,
                        "cost_usd": None,
                    },
                    {
                        "input": 41_000,
                        "output": 1_000,
                        "thinking": 0,
                        "token_source": "provider",
                        "thinking_in_output": True,
                        "cost_usd": None,
                    },
                ]
            }
        },
    )

    provider._record_usage_line_item(
        request_id="request-tiered",
        phase="execution",
        engine="openrouter",
        model="qwen/qwen3.7-flash",
        response=response,
    )

    assert len(provider.usage_line_items) == 2
    assert provider.usage_line_items[0].cost_usd == pytest.approx(0.00106)
    assert provider.usage_line_items[1].cost_usd == pytest.approx(0.0045)


def test_her_stage_provider_preserves_deepseek_cache_and_call_latency():
    provider = object.__new__(HashiStageProvider)
    provider.usage_line_items = []
    response = BackendResponse(
        text="done",
        duration_ms=1,
        usage=TokenUsage(input_tokens=1_000, output_tokens=100),
        stream_metadata={
            "meter": {
                "provider_calls": [
                    {
                        "input": 1_000,
                        "output": 100,
                        "thinking": 0,
                        "token_source": "provider",
                        "thinking_in_output": True,
                        "cost_usd": None,
                        "prompt_cache_hit_tokens": 800,
                        "prompt_cache_miss_tokens": 200,
                        "provider_call_latency_ms": 321.9874,
                    }
                ]
            }
        },
    )

    provider._record_usage_line_item(
        request_id="request-deepseek-cache",
        phase="execution",
        engine="deepseek-api",
        model="deepseek-v4-flash",
        response=response,
    )

    assert len(provider.usage_line_items) == 1
    item = provider.usage_line_items[0]
    assert item.prompt_cache_hit_tokens == 800
    assert item.prompt_cache_miss_tokens == 200
    assert item.provider_call_latency_ms == 321.987
    assert item.cost_usd == pytest.approx(0.000058)


def test_her_stage_provider_does_not_invent_call_from_explicit_empty_meter():
    provider = object.__new__(HashiStageProvider)
    provider.usage_line_items = []
    response = BackendResponse(
        text="",
        duration_ms=1,
        is_success=False,
        stream_metadata={"meter": {"provider_calls": []}},
    )

    provider._record_usage_line_item(
        request_id="request-before-http",
        phase="execution",
        engine="deepseek-api",
        model="deepseek-v4-flash",
        response=response,
    )

    assert provider.usage_line_items == []


# ── Formatter ────────────────────────────────────────────────────────────────

def test_formatter_pricing_table_has_approx():
    receipt = UsageReceipt(line_items=[
        PerCallUsageLineItem(model="claude-sonnet-4-6", input_tokens=1000,
                             output_tokens=500, cost_usd=0.0105,
                             cost_source="pricing_table"),
    ])
    tail = format_cost_tail(receipt)
    assert tail.startswith("💰 前台回合：≈ US$")
    assert "价目表估算" in tail
    assert "1.5K tokens" in tail


def test_formatter_provider_no_approx():
    receipt = UsageReceipt(line_items=[
        PerCallUsageLineItem(model="gpt-5.4", input_tokens=1000,
                             output_tokens=500, cost_usd=0.012345,
                             cost_source="provider"),
    ])
    tail = format_cost_tail(receipt)
    assert "≈" not in tail
    assert "Provider 实报" in tail


def test_formatter_small_cost():
    receipt = UsageReceipt(line_items=[
        PerCallUsageLineItem(cost_usd=0.00005, cost_source="provider"),
    ])
    assert "< US$0.0001" in format_cost_tail(receipt)


def test_formatter_unknown_cost():
    receipt = UsageReceipt(line_items=[
        PerCallUsageLineItem(cost_usd=None, cost_source="unknown"),
    ])
    assert "成本未知" in format_cost_tail(receipt)


def test_formatter_task_total():
    receipt = UsageReceipt(line_items=[
        PerCallUsageLineItem(cost_usd=0.012347, cost_source="provider"),
    ])
    tail = format_cost_tail(receipt, task_total_usd=0.013587)
    assert "任务累计 ≈" in tail


# ── Command registration ─────────────────────────────────────────────────────

def test_meter_command_registered():
    spec = COMMAND_SPEC_BY_NAME["meter"]
    assert spec.method_name == "cmd_meter"
    assert spec.menu_visible is True
    assert spec.alias_of is None
    alias = COMMAND_SPEC_BY_NAME["metre"]
    assert alias.method_name == "cmd_meter"
    assert alias.menu_visible is False
    assert alias.alias_of == "meter"


# ── Display-preference whitelist ─────────────────────────────────────────────

def test_meter_in_whitelist():
    assert "meter" in DISPLAY_PREFERENCE_NAMES


def test_meter_display_preference_persists(tmp_path: Path):
    runtime = SimpleNamespace(workspace_dir=tmp_path)
    assert get_display_preference(runtime, "meter", default=False) is False
    set_display_preference(runtime, "meter", True)
    assert get_display_preference(runtime, "meter", default=False) is True


# ── Line item round-trip ─────────────────────────────────────────────────────

def test_line_item_from_dict_roundtrip():
    item = PerCallUsageLineItem(
        request_id="r1", phase="execution", engine="deepseek", model="deepseek-v4-pro",
        input_tokens=10, output_tokens=20, thinking_tokens=3,
        token_source="provider", cost_usd=0.0001, cost_source="provider",
        prompt_cache_hit_tokens=8, prompt_cache_miss_tokens=2,
        provider_call_latency_ms=12.346,
    )
    restored = line_item_from_dict(item.to_dict())
    assert restored == item


# ── Meditation cost tail formatter ──────────────────────────────────────────

def test_meditation_formatter_label_and_approx():
    receipt = UsageReceipt(line_items=[
        PerCallUsageLineItem(model="deepseek-v4-pro", input_tokens=100,
                             output_tokens=50, cost_usd=0.001240,
                             cost_source="pricing_table"),
    ])
    tail = format_meditation_cost_tail(receipt)
    assert tail.startswith("🧘 冥想：≈ US$")
    assert "价目表估算" in tail


def test_meditation_formatter_task_total():
    receipt = UsageReceipt(line_items=[
        PerCallUsageLineItem(cost_usd=0.001240, cost_source="provider"),
    ])
    tail = format_meditation_cost_tail(receipt, task_total_usd=0.013587)
    assert "任务累计 ≈" in tail
    assert "🧘 冥想" in tail


def test_meditation_formatter_unknown_cost():
    receipt = UsageReceipt(line_items=[
        PerCallUsageLineItem(cost_usd=None, cost_source="unknown"),
    ])
    assert "成本未知" in format_meditation_cost_tail(receipt)
