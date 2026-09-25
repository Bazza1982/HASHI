from __future__ import annotations

from orchestrator.her_v2.config import HERv2Config
from orchestrator.her_v2.models import Stage, parse_effort
from orchestrator.her_v2.turn_services import parse_health
from orchestrator.her_v2.v3_prompt import compile_main_prompt


def _legacy_config():
    return {
        "profiles": {
            "lightweight": {"engine": "deepseek-api", "model": "deepseek-flash", "reasoning": "high"},
            "premium": {"engine": "deepseek-api", "model": "deepseek-v4-pro", "reasoning": "high"},
        },
        "stage_roles": {"direct": "lightweight", "execution": "premium"},
    }


def test_legacy_config_collapses_to_one_main_foreground_model():
    config = HERv2Config.from_mapping(_legacy_config())
    assert set(config.profiles) == {"main", "auxiliary"}
    assert config.profile_for(Stage.DIRECT).model == "deepseek-v4-pro"
    assert config.profile_for(Stage.EXECUTION).model == "deepseek-v4-pro"


def test_effort_none_is_model_reasoning_wire_alias():
    assert parse_effort("none").value == "zero"
    assert parse_effort("xhigh").value == "xhigh"


def test_strategy_cards_are_optional_context_not_workflow():
    system, user = compile_main_prompt(
        pcm_input={"current_request": "do the task", "sections": [], "history": []},
        fallback_request="fallback",
        context={"strategy_playbook": {"cards": [{"id": "SAFE"}]}, "habit_catalogue": []},
    )
    assert "optional advice" in system
    assert "No mandatory selection" in system
    assert "do the task" in user


def test_agent_companion_only_intervenes_on_high_confidence_trouble():
    payload = {
        "answers": {
            "health": {
                "type": "choice",
                "choice": "trouble",
                "probabilities": {"continue": 0.05, "trouble": 0.9, "unknown": 0.05},
            }
        }
    }
    assert parse_health(payload)
    payload["answers"]["health"]["probabilities"] = {"continue": 0.2, "trouble": 0.7, "unknown": 0.1}
    assert not parse_health(payload)
