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
    json_repair_schema_for_stage,
    render_direct_system_prompt,
    render_execution_system_prompt,
    render_finalisation_system_prompt,
    render_immediate_response_system_prompt,
    render_internal_stage_system_prompt,
    render_persona_commentary_system_prompt,
    render_review_system_prompt,
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
    assert "task strategist" in prompt_catalog.load_prompt_asset("system_strategy")


def test_json_repair_prompt_is_generic_tool_free_and_data_driven() -> None:
    prompt = prompt_catalog.load_prompt_asset("system_json_repair")

    assert "JSON Repair Agent" in prompt
    assert "rejected_output" in prompt
    assert "required_schema" in prompt
    assert "validation_error" in prompt
    assert "Do not call tools" in prompt
    assert "Do not introduce facts" in prompt
    assert "Return only the repaired JSON" in prompt

    request = _request(
        Stage.JSON_REPAIR,
        json_repair_input=(
            '{"rejected_output":"bad","required_schema":{"value":"string"},'
            '"validation_error":"missing value"}'
        ),
    )
    assert render_stage_prompt(request) == request.context["json_repair_input"]
    assert render_internal_stage_system_prompt(request) == prompt


def test_every_live_json_contract_routes_to_specialist_schema() -> None:
    for stage in (
        Stage.TRIAGE,
        Stage.PLANNING,
        Stage.REPLANNING,
        Stage.REVIEW,
    ):
        assert json_repair_schema_for_stage(stage, role="primary") is not None

    assert (
        json_repair_schema_for_stage(
            Stage.EXECUTION,
            role="sub_agent:worker",
        )
        is not None
    )
    for stage in (
        Stage.DIRECT,
        Stage.IMMEDIATE_RESPONSE,
        Stage.EXECUTION,
        Stage.FINALISATION,
        Stage.MEDITATION,
        Stage.DREAM,
        Stage.JSON_REPAIR,
    ):
        assert json_repair_schema_for_stage(stage, role="primary") is None


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
def test_every_stage_has_a_non_empty_provider_user_turn(stage: Stage) -> None:
    context: dict[str, object] = {}
    if stage is Stage.MEDITATION:
        context["meditation_input"] = '{"mode":"initial"}'
    if stage is Stage.DREAM:
        context["dream_input"] = '{"mode":"initial"}'
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
    request = _request(
        Stage.EXECUTION,
        sub_agent=True,
        real_goal="Inspect the assigned target.",
        relevant_habits=["Inspect current state before reporting."],
        assignment_id="worker",
        assigned_task="Inspect one target",
    )
    subagent_system = render_internal_stage_system_prompt(request)
    assert subagent_system is not None
    assert "bounded HER v2 sub-agent" in subagent_system
    assert "Inspect the assigned target." in subagent_system
    assert "Inspect current state before reporting." in subagent_system

    common = {
        "guidance": "Speak plainly.",
        "display_name": "Agent",
        "usable": True,
        "persona_block_begin": "[persona]",
        "persona_block_end": "[persona_end]",
    }
    finalisation = render_finalisation_system_prompt(
        goal="Complete the requested work.",
        relevant_habits=["Preserve evidence."],
        draft_response="The requested work is complete.",
        reviewer_findings={
            "status": "CONDITIONAL_PASS",
            "reason": "The result is substantially complete.",
            "conditions": "One limitation remains.",
        },
        completion_evidence={"evidence_refs": ["receipt:42"]},
        **common,
    )
    assert "Speak plainly." in finalisation
    assert "The requested work is complete." in finalisation
    assert finalisation.count("The requested work is complete.") == 1
    assert '"status": "CONDITIONAL_PASS"' in finalisation
    assert '"evidence_refs"' in finalisation
    assert "Return only the final user-facing response" in finalisation
    assert "Return exactly one JSON object" not in finalisation
    assert "$draft_response" not in finalisation
    immediate = render_immediate_response_system_prompt(
        goal="Explain the supplied result.", **common
    )
    assert "Speak plainly." in immediate
    assert "Explain the supplied result." in immediate
    assert "Choose exactly one" in immediate
    assert "$goal" not in immediate
    assert "Speak plainly." in render_persona_commentary_system_prompt(
        persona_guidance="Speak plainly.",
        persona_block_begin="[persona]",
        persona_block_end="[persona_end]",
    )

    execution = render_execution_system_prompt(
        goal="Inspect and report the current state.",
        relevant_habits=["Inspect current state before reporting."],
        active_plan={"plan": ["inspect", "report"]},
        delegated_execution={
            "plan_id": "turn-1:plan:v1",
            "results": [{"assignment_id": "worker", "summary": "Inspected."}],
        },
        strategy_handoff={
            "selected_strategy_cards": [
                {
                    "id": "CODE_MODIFY",
                    "version": "1.0.0",
                    "title": "功能修改与扩展",
                    "content": "Inspect architecture, modify, and verify.",
                }
            ],
            "execution_brief": {
                "strategy": "Inspect, modify, and verify.",
                "stages": ["Inspect", "Modify", "Verify"],
                "dependencies": ["Modify follows inspection"],
                "verification": ["Run focused tests"],
                "success_criteria": ["Requested behaviour works"],
                "replan_conditions": ["Architecture differs from assumptions"],
            },
        },
        tool_catalogue=[{"function": {"name": "file_read"}}],
        **common,
    )
    assert "Inspect and report the current state." in execution
    assert '"inspect"' in execution
    assert '"name": "file_read"' in execution
    assert '"plan_id": "turn-1:plan:v1"' in execution
    assert '"assignment_id": "worker"' in execution
    assert '"id": "CODE_MODIFY"' in execution
    assert "Inspect, modify, and verify." in execution
    assert "Inspect current state before reporting." in execution
    assert "Speak plainly." in execution
    assert "Return exactly one JSON object" not in execution
    assert "$goal" not in execution


def test_meditation_uses_complete_system_contract_and_data_only_user_input() -> None:
    meditation_input = '{"mode":"initial","max_actions":3}'
    request = _request(Stage.MEDITATION, meditation_input=meditation_input)

    assert render_stage_prompt(request) == meditation_input
    system_prompt = render_internal_stage_system_prompt(request)
    assert system_prompt is not None
    assert "agent task reflection agent" in system_prompt
    assert "JSON Repair Agent" not in system_prompt
    assert '"actions"' in system_prompt
    assert "$maintenance_prompt" not in system_prompt


def test_dream_uses_separate_maintenance_and_persona_report_contracts() -> None:
    maintenance_input = '{"mode":"initial"}'
    maintenance = _request(
        Stage.DREAM,
        dream_input=maintenance_input,
        dream_role="maintenance",
    )
    report_input = '{"mode":"persona_report"}'
    report = _request(
        Stage.DREAM,
        dream_input=report_input,
        dream_role="report",
    )

    assert render_stage_prompt(maintenance) == maintenance_input
    maintenance_system = render_internal_stage_system_prompt(maintenance)
    report_system = render_internal_stage_system_prompt(report)
    assert maintenance_system is not None
    assert report_system is not None
    assert "Habit Maintenance Agent" in maintenance_system
    assert '"groups"' in maintenance_system
    assert "Persona Report Renderer" not in maintenance_system
    assert "Persona Report Renderer" in report_system
    assert "Return only the message" in report_system
    assert '"groups"' not in report_system


def test_primary_execution_uses_natural_language_prompt_while_subagent_keeps_json() -> (
    None
):
    primary = _request(
        Stage.EXECUTION,
        active_plan={"plan": ["inspect"]},
    )
    subagent = _request(
        Stage.EXECUTION,
        sub_agent=True,
        assignment_id="worker",
        assigned_task="Inspect one target",
        delegated_tools=["file_read"],
        real_goal="Inspect the authorised target.",
        relevant_habits=["Use current workspace evidence."],
    )

    assert render_stage_prompt(primary) == primary.goal
    assert render_internal_stage_system_prompt(primary) is None
    subagent_prompt = render_stage_prompt(subagent)
    subagent_system = render_internal_stage_system_prompt(subagent)
    assert subagent_prompt == subagent.goal
    assert subagent_system is not None
    assert "Return exactly one valid JSON object" in subagent_system
    assert "Bounded assignment and authority envelope" in subagent_system
    assert '"may_replan": false' in subagent_system
    assert "Inspect the authorised target." in subagent_system
    assert "Use current workspace evidence." in subagent_system
    subagent_assets = {
        name for name in prompt_catalog.PROMPT_ASSET_FIELDS if "sub_agent" in name
    }
    assert subagent_assets == {"system_sub_agent"}


def test_planning_binds_high_volume_assignments_to_runtime_plan_snapshot() -> None:
    prompt = prompt_catalog.load_prompt_asset("system_planning")

    assert "only for `HIGH_VOLUME_TASK`" in prompt
    assert "combination of `plan_id` and assignment `id`" in prompt
    assert "Do not invent a `plan_id`" in prompt
    assert (
        "runtime explicitly attaches to the current authoritative plan snapshot"
        in prompt
    )
    assert "unrelated historical batches" in prompt


def test_planning_renders_exact_available_subagent_profile_names() -> None:
    request = _request(
        Stage.PLANNING,
        available_sub_agent_profiles=["lightweight", "premium", "orchestrator"],
    )

    rendered = render_stage_prompt(request)

    assert '["lightweight", "premium", "orchestrator"]' in rendered
    assert "Do not invent aliases such as `default`" in rendered
    assert "$available_sub_agent_profiles" not in rendered


@pytest.mark.parametrize("stage", [Stage.PLANNING, Stage.REPLANNING])
def test_planning_roles_receive_exact_registered_tool_catalogue(stage: Stage) -> None:
    request = _request(
        stage,
        available_execution_tools=[
            {
                "type": "function",
                "function": {"name": "file_read"},
                "hashi_read_only": True,
            },
            {
                "type": "function",
                "function": {"name": "file_write"},
                "hashi_read_only": False,
            },
        ],
    )

    rendered = render_stage_prompt(request)

    assert '"name": "file_read"' in rendered
    assert '"name": "file_write"' in rendered
    assert '"hashi_read_only": true' in rendered
    assert "Never invent a tool or capability name" in rendered
    assert "$available_execution_tools" not in rendered
    if stage is Stage.PLANNING:
        assert "Never add multiple progress messages" in rendered
    else:
        assert "Produce exactly one concise user-facing commentary" in rendered


def test_replanning_requires_explicit_reuse_in_replacement_plan() -> None:
    prompt = prompt_catalog.load_prompt_asset("system_replanning")

    assert (
        "Results and assignments from the previous plan do not automatically enter"
        in prompt
    )
    assert "explicitly preserves or redefines the corresponding assignment" in prompt
    assert "combination of `plan_id` and assignment `id`" in prompt
    assert "must not search for or adopt unrelated historical batches" in prompt


def test_immediate_response_uses_filtered_goal_without_a_second_prompt_contract() -> (
    None
):
    request = StageRequest(
        turn_id="turn-1",
        request_ref="hashi-request:req-1",
        stage=Stage.IMMEDIATE_RESPONSE,
        role="primary",
        attempt=1,
        goal="""Bridge-managed context follows.

--- ADDITIONAL SYSTEM CONTEXT ---

GLOBAL SYS CONTENT

--- RECENT CONTEXT ---

USER: Earlier context remains available.

--- CURRENT USER REQUEST — AUTHORITATIVE ---

Please inspect the request.""",
        classification=None,
        effort=Effort.LOW,
    )

    user_prompt = render_stage_prompt(request)
    system_prompt = render_immediate_response_system_prompt(
        goal=request.goal,
        guidance="Address the user as Captain.",
        display_name="Agent",
        usable=True,
        persona_block_begin="[persona]",
        persona_block_end="[persona_end]",
    )

    assert "GLOBAL SYS CONTENT" not in user_prompt
    assert "GLOBAL SYS CONTENT" not in system_prompt
    assert "Earlier context remains available." in user_prompt
    assert "Earlier context remains available." in system_prompt
    assert "Please inspect the request." in user_prompt
    assert "Please inspect the request." in system_prompt
    assert "Return exactly one JSON object" not in user_prompt
    assert "Return exactly one JSON object" not in system_prompt


def test_direct_prompt_is_one_natural_language_agent_with_full_catalogues() -> None:
    request = _request(Stage.DIRECT)
    system_prompt = render_direct_system_prompt(
        goal=request.goal,
        habit_catalogue=["Check the current workspace before reporting success."],
        skills_catalogue=[
            {
                "id": "reports",
                "description": "Build reports",
                "skill_md": "/skills/reports/SKILL.md",
            }
        ],
        tool_catalogue=[
            {
                "type": "function",
                "function": {"name": "file_write"},
                "hashi_read_only": False,
            }
        ],
        guidance="Address the user as Captain.",
        display_name="Agent",
        usable=True,
        persona_block_begin="[persona]",
        persona_block_end="[persona_end]",
    )

    assert render_stage_prompt(request) == request.goal
    assert render_internal_stage_system_prompt(request) is None
    assert "zero-orchestration Direct route" in system_prompt
    assert "Never hand the task off" in system_prompt
    assert "request an orchestration upgrade" in system_prompt
    assert '"name": "file_write"' in system_prompt
    assert '"id": "reports"' in system_prompt
    assert "Check the current workspace" in system_prompt
    assert "Address the user as Captain." in system_prompt
    assert "Return only the natural-language response" in system_prompt
    assert "$tool_catalogue" not in system_prompt


def test_prompt_tool_catalogue_keeps_guidance_without_duplicate_parameters() -> None:
    system_prompt = render_direct_system_prompt(
        goal="Read one file.",
        habit_catalogue=[],
        skills_catalogue=[],
        tool_catalogue=[
            {
                "type": "function",
                "function": {
                    "name": "file_read",
                    "description": "Read one file; this does not verify correctness.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string", "description": "SECRET_SCHEMA_MARKER"}
                        },
                    },
                },
                "hashi_read_only": True,
            }
        ],
        guidance="",
        display_name="Agent",
        usable=False,
        persona_block_begin="[persona]",
        persona_block_end="[persona_end]",
    )

    assert '"name": "file_read"' in system_prompt
    assert "Read one file; this does not verify correctness." in system_prompt
    assert '"hashi_read_only": true' in system_prompt
    assert "SECRET_SCHEMA_MARKER" not in system_prompt
    assert '"parameters"' not in system_prompt


def test_review_uses_one_complete_prompt_with_draft_plan_and_actual_tools() -> None:
    request = _request(Stage.REVIEW)
    review_system = render_review_system_prompt(
        goal=request.goal,
        relevant_habits=["Inspect current state before reporting."],
        active_plan_id="turn-1:plan:v2",
        active_plan={"plan": ["implement"], "success_criteria": ["tests pass"]},
        draft_response="Implemented the requested change.",
        execution_record={
            "disposition": "COMPLETED",
            "summary": "Implemented the requested change.",
        },
        evidence_refs=["receipt:execution:42"],
        review_kind="closure",
        findings_to_close=["The original test evidence was incomplete."],
        available_review_tools=[{"function": {"name": "workspace_inspect"}}],
    )

    assert render_stage_prompt(request) == request.goal
    assert render_internal_stage_system_prompt(request) is None
    review_assets = {
        name for name in prompt_catalog.PROMPT_ASSET_FIELDS if "review" in name
    }
    assert review_assets == {"system_review"}
    assert "stage_request" not in prompt_catalog.PROMPT_ASSET_FIELDS
    assert "Implemented the requested change." in review_system
    assert '"active_plan_id": "turn-1:plan:v2"' in review_system
    assert '"review_kind": "closure"' in review_system
    assert "The original test evidence was incomplete." in review_system
    assert '"disposition": "COMPLETED"' in review_system
    assert '"receipt:execution:42"' in review_system
    assert '"name": "workspace_inspect"' in review_system
    assert "authoritative resolved goal" in review_system
    assert "original request" not in review_system
    assert "Inspect current state before reporting." not in review_system
    assert '"tests pass"' not in review_system
    assert "PASS" in review_system
    assert "CONDITIONAL_PASS" in review_system
    assert "FAIL" in review_system
    assert "Do not contact the user" in review_system
    assert "change the immutable Triage classification" in review_system
    assert "write the final user-facing answer" in review_system
    assert "INCONCLUSIVE" not in review_system
    assert "UNAVAILABLE" not in review_system
    assert "$goal" not in review_system
    assert "$draft_response" not in review_system
    assert "$review_context" not in review_system
    assert "$execution_evidence" not in review_system
    assert "$relevant_habits" not in review_system
    assert "$active_plan" not in review_system


def test_strategy_uses_schema_v3_with_playbook_capabilities_and_resources() -> None:
    request = _request(
        Stage.TRIAGE,
        habit_catalogue=["[inspect-first] Inspect current state before reporting."],
        strategy_cards={
            "playbook_version": "1",
            "cards": [
                {
                    "id": "CODE_MODIFY",
                    "version": "1.0.0",
                    "title": "Modify code",
                    "content": "Inspect, modify, and verify.",
                }
            ],
        },
        execution_capabilities={"tools": [{"function": {"name": "file_read"}}]},
        request_resources={"attachments": [{"attachment_id": "attachment-1"}]},
    )
    system_prompt = render_internal_stage_system_prompt(request)
    stage_prompt = render_stage_prompt(request)

    assert system_prompt == stage_prompt
    assert "task strategist and context preparation agent" in stage_prompt
    assert "Original user request and context" in stage_prompt
    assert '"real_goal"' in stage_prompt
    assert '"selected_strategy_cards"' in stage_prompt
    assert '"execution_brief"' in stage_prompt
    assert '"relevant_habits"' in stage_prompt
    assert '"id": "CODE_MODIFY"' in stage_prompt
    assert '"name": "file_read"' in stage_prompt
    assert '"attachment_id": "attachment-1"' in stage_prompt
    assert "[inspect-first] Inspect current state before reporting." in stage_prompt
    assert "$schema_v3" not in stage_prompt
    assert "$strategy_cards" not in stage_prompt
    assert "$habit_catalogue" not in stage_prompt
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
        context={"relevant_habits": ["Habit one", "Habit two"]},
    )

    rendered = render_stage_prompt(request)
    assert render_internal_stage_system_prompt(request) == rendered
    assert "Complete the supplied goal." in rendered
    assert "`COMPLEX_TASK`" in rendered
    assert '"Habit one"' in rendered
    assert '"Habit two"' in rendered
    assert '"success_criteria"' in rendered
    assert "$goal" not in rendered
    assert "$classification" not in rendered
    assert "$relevant_habits" not in rendered
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
