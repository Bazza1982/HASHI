from __future__ import annotations

from pathlib import Path

import pytest

from orchestrator.her_v2 import prompt_catalog
from orchestrator.her_v2.models import Effort, Stage, StageRequest
from orchestrator.her_v2.prompt_catalog import PromptAssetError
from orchestrator.her_v2.prompts import (
    render_finalisation_system_prompt,
    render_immediate_response_system_prompt,
    render_internal_stage_system_prompt,
    render_persona_commentary_system_prompt,
    render_persona_required_message_system_prompt,
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
    assert "Triage classifier" in prompt_catalog.load_prompt_asset("system_triage")


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
    assert "Do not add a question" in render_persona_required_message_system_prompt(
        message_kind="final",
        kind_rule="Do not add a question.",
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
