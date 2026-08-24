from __future__ import annotations

from pathlib import Path

import pytest

from orchestrator.her_v2 import prompt_catalog
from orchestrator.her_v2.models import (
    Effort,
    Stage,
    StageRequest,
    TriageClassification,
)
from orchestrator.her_v2.prompt_catalog import PromptAssetError
from orchestrator.her_v2.prompts import (
    render_finalisation_system_prompt,
    render_immediate_response_system_prompt,
    render_internal_stage_system_prompt,
    render_persona_commentary_system_prompt,
    render_stage_prompt,
)


def _request(stage: Stage, **context: object) -> StageRequest:
    return StageRequest(
        turn_id="turn-1",
        request_ref="hashi-request:req-1",
        stage=stage,
        role="sub_agent:worker" if context.pop("sub_agent", False) else "primary",
        attempt=1,
        goal="Keep $HOME literal and finish the work.",
        classification=None,
        effort=Effort.MEDIUM,
        context=context,
    )


def test_external_prompt_inventory_is_complete_and_cwd_independent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    files = {path.stem for path in prompt_catalog.PROMPT_ASSET_ROOT.glob("*.txt")}
    assert files == set(prompt_catalog.PROMPT_ASSET_FIELDS)

    monkeypatch.chdir(tmp_path)
    prompt_catalog.validate_prompt_assets()
    assert "triage classifier" in prompt_catalog.load_prompt_asset("system_triage")


def test_external_prompt_placeholder_drift_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "system_dream.txt").write_text(
        "Unexpected placeholder: $wrong", encoding="utf-8"
    )
    monkeypatch.setattr(prompt_catalog, "PROMPT_ASSET_ROOT", tmp_path)
    prompt_catalog.load_prompt_asset.cache_clear()
    try:
        with pytest.raises(PromptAssetError, match="placeholders are invalid"):
            prompt_catalog.load_prompt_asset("system_dream")
    finally:
        prompt_catalog.load_prompt_asset.cache_clear()


@pytest.mark.parametrize("stage", list(Stage))
def test_every_stage_prompt_renders_from_validated_assets(stage: Stage) -> None:
    context: dict[str, object] = {}
    if stage in {Stage.MEDITATION, Stage.DREAM}:
        context["maintenance_prompt"] = "Maintain quoted evidence only."
    if stage is Stage.TRIAGE:
        context["previous_structure_error"] = {
            "attempt": 1,
            "error": "missing classification",
        }
    if stage is Stage.EXECUTION:
        context.update(
            {
                "active_plan": {"plan": ["inspect", "verify"]},
                "sub_agent_results": [{"assignment_id": "a-1"}],
                "sub_agent": True,
                "assignment_id": "a-2",
                "assigned_task": "inspect",
                "delegated_tools": ["file_read"],
            }
        )
    rendered = render_stage_prompt(_request(stage, **context))
    assert rendered.strip()
    if stage not in {Stage.MEDITATION, Stage.DREAM}:
        assert "$HOME" in rendered


def test_system_prompt_renderers_preserve_persona_and_authority_envelopes() -> None:
    request = _request(Stage.EXECUTION, sub_agent=True)
    assert "bounded HER v2 sub-agent" in render_internal_stage_system_prompt(request)

    common = {
        "guidance": "Speak plainly.",
        "display_name": "Agent",
        "usable": True,
        "persona_block_begin": "[persona]",
        "persona_block_end": "[persona_end]",
    }
    assert "Speak plainly." in render_finalisation_system_prompt(**common)
    assert "no tool access" in render_immediate_response_system_prompt(**common)
    assert "Speak plainly." in render_persona_commentary_system_prompt(
        persona_guidance="Speak plainly.",
        persona_block_begin="[persona]",
        persona_block_end="[persona_end]",
    )


def test_review_and_verification_prompts_enforce_tool_backed_bounded_evidence() -> None:
    review_system = render_internal_stage_system_prompt(_request(Stage.REVIEW))
    verification_system = render_internal_stage_system_prompt(
        _request(Stage.VERIFICATION)
    )
    review_request = render_stage_prompt(_request(Stage.REVIEW))
    verification_request = render_stage_prompt(_request(Stage.VERIFICATION))

    assert "delegated read-only" in review_system
    assert "workspace_inspect operation=snapshot" in review_system
    assert "UNAVAILABLE, never CONDITIONAL_PASS" in review_system
    assert "HASHI_EVIDENCE_RECEIPT" in review_request
    assert "PASS | CONDITIONAL_PASS | FAIL | INCONCLUSIVE | UNAVAILABLE" in (
        review_request
    )

    assert "Assured Verifier" in verification_system
    assert "authoritative current workspace" in verification_system
    assert "inherits HASHI's process identity" in verification_system
    assert "automatically grows from the cumulative Execution duration" in (
        verification_system
    )
    assert "workspace_test" in verification_system
    assert "Fabricated, stale, or prior-invocation references are forbidden" in (
        verification_system
    )
    assert "verification_run operation=run" in verification_system
    assert "PARTIALLY_VERIFIED" in verification_request
    assert "NOT_AI_VERIFIABLE" in verification_request


def test_triage_uses_one_complete_prompt_without_checkpoint_risk_metadata() -> None:
    system_prompt = render_internal_stage_system_prompt(_request(Stage.TRIAGE))
    stage_prompt = render_stage_prompt(_request(Stage.TRIAGE))

    assert system_prompt == stage_prompt
    assert "TRIAGE AGENT" in stage_prompt or "TRAIGE AGENT" in stage_prompt
    assert "User request and context:" in stage_prompt
    assert "checkpoint_policy" not in stage_prompt
    assert "checkpoint_reason" not in stage_prompt


def test_planning_uses_one_complete_prompt_with_all_runtime_inputs() -> None:
    request = StageRequest(
        turn_id="turn-1",
        request_ref="hashi-request:req-1",
        stage=Stage.PLANNING,
        role="primary",
        attempt=1,
        goal="Complete the supplied goal.",
        classification=TriageClassification.COMPLEX_TASK,
        effort=Effort.MEDIUM,
        context={"habits": ["Habit one", "Habit two"]},
    )

    rendered = render_stage_prompt(request)
    assert render_internal_stage_system_prompt(request) == rendered
    assert "Complete the supplied goal." in rendered
    assert "`COMPLEX_TASK`" in rendered
    assert "Habit one\n\nHabit two" in rendered
    assert '"success_criteria"' in rendered
    assert "$goal" not in rendered
    assert "$classification" not in rendered
    assert "$all_active_habits" not in rendered
    assert "$schema" not in rendered


def test_replanning_uses_complete_prompt_with_real_runtime_inputs() -> None:
    request = StageRequest(
        turn_id="turn-1",
        request_ref="hashi-request:req-1",
        stage=Stage.REPLANNING,
        role="primary",
        attempt=1,
        goal="Complete the authorised repair.",
        classification=TriageClassification.COMPLEX_TASK,
        effort=Effort.HIGH,
        plan_id="turn-1:plan:v2",
        context={
            "active_plan": {
                "plan": ["Inspect", "Repair", "Verify"],
                "success_criteria": ["The repair is verified"],
            },
            "plan_edit_history": [
                {
                    "revision": 1,
                    "plan_changed": True,
                    "change_reason": "The first route was unavailable.",
                }
            ],
            "workflow_state_and_evidence": {
                "ledger": {"status": "REPLANNING"},
                "evidence_refs": ["receipt:verified-change"],
                "review": {"outcome": "FAIL"},
            },
        },
    )

    stage_prompt = render_stage_prompt(request)
    assert render_internal_stage_system_prompt(request) == stage_prompt
    assert "Complete the authorised repair." in stage_prompt
    assert "COMPLEX_TASK" in stage_prompt
    assert '"Repair"' in stage_prompt
    assert '"revision": 1' in stage_prompt
    assert '"receipt:verified-change"' in stage_prompt
    assert '"completion_percent"' in stage_prompt
    assert '"commentary"' in stage_prompt
    assert "$active_plan" not in stage_prompt
    assert "$plan_edit_history" not in stage_prompt
    assert "$workflow_state_and_evidence" not in stage_prompt
    assert "$schema" not in stage_prompt
