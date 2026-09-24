"""Unit and runtime regression tests for the HER v2 Routing Card feature."""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from orchestrator.command_specs import COMMAND_SPEC_BY_NAME
from orchestrator import telegram_stream_policy
from tools.herv2_card import (
    Herv2CardData,
    Herv2StageItem,
    _fmt_tokens,
    _resolve_slot,
    format_herv2_card,
    herv2_card_data_from_metadata,
)


def test_command_specs_registration():
    assert "herv2" in COMMAND_SPEC_BY_NAME
    herv2_spec = COMMAND_SPEC_BY_NAME["herv2"]
    assert herv2_spec.method_name == "cmd_herv2"
    assert herv2_spec.group == "session"
    assert herv2_spec.guide is not None
    assert "on" in herv2_spec.guide.choices
    assert "off" in herv2_spec.guide.choices
    assert "status" in herv2_spec.guide.choices


def test_display_preference_registration(tmp_path):
    class FakeRuntime:
        def __init__(self, workspace):
            self.workspace_dir = workspace
            self.name = "test-agent"

    ws = tmp_path / "agent"
    ws.mkdir()
    runtime = FakeRuntime(ws)

    assert "herv2" in telegram_stream_policy.DISPLAY_PREFERENCE_NAMES
    # Default is False
    assert telegram_stream_policy.get_display_preference(runtime, "herv2", default=False) is False

    # Enable
    telegram_stream_policy.set_display_preference(runtime, "herv2", True)
    assert telegram_stream_policy.get_display_preference(runtime, "herv2", default=False) is True

    # Disable
    telegram_stream_policy.set_display_preference(runtime, "herv2", False)
    assert telegram_stream_policy.get_display_preference(runtime, "herv2", default=False) is False


def test_resolve_slot():
    # Direct route is unconditionally Quick
    assert _resolve_slot("direct", "any-model") == "Quick"

    # Default mappings
    assert _resolve_slot("triage", "") == "Quick"
    assert _resolve_slot("planning", "") == "Pro"
    assert _resolve_slot("execution", "") == "Pro"
    assert _resolve_slot("review", "") == "Pro"

    # Model name heuristics
    assert _resolve_slot("unknown_stage", "gpt-5.4-mini") == "Quick"
    assert _resolve_slot("unknown_stage", "claude-sonnet-4-6") == "Pro"

    # Slot models override
    slot_models = {"fast": "gemini-2.5-flash", "pro": "gemini-2.5-pro"}
    assert _resolve_slot("triage", "gemini-2.5-flash", slot_models=slot_models) == "Quick"
    assert _resolve_slot("planning", "gemini-2.5-pro", slot_models=slot_models) == "Pro"

    # Route model slots override
    route_model_slots = {"execute": "fast"}
    assert _resolve_slot("execution", "custom-model", route_model_slots=route_model_slots) == "Quick"


def test_fmt_tokens_compact_units():
    # Chinese uses 万 (10k), singular "token"
    assert _fmt_tokens(653358, is_zh=True) == "65.3万 token"
    assert _fmt_tokens(85416, is_zh=True) == "8.5万 token"
    assert _fmt_tokens(107726, is_zh=True) == "10.8万 token"
    assert _fmt_tokens(10000, is_zh=True) == "1万 token"
    assert _fmt_tokens(1500, is_zh=True) == "1,500 token"
    assert _fmt_tokens(350, is_zh=True) == "350 token"

    # English uses K (1k), plural "tokens"
    assert _fmt_tokens(653358, is_zh=False) == "653.4K tokens"
    assert _fmt_tokens(85416, is_zh=False) == "85.4K tokens"
    assert _fmt_tokens(107726, is_zh=False) == "107.7K tokens"
    assert _fmt_tokens(1000, is_zh=False) == "1K tokens"
    assert _fmt_tokens(1500, is_zh=False) == "1.5K tokens"
    assert _fmt_tokens(350, is_zh=False) == "350 tokens"

    # Non-positive → empty
    assert _fmt_tokens(0, is_zh=True) == ""
    assert _fmt_tokens(-1, is_zh=False) == ""


def test_herv2_card_data_extraction_complex():
    her_v2_meta = {
        "turn_id": "turn-123",
        "classification": "COMPLEX_TASK",
        "terminal_state": "COMPLETED",
        "effort": {"requested_effort": "medium"},
        "strategy_cards": ["CODE_MODIFY", "CURRENT_FACT_RESEARCH"],
        "strategy_card_details": [
            {"id": "CODE_MODIFY", "title": "代码修改"},
            {"id": "CURRENT_FACT_RESEARCH", "title": "当前事实检索"},
        ],
        "strategy_brief": {"summary": "Implement unit tests and verify isolation."},
        "stage_timings_s": {"triage": 0.5, "planning": 1.5, "execution": 3.2, "review": 0.8},
        "review_count": 1,
        "replan_count": 0,
        "checkpoint_count": 0,
    }
    meter_meta = {
        "line_items": [
            {
                "phase": "triage",
                "engine": "gemini-cli",
                "model": "gemini-2.5-flash",
                "input": 500,
                "output": 150,
                "cost_usd": 0.0001,
            },
            {
                "phase": "planning",
                "engine": "claude-cli",
                "model": "claude-sonnet-4-6",
                "input": 1200,
                "output": 400,
                "cost_usd": 0.005,
            },
            {
                "phase": "execution",
                "engine": "claude-cli",
                "model": "claude-sonnet-4-6",
                "input": 2500,
                "output": 800,
                "cost_usd": 0.012,
            },
            {
                "phase": "review",
                "engine": "claude-cli",
                "model": "claude-sonnet-4-6",
                "input": 1000,
                "output": 200,
                "cost_usd": 0.004,
            },
        ]
    }

    card_data = herv2_card_data_from_metadata(her_v2_meta, meter=meter_meta)
    assert card_data is not None
    assert card_data.turn_id == "turn-123"
    assert card_data.classification == "COMPLEX_TASK"
    assert card_data.terminal_state == "COMPLETED"
    assert card_data.effort == "medium"
    assert len(card_data.strategy_cards) == 2
    assert len(card_data.strategy_card_details) == 2
    assert card_data.execution_brief == "Implement unit tests and verify isolation."
    assert card_data.review_count == 1
    assert card_data.replan_count == 0

    assert len(card_data.stages) == 4
    stages_by_name = {s.stage: s for s in card_data.stages}
    assert stages_by_name["triage"].slot == "Quick"
    assert stages_by_name["triage"].model == "gemini-2.5-flash"
    assert stages_by_name["triage"].tokens == 650
    assert stages_by_name["triage"].elapsed_s == 0.5

    assert stages_by_name["planning"].slot == "Pro"
    assert stages_by_name["planning"].model == "claude-sonnet-4-6"

    assert stages_by_name["execution"].slot == "Pro"
    assert stages_by_name["review"].slot == "Pro"


def test_herv2_card_data_extraction_direct():
    her_v2_meta = {
        "turn_id": "turn-456",
        "classification": "SIMPLE_TASK",
        "terminal_state": "COMPLETED",
        "strategy_cards": ["SIMPLE_QA"],
        "strategy_card_details": [{"id": "SIMPLE_QA", "title": "简单知识问答"}],
        "stage_timings_s": {"triage": 0.3, "direct": 0.9},
    }
    meter_meta = {
        "line_items": [
            {
                "phase": "triage",
                "engine": "gemini-cli",
                "model": "gemini-2.5-flash",
                "input": 200,
                "output": 50,
            },
            {
                "phase": "direct",
                "engine": "gemini-cli",
                "model": "gemini-2.5-flash",
                "input": 300,
                "output": 120,
            },
        ]
    }

    card_data = herv2_card_data_from_metadata(her_v2_meta, meter=meter_meta)
    assert card_data is not None
    assert card_data.classification == "SIMPLE_TASK"
    assert len(card_data.stages) == 2
    for st in card_data.stages:
        assert st.slot == "Quick"


def test_herv2_card_direct_mode_without_triage_is_not_unknown():
    card_data = herv2_card_data_from_metadata(
        {
            "turn_id": "turn-direct-zero",
            "classification": None,
            "terminal_state": "COMPLETED",
            "effort": {"effective": "zero"},
        }
    )

    assert card_data is not None
    assert card_data.classification == ""
    assert card_data.execution_route == "DIRECT"

    plain = format_herv2_card(card_data, locale="en", surface="plain")
    assert "Route: DIRECT · Direct (no triage) · zero" in plain
    assert "Route: UNKNOWN" not in plain

    zh_plain = format_herv2_card(card_data, locale="zh-CN", surface="plain")
    assert "\u8def\u7531\uff1aDIRECT \u00b7 \u76f4\u8fbe\uff08\u672a\u5206\u8bca\uff09 \u00b7 zero" in zh_plain

    telegram_html = format_herv2_card(
        card_data,
        locale="zh-CN",
        surface="telegram",
    )
    assert "<code>DIRECT</code> \u00b7 \u76f4\u8fbe\uff08\u672a\u5206\u8bca\uff09 \u00b7 zero" in telegram_html


def test_herv2_card_formatting_telegram_html():
    card_data = Herv2CardData(
        turn_id="turn-789",
        classification="COMPLEX_TASK",
        terminal_state="COMPLETED",
        strategy_cards=("CODE_MODIFY",),
        strategy_card_details=({"id": "CODE_MODIFY", "title": "代码修改"},),
        execution_brief="Refactor routing",
        effort="medium",
        stages=(
            Herv2StageItem(
                stage="triage",
                slot="Quick",
                model="gemini-2.5-flash",
                elapsed_s=0.6,
                tokens=350,
            ),
            Herv2StageItem(
                stage="execution",
                slot="Pro",
                model="claude-sonnet-4-6",
                elapsed_s=3.5,
                tokens=1500,
            ),
        ),
        review_count=1,
        replan_count=0,
    )

    # Chinese Telegram HTML
    zh_html = format_herv2_card(card_data, locale="zh-CN", surface="telegram")
    assert "🧭 <b>HER v2 路由与策略卡</b>" in zh_html
    assert "<b>路由：</b><code>COMPLEX_TASK</code> · medium" in zh_html
    assert "<b>策略：</b>代码修改" in zh_html
    # Strategy card id must be dropped in favour of the localized title
    assert "<code>CODE_MODIFY</code>" not in zh_html
    # The former execution-brief section is no longer rendered
    assert "执行概要" not in zh_html
    assert "<b>阶段与模型：</b>" in zh_html
    assert "• <b>分诊</b> · <b>快速档</b> · <code>gemini-2.5-flash</code> · 0.6秒 · 350 token" in zh_html
    assert "• <b>执行</b> · <b>专业档</b> · <code>claude-sonnet-4-6</code> · 3.5秒 · 1,500 token" in zh_html
    assert "<b>收尾：</b>未做合并总结（本档由执行直接给出最终答复）" in zh_html
    assert "<b>运行：</b><b>评审</b>: <code>1</code>" in zh_html
    assert "<b>重规划</b>: <code>0</code>" in zh_html
    assert "<b>最终状态</b>: <code>COMPLETED</code>" in zh_html

    # English Telegram HTML
    en_html = format_herv2_card(card_data, locale="en", surface="telegram")
    assert "🧭 <b>HER v2 Routing Card</b>" in en_html
    assert "<b>Route: </b><code>COMPLEX_TASK</code> · medium" in en_html
    assert "<b>Strategy: </b>代码修改" in en_html
    assert "<b>Triage</b> · <b>Quick</b> · <code>gemini-2.5-flash</code>" in en_html
    assert "0.6s" in en_html
    assert "350 tokens" in en_html
    assert "<b>Execution</b> · <b>Pro</b> · <code>claude-sonnet-4-6</code> · 3.5s · 1.5K tokens" in en_html
    assert "<b>Finalisation: </b>No merge summary (this tier delivered the final answer directly via execution)" in en_html
    assert "<b>Run: </b><b>Reviews</b>: <code>1</code>" in en_html
    assert "<b>State</b>: <code>COMPLETED</code>" in en_html


def test_herv2_card_formatting_plain():
    card_data = Herv2CardData(
        turn_id="turn-789",
        classification="COMPLEX_TASK",
        terminal_state="COMPLETED",
        strategy_cards=("CODE_MODIFY",),
        strategy_card_details=({"id": "CODE_MODIFY", "title": "代码修改"},),
        execution_brief="Refactor routing",
        effort="medium",
        stages=(
            Herv2StageItem(
                stage="triage",
                slot="Quick",
                model="gemini-2.5-flash",
                elapsed_s=0.6,
                tokens=350,
            ),
        ),
        review_count=0,
    )

    plain = format_herv2_card(card_data, locale="zh-CN", surface="plain")
    assert "🧭 HER v2 路由与策略卡" in plain
    assert "──────────" in plain
    assert "<b>" not in plain
    assert "<code>" not in plain
    assert "路由：COMPLEX_TASK · medium" in plain
    assert "策略：代码修改" in plain
    assert "CODE_MODIFY" not in plain
    assert "执行概要" not in plain
    assert "• 分诊 · 快速档 · gemini-2.5-flash · 0.6秒 · 350 token" in plain
    assert "收尾：未做合并总结（本档由执行直接给出最终答复）" in plain
    assert "运行：评审: 0 · 重规划: 0 · 最终状态: COMPLETED" in plain


def test_herv2_card_aggregates_repeated_stage():
    card_data = Herv2CardData(
        turn_id="turn-agg",
        classification="SIMPLE_TASK",
        terminal_state="COMPLETED",
        strategy_cards=("SIMPLE_QA",),
        strategy_card_details=(),
        stages=(
            Herv2StageItem("execution", "Quick", "deepseek-flash", elapsed_s=60.0, tokens=200000),
            Herv2StageItem("execution", "Quick", "deepseek-flash", elapsed_s=60.0, tokens=200000),
            Herv2StageItem("execution", "Quick", "deepseek-flash", elapsed_s=36.0, tokens=253358),
        ),
        review_count=0,
        replan_count=0,
    )

    zh = format_herv2_card(card_data, locale="zh-CN", surface="plain")
    # Exactly one execution line, aggregated with rounds + total duration + total tokens
    assert zh.count("• 执行") == 1
    assert "• 执行 · 快速档 · deepseek-flash · 3轮 · 合计2分36秒 · 65.3万 token" in zh

    en = format_herv2_card(card_data, locale="en", surface="plain")
    assert en.count("• Execution") == 1
    assert "• Execution · Quick · deepseek-flash · 3 rounds · Total 2m 36s · 653.4K tokens" in en


def test_herv2_card_finalisation_present():
    card_data = Herv2CardData(
        turn_id="turn-fin",
        classification="COMPLEX_TASK",
        terminal_state="COMPLETED",
        stages=(
            Herv2StageItem("execution", "Pro", "claude-sonnet-4-6", elapsed_s=10.0, tokens=9000),
            Herv2StageItem("finalisation", "Pro", "claude-sonnet-4-6", elapsed_s=2.0, tokens=3000),
        ),
        review_count=1,
        replan_count=0,
    )

    zh = format_herv2_card(card_data, locale="zh-CN", surface="plain")
    assert "收尾：已做合并总结" in zh

    en = format_herv2_card(card_data, locale="en", surface="plain")
    assert "Finalisation: Merged summary produced" in en


def test_herv2_card_plain_html_copy_consistency():
    card_data = Herv2CardData(
        turn_id="turn-consistency",
        classification="SIMPLE_TASK",
        terminal_state="COMPLETED",
        strategy_card_details=(
            {"id": "A", "title": "监控与条件触发"},
            {"id": "B", "title": "证据与条款抽取"},
        ),
        stages=(
            Herv2StageItem("immediate_response", "Quick", "deepseek-flash", elapsed_s=10.1, tokens=85416),
            Herv2StageItem("triage", "Quick", "deepseek-flash", elapsed_s=16.7, tokens=107726),
        ),
        review_count=0,
        replan_count=0,
    )

    zh_html = format_herv2_card(card_data, locale="zh-CN", surface="telegram")
    zh_plain = format_herv2_card(card_data, locale="zh-CN", surface="plain")

    # Strip HTML tags; the remaining copy should match the plain branch.
    import re

    zh_html_text = re.sub(r"<[^>]+>", "", zh_html)
    assert "路由：SIMPLE_TASK" in zh_html_text
    assert "策略：监控与条件触发 · 证据与条款抽取" in zh_html_text
    assert "即时响应 · 快速档 · deepseek-flash · 10.1秒 · 8.5万 token" in zh_html_text
    assert "分诊 · 快速档 · deepseek-flash · 16.7秒 · 10.8万 token" in zh_html_text

    assert "路由：SIMPLE_TASK" in zh_plain
    assert "策略：监控与条件触发 · 证据与条款抽取" in zh_plain
    assert "• 即时响应 · 快速档 · deepseek-flash · 10.1秒 · 8.5万 token" in zh_plain
    assert "• 分诊 · 快速档 · deepseek-flash · 16.7秒 · 10.8万 token" in zh_plain


@pytest.mark.asyncio
async def test_runtime_send_herv2_card_delivery():
    from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime

    runtime = MagicMock(spec=FlexibleAgentRuntime)
    runtime.logger = logging.getLogger("test.herv2")
    runtime.send_long_message = AsyncMock()
    runtime._should_buffer_during_transfer = MagicMock(return_value=False)

    # Simulate _send_herv2_card unbound method call
    item = SimpleNamespace(
        request_id="req-123",
        chat_id=999,
        silent=False,
        deliver_to_telegram=True,
        owner_id="user-1",
        session_surface="telegram",
        session_channel_key="chat-999",
        session_id="sess-1",
    )
    response = SimpleNamespace(
        stream_metadata={
            "her_v2": {
                "turn_id": "turn-123",
                "classification": "COMPLEX_TASK",
                "terminal_state": "COMPLETED",
                "strategy_cards": ["CODE_MODIFY"],
                "strategy_card_details": [{"id": "CODE_MODIFY", "title": "代码修改"}],
                "stage_timings_s": {"triage": 0.5, "execution": 2.0},
            },
            "meter": {
                "line_items": [
                    {"phase": "triage", "model": "gemini-2.5-flash", "input": 100, "output": 50},
                    {"phase": "execution", "model": "claude-sonnet-4-6", "input": 500, "output": 200},
                ]
            },
        }
    )

    # 1. When herv2_at_start is False, no message is sent
    with patch("orchestrator.runtime_pipeline.request_meta_for", return_value={"herv2_at_start": False}):
        with patch.object(FlexibleAgentRuntime, "_should_buffer_during_transfer", return_value=False):
            await FlexibleAgentRuntime._send_herv2_card(runtime, item, response=response)
            runtime.send_long_message.assert_not_called()

    # 2. When herv2_at_start is True, card is delivered to Telegram and recorded to session
    with patch("orchestrator.runtime_pipeline.request_meta_for", return_value={"herv2_at_start": True, "ui_locale_at_start": "zh-CN"}):
        with patch.object(FlexibleAgentRuntime, "_should_buffer_during_transfer", return_value=False):
            with patch("orchestrator.runtime_session.record_frontend_message") as mock_record:
                await FlexibleAgentRuntime._send_herv2_card(runtime, item, response=response)
                runtime.send_long_message.assert_called_once()
                call_args = runtime.send_long_message.call_args[1]
                assert call_args["chat_id"] == 999
                assert call_args["purpose"] == "herv2-card"
                assert call_args["parse_mode"] == "HTML"
                assert "<b>HER v2 路由与策略卡</b>" in call_args["text"]

                mock_record.assert_called_once()
                rec_kwargs = mock_record.call_args[1]
                assert rec_kwargs["presentation_channel"] == "herv2"
                assert rec_kwargs["role"] == "assistant"
                assert "HER v2 路由与策略卡" in rec_kwargs["text"]
