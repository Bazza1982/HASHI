from __future__ import annotations

import pytest

from orchestrator.her_v2.interfaces import StructuredOutputError
from orchestrator.her_v2.models import (
    Effort,
    Stage,
    StageRequest,
    StageResponse,
    StrategyDecision,
    TriageClassification,
)
from orchestrator.her_v2.prompts import render_internal_stage_system_prompt
from orchestrator.her_v2.strategy_playbook import (
    StrategyPlaybookError,
    load_strategy_playbook,
)
from orchestrator.her_v2.structured import parse_strategy


def _brief(strategy: str = "Inspect, change, and verify.") -> dict[str, object]:
    return {
        "strategy": strategy,
        "stages": ["Inspect", "Change", "Verify"] if strategy else [],
        "dependencies": ["Change follows inspection"] if strategy else [],
        "verification": ["Run focused checks"] if strategy else [],
        "success_criteria": ["The requested outcome works"] if strategy else [],
        "replan_conditions": ["The observed architecture differs"] if strategy else [],
    }


def test_external_strategy_playbook_is_complete_versioned_and_resolvable() -> None:
    playbook = load_strategy_playbook()

    assert playbook.playbook_version == "2026-08-29.1"
    assert len(playbook.cards) == 38
    assert len(set(playbook.card_ids)) == 38
    assert playbook.sha256.startswith("sha256:")
    selected = playbook.resolve_cards(["CODE_MODIFY", "TEST_QA"])
    assert [card["id"] for card in selected] == ["CODE_MODIFY", "TEST_QA"]
    assert all(card["content"] for card in selected)
    assert all(card["strategy"] for card in selected)
    assert all(card["validation"] for card in selected)
    assert all(isinstance(card["topology"], dict) for card in selected)
    with pytest.raises(StrategyPlaybookError, match="unknown Strategy Card ID"):
        playbook.resolve_cards(["NOT_A_REAL_CARD"])


def test_strategy_schema_v3_preserves_card_ids_and_strategic_brief() -> None:
    decision = parse_strategy(
        StageResponse(
            data={
                "classification": "SIMPLE_TASK",
                "real_goal": "Modify and verify the target.",
                "selected_strategy_cards": ["CODE_MODIFY", "TEST_QA"],
                "relevant_habits": ["[inspect-first] Inspect current state."],
                "execution_brief": _brief(),
                "clarification": None,
            }
        )
    )

    assert isinstance(decision, StrategyDecision)
    assert decision.classification is TriageClassification.SIMPLE_TASK
    assert decision.selected_strategy_cards == ("CODE_MODIFY", "TEST_QA")
    assert decision.execution_brief["strategy"] == "Inspect, change, and verify."


def test_strategy_schema_v3_keeps_confirmation_empty_and_concrete() -> None:
    decision = parse_strategy(
        StageResponse(
            data={
                "classification": "CONFIRMATION_REQUIRED",
                "real_goal": None,
                "selected_strategy_cards": [],
                "relevant_habits": [],
                "execution_brief": _brief(""),
                "clarification": "Which target should be changed?",
            }
        )
    )
    assert decision.clarification == "Which target should be changed?"

    with pytest.raises(StructuredOutputError, match="empty execution_brief"):
        parse_strategy(
            StageResponse(
                data={
                    "classification": "CONFIRMATION_REQUIRED",
                    "real_goal": None,
                    "selected_strategy_cards": [],
                    "relevant_habits": [],
                    "execution_brief": _brief(),
                    "clarification": "Which target should be changed?",
                }
            )
        )


def test_strategy_system_prompt_receives_full_playbook_capabilities_and_resources() -> None:
    playbook = load_strategy_playbook()
    request = StageRequest(
        turn_id="turn-strategy",
        request_ref="hashi-request:req-strategy",
        stage=Stage.TRIAGE,
        role="strategist",
        attempt=1,
        goal="Modify the supplied repository and verify the result.",
        classification=None,
        effort=Effort.LOW,
        context={
            "strategy_cards": playbook.prompt_payload(),
            "habit_catalogue": ["[inspect-first] Inspect current state."],
            "execution_capabilities": {
                "tools": [{"function": {"name": "file_read"}}],
                "skills": [{"name": "debug"}],
                "allow_side_effects": True,
            },
            "request_resources": {
                "attachments": [{"attachment_id": "attachment-1"}]
            },
        },
        allow_tools=True,
        allow_side_effects=True,
    )

    prompt = render_internal_stage_system_prompt(request)
    assert prompt is not None
    assert '"id": "SIMPLE_QA"' in prompt
    assert '"id": "HIGH_RISK_ACTION"' in prompt
    assert '"name": "file_read"' in prompt
    assert '"name": "debug"' in prompt
    assert '"attachment_id": "attachment-1"' in prompt
    assert '"selected_strategy_cards"' in prompt
    assert '"execution_brief"' in prompt
    assert "$strategy_cards" not in prompt
    assert "$schema_v3" not in prompt
