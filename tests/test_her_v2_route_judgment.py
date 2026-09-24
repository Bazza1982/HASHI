from __future__ import annotations

import pytest

from orchestrator.her_v2.models import Effort, Stage, StageRequest, TriageClassification
from orchestrator.her_v2.prompts import (
    render_immediate_response_system_prompt,
    render_internal_stage_system_prompt,
)
from orchestrator.her_v2.route_judgment import (
    ROUTE_CLASSIFICATIONS,
    ROUTE_QUESTION,
    RouteJudgmentConfig,
    parse_route_answer,
)


def test_route_choice_accepts_only_the_four_active_categories() -> None:
    assert ROUTE_CLASSIFICATIONS == (
        TriageClassification.DIRECT_RESPONSE,
        TriageClassification.SIMPLE_TASK,
        TriageClassification.COMPLEX_TASK,
        TriageClassification.CONFIRMATION_REQUIRED,
    )

    answer = parse_route_answer(
        {
            "model": "jev-latest",
            "request_id": "jev-1",
            "answers": {
                "route": {
                    "type": "choice",
                    "choice": "COMPLEX_TASK",
                    "probabilities": {
                        "DIRECT_RESPONSE": 0.02,
                        "SIMPLE_TASK": 0.08,
                        "COMPLEX_TASK": 0.87,
                        "CONFIRMATION_REQUIRED": 0.03,
                    },
                    "confidence": 0.87,
                }
            },
        }
    )
    assert answer.classification is TriageClassification.COMPLEX_TASK
    assert answer.probabilities["COMPLEX_TASK"] == 0.87
    assert answer.provider_request_id == "jev-1"

    with pytest.raises(ValueError, match="retired choice"):
        parse_route_answer(
            {
                "answers": {
                    "route": {
                        "type": "choice",
                        "choice": "HIGH_VOLUME_TASK",
                    }
                }
            }
        )


def test_route_question_limits_confirmation_to_scope_not_authority() -> None:
    instructions = ROUTE_QUESTION["instructions"]
    criteria = ROUTE_QUESTION["criteria"][
        TriageClassification.CONFIRMATION_REQUIRED.value
    ]

    assert "scope/goal clarification route only" in instructions
    assert "typed request envelope and downstream permission/side-effect gates" in instructions
    assert "If the goal and scope are clear, choose SIMPLE_TASK or COMPLEX_TASK" in instructions
    assert "authorization" in instructions
    assert "risk acceptance" in criteria
    assert "technical parameters are not confirmation triggers" in criteria
    assert "significant risk blocks safe execution" not in criteria


def test_route_config_is_opt_in_and_serial_mode_is_explicit() -> None:
    config = RouteJudgmentConfig.from_mapping(
        {
            "enabled": True,
            "serial_initial_response": True,
            "model": "jev-latest",
            "check_timeout_s": 3,
        }
    )
    assert config.enabled is True
    assert config.serial_initial_response is True
    assert config.check_timeout_s == 3.0


def test_strategy_prompt_keeps_existing_cards_but_freezes_jev_route() -> None:
    request = StageRequest(
        turn_id="turn-route",
        request_ref="hashi-request:req-route",
        stage=Stage.TRIAGE,
        role="strategist",
        attempt=1,
        goal="Inspect the target and apply the requested change.",
        classification=None,
        effort=Effort.LOW,
        context={
            "strategy_cards": {"cards": [{"id": "CODE_MODIFY"}]},
            "habit_catalogue": [],
            "execution_capabilities": {},
            "request_resources": {},
            "route_judgment": {
                "classification": "SIMPLE_TASK",
                "probabilities": {"SIMPLE_TASK": 0.8},
                "confidence": 0.8,
            },
        },
        allow_tools=False,
    )
    prompt = render_internal_stage_system_prompt(request)
    assert prompt is not None
    assert "Authoritative JEV route" in prompt
    assert "do not replace the route" in prompt
    assert '"id": "CODE_MODIFY"' in prompt


def test_initial_response_prompt_is_ack_only_when_serial_mode_is_enabled() -> None:
    prompt = render_immediate_response_system_prompt(
        goal="Please inspect the repository and fix the issue.",
        initial_response_only=True,
        guidance="Speak plainly.",
        display_name="Agent",
        usable=True,
        persona_block_begin="[persona]",
        persona_block_end="[persona_end]",
    )
    assert "Serial initial-response mode" in prompt
    assert "Return only a brief natural acknowledgement" in prompt
