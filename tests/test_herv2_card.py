"""Regression tests for the HERV3 report on its compatibility path."""

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


def test_retired_herv2_command_is_not_publicly_registered():
    assert "herv2" not in COMMAND_SPEC_BY_NAME


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
    assert "🧭 HERV3 Runtime Report" in plain
    assert "Model reasoning: zero" in plain
    assert "Route" not in plain
    assert "Triage" not in plain

    zh_plain = format_herv2_card(card_data, locale="zh-CN", surface="plain")
    assert "🧭 HERV3 运行报告" in zh_plain
    assert "模型推理：zero" in zh_plain
    assert "路由" not in zh_plain
    assert "分诊" not in zh_plain

    telegram_html = format_herv2_card(
        card_data,
        locale="zh-CN",
        surface="telegram",
    )
    assert "🧭 <b>HERV3 运行报告</b>" in telegram_html
    assert "<b>模型推理：</b><code>zero</code>" in telegram_html
    assert "DIRECT" not in telegram_html


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
    assert "🧭 <b>HERV3 运行报告</b>" in zh_html
    assert "<b>模型推理：</b><code>medium</code>" in zh_html
    assert "<b>策略卡（可选参考）：</b>代码修改" in zh_html
    # Strategy card id must be dropped in favour of the localized title
    assert "<code>CODE_MODIFY</code>" not in zh_html
    assert "<b>模型调用：</b>" in zh_html
    assert "• <code>gemini-2.5-flash</code> · 0.6秒 · 350 token" in zh_html
    assert "• <code>claude-sonnet-4-6</code> · 3.5秒 · 1,500 token" in zh_html
    assert "<b>最终状态：</b><code>COMPLETED</code>" in zh_html
    for retired_copy in ("路由", "分诊", "执行概要", "收尾", "评审", "重规划"):
        assert retired_copy not in zh_html

    # English Telegram HTML
    en_html = format_herv2_card(card_data, locale="en", surface="telegram")
    assert "🧭 <b>HERV3 Runtime Report</b>" in en_html
    assert "<b>Model reasoning: </b><code>medium</code>" in en_html
    assert "<b>Strategy Cards (optional): </b>代码修改" in en_html
    assert "<b>Model calls: </b>" in en_html
    assert "• <code>gemini-2.5-flash</code> · 0.6s · 350 tokens" in en_html
    assert "• <code>claude-sonnet-4-6</code> · 3.5s · 1.5K tokens" in en_html
    assert "<b>State: </b><code>COMPLETED</code>" in en_html
    for retired_copy in ("Route", "Triage", "Execution", "Finalisation", "Reviews", "Replan"):
        assert retired_copy not in en_html


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
    assert "🧭 HERV3 运行报告" in plain
    assert "──────────" in plain
    assert "<b>" not in plain
    assert "<code>" not in plain
    assert "模型推理：medium" in plain
    assert "策略卡（可选参考）：代码修改" in plain
    assert "CODE_MODIFY" not in plain
    assert "• gemini-2.5-flash · 0.6秒 · 350 token" in plain
    assert "最终状态：COMPLETED" in plain
    for retired_copy in ("路由", "分诊", "执行概要", "收尾", "评审", "重规划"):
        assert retired_copy not in plain


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
    # Exactly one model line, aggregated with calls + total duration + total tokens
    assert zh.count("• deepseek-flash") == 1
    assert "• deepseek-flash · 3 次 · 2分36秒 · 65.3万 token" in zh

    en = format_herv2_card(card_data, locale="en", surface="plain")
    assert en.count("• deepseek-flash") == 1
    assert "• deepseek-flash · 3 calls · 2m 36s · 653.4K tokens" in en


def test_herv2_card_hides_legacy_finalisation_stage():
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
    assert "• claude-sonnet-4-6 · 2 次 · 12秒 · 1.2万 token" in zh
    assert "收尾" not in zh

    en = format_herv2_card(card_data, locale="en", surface="plain")
    assert "• claude-sonnet-4-6 · 2 calls · 12s · 12K tokens" in en
    assert "Finalisation" not in en


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
    assert "HERV3 运行报告" in zh_html_text
    assert "策略卡（可选参考）：监控与条件触发 · 证据与条款抽取" in zh_html_text
    assert "deepseek-flash · 2 次 · 26.8秒 · 19.3万 token" in zh_html_text
    assert "路由" not in zh_html_text
    assert "分诊" not in zh_html_text

    assert "HERV3 运行报告" in zh_plain
    assert "策略卡（可选参考）：监控与条件触发 · 证据与条款抽取" in zh_plain
    assert "• deepseek-flash · 2 次 · 26.8秒 · 19.3万 token" in zh_plain
    assert "路由" not in zh_plain
    assert "分诊" not in zh_plain


@pytest.mark.asyncio
async def test_runtime_send_herv2_card_delivery():
    from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime

    runtime = MagicMock(spec=FlexibleAgentRuntime)
    runtime.logger = logging.getLogger("test.herv2")
    delivery_order = []
    async def send_long_message(**kwargs):
        delivery_order.append("telegram")
        return 0.0, 1

    runtime.send_long_message = AsyncMock(side_effect=send_long_message)
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

    # 2. The canonical Session event is committed before any Telegram projection.
    with patch("orchestrator.runtime_pipeline.request_meta_for", return_value={"herv2_at_start": True, "ui_locale_at_start": "zh-CN"}):
        with patch.object(FlexibleAgentRuntime, "_should_buffer_during_transfer", return_value=False):
            with patch("orchestrator.runtime_session.record_frontend_message") as mock_record:
                def record_event(*_args, **kwargs):
                    delivery_order.append("session-event")
                    return {
                        "delivery_event_id": "evt-herv2-1",
                        "session_id": kwargs["explicit_session_id"],
                    }

                mock_record.side_effect = record_event
                await FlexibleAgentRuntime._send_herv2_card(runtime, item, response=response)
                runtime.send_long_message.assert_called_once()
                call_args = runtime.send_long_message.call_args[1]
                assert call_args["chat_id"] == 999
                assert call_args["purpose"] == "herv2-card"
                assert call_args["parse_mode"] == "HTML"
                assert "<b>HERV3 运行报告</b>" in call_args["text"]
                assert call_args["frontend_event_id"] == "evt-herv2-1"

                mock_record.assert_called_once()
                rec_kwargs = mock_record.call_args[1]
                assert rec_kwargs["presentation_channel"] == "herv2"
                assert rec_kwargs["role"] == "assistant"
                assert "HERV3 运行报告" in rec_kwargs["text"]
                assert delivery_order == ["session-event", "telegram"]

    # 3. Disabling Telegram does not suppress the canonical card for other clients.
    runtime.send_long_message.reset_mock()
    item.deliver_to_telegram = False
    with patch("orchestrator.runtime_pipeline.request_meta_for", return_value={"herv2_at_start": True, "ui_locale_at_start": "zh-CN"}):
        with patch.object(FlexibleAgentRuntime, "_should_buffer_during_transfer", return_value=False):
            with patch("orchestrator.runtime_session.record_frontend_message", return_value={"delivery_event_id": "evt-herv2-2", "session_id": "sess-1"}) as mock_record:
                await FlexibleAgentRuntime._send_herv2_card(runtime, item, response=response)
                mock_record.assert_called_once()
                runtime.send_long_message.assert_not_called()
