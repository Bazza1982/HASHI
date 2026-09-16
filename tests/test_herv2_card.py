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
    assert "<b>路由分诊：</b><code>COMPLEX_TASK</code> · medium" in zh_html
    assert "<code>CODE_MODIFY</code> (代码修改)" in zh_html
    assert "<b>执行概要：</b><code>Refactor routing</code>" in zh_html
    assert "<b>分诊 (Triage)：</b><b>Quick</b> · <code>gemini-2.5-flash</code>" in zh_html
    assert "0.6秒" in zh_html
    assert "350 token" in zh_html
    assert "<b>执行 (Execution)：</b><b>Pro</b> · <code>claude-sonnet-4-6</code>" in zh_html
    assert "3.5秒" in zh_html
    assert "1,500 token" in zh_html
    assert "<b>评审</b>: <code>1</code>" in zh_html
    assert "<b>最终状态</b>: <code>COMPLETED</code>" in zh_html

    # English Telegram HTML
    en_html = format_herv2_card(card_data, locale="en", surface="telegram")
    assert "🧭 <b>HER v2 Routing Card</b>" in en_html
    assert "<b>Route：</b><code>COMPLEX_TASK</code> · medium" in en_html
    assert "<b>Triage：</b><b>Quick</b> · <code>gemini-2.5-flash</code>" in en_html
    assert "0.6s" in en_html
    assert "350 tokens" in en_html
    assert "<b>Reviews</b>: <code>1</code>" in en_html


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
    assert "路由分诊: COMPLEX_TASK · medium" in plain
    assert "CODE_MODIFY (代码修改)" in plain
    assert "• 分诊 (Triage): Quick · gemini-2.5-flash · 0.6秒 · 350 token" in plain


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
