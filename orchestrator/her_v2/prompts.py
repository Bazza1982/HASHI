"""Provider-neutral stage envelopes for HER v2 model roles."""

from __future__ import annotations

import copy
import json
from typing import Any, Mapping, Sequence

from .models import Stage, StageRequest
from .prompt_catalog import load_prompt_asset, render_prompt_asset

_SCHEMAS = {
    Stage.TRIAGE: {
        "classification": (
            "DIRECT_RESPONSE | SIMPLE_TASK | COMPLEX_TASK | "
            "HIGH_VOLUME_TASK | CONFIRMATION_REQUIRED"
        ),
        "goal": (
            "optional concise interpretation of the current request, or null when "
            "unnecessary"
        ),
        "clarification": (
            "a concrete question required only for CONFIRMATION_REQUIRED; otherwise "
            "null"
        ),
    },
    Stage.PLANNING: {
        "plan": ["ordered, concrete action"],
        "success_criteria": ["observable criterion"],
        "parallel_groups": [["assignment IDs in one concurrent execution wave"]],
        "sub_agents": [
            {
                "id": "unique bounded assignment id",
                "task": "bounded task",
                "profile": "configured execution profile",
                "tools": [],
                "attachment_ids": [
                    "only attachment_id values required and explicitly delegated"
                ],
                "allow_side_effects": False,
            }
        ],
        "commentary": (
            "optional concise neutral user-facing update based only on this "
            "completed stage result; omit when no useful update exists"
        ),
    },
    Stage.REPLANNING: {
        "plan": [
            "complete active plan for the next version; preserve the prior steps "
            "when current evidence does not require a change"
        ],
        "success_criteria": [
            "observable criterion tied to the authoritative user goal"
        ],
        "completion_percent": "integer from 0 through 100",
        "completion_basis": (
            "evidence-based comparison with the original goal, authority, and "
            "success criteria"
        ),
        "plan_changed": True,
        "change_reason": (
            "concrete new evidence or condition requiring the change; null when "
            "plan_changed is false"
        ),
        "next_step": (
            "next authorised action, or Review/Finalisation when completion is 100"
        ),
        "parallel_groups": [["assignment IDs in one concurrent execution wave"]],
        "sub_agents": [
            {
                "id": "unique bounded assignment id",
                "task": "bounded task",
                "profile": "configured execution profile",
                "tools": [],
                "attachment_ids": [
                    "only attachment_id values required and explicitly delegated"
                ],
                "allow_side_effects": False,
            }
        ],
        "commentary": (
            "required concise neutral update stating completion percentage, whether "
            "the plan changed, why when changed, and the next step; Runtime creates "
            "a deterministic fallback from validated fields if omitted"
        ),
    },
    Stage.EXECUTION: {
        "disposition": (
            "COMPLETED | COMPLETED_WITH_LIMITATIONS | FAILED | USER_INPUT_REQUIRED"
        ),
        "summary": "truthful result based only on actual execution evidence",
        "work_performed": ["concrete action actually performed"],
        "verification": ["check actually run and its result"],
        "evidence_refs": [],
        "limitations": [],
        "remaining_work": [],
        "clarification": "required only for USER_INPUT_REQUIRED",
        "commentary": (
            "optional concise neutral user-facing update based only on this "
            "completed stage result; omit when no useful update exists"
        ),
    },
    Stage.REVIEW: {
        "status": "PASS | CONDITIONAL_PASS | FAIL",
        "reason": "reason for the review decision",
        "conditions": (
            "material conditions for CONDITIONAL_PASS; null for PASS or FAIL"
        ),
    },
    Stage.VERIFICATION: {
        "outcome": (
            "VERIFIED | PARTIALLY_VERIFIED | FAILED | NOT_AI_VERIFIABLE | "
            "UNAVAILABLE | INCONCLUSIVE"
        ),
        "summary": "comprehensive assurance result without changing Execution status",
        "checks": [
            {
                "claim": "one concrete result claim or success criterion",
                "verifiability": (
                    "VERIFIABLE | PARTIALLY_VERIFIABLE | NOT_AI_VERIFIABLE | "
                    "UNAVAILABLE"
                ),
                "result": (
                    "VERIFIED | PARTIALLY_VERIFIED | FAILED | "
                    "NOT_AI_VERIFIABLE | UNAVAILABLE | INCONCLUSIVE"
                ),
                "method": (
                    "workspace_test | workspace_snapshot | workspace_status | "
                    "workspace_diff | workspace_search | file_hash | "
                    "artifact_inspection | process_health | read_only_api | "
                    "visual_inspection"
                ),
                "evidence_refs": [
                    "exact HASHI_EVIDENCE_RECEIPT values from this Verification invocation"
                ],
                "observed": "what the current evidence actually established",
                "required": True,
            }
        ],
        "evidence_refs": [],
        "limitations": [],
        "commentary": (
            "optional concise neutral user-facing update based only on this "
            "completed stage result; omit when no useful update exists"
        ),
    },
    Stage.MEDITATION: {"actions": []},
    Stage.DREAM: {"groups": []},
}


_JSON_REPAIR_STAGES = frozenset(
    {
        Stage.TRIAGE,
        Stage.PLANNING,
        Stage.REPLANNING,
        Stage.REVIEW,
        Stage.VERIFICATION,
    }
)


def json_repair_schema_for_stage(
    stage: Stage,
    *,
    role: str = "",
) -> Mapping[str, Any] | None:
    """Return the frozen schema for stages whose model answer must be JSON."""

    if stage in _JSON_REPAIR_STAGES:
        return copy.deepcopy(_SCHEMAS[stage])
    if stage is Stage.EXECUTION and str(role).startswith("sub_agent:"):
        return copy.deepcopy(_SCHEMAS[Stage.EXECUTION])
    return None


_BRIDGE_CURRENT_REQUEST_MARKER = "--- CURRENT USER REQUEST — AUTHORITATIVE ---"
_IMMEDIATE_OMITTED_BRIDGE_SECTIONS = frozenset(
    {"ADDITIONAL SYSTEM CONTEXT", "SYSTEM IDENTITY"}
)


def _immediate_response_goal(goal: str) -> str:
    """Remove identity and /sys packaging that Immediate must not consume."""

    goal = str(goal or "")
    if _BRIDGE_CURRENT_REQUEST_MARKER not in goal:
        return goal.strip()

    rendered: list[str] = []
    omitting = False
    before_current_request = True
    for line in goal.splitlines():
        stripped = line.strip()
        if stripped == _BRIDGE_CURRENT_REQUEST_MARKER:
            before_current_request = False
            omitting = False
            rendered.append(line)
            continue
        if (
            before_current_request
            and stripped.startswith("--- ")
            and stripped.endswith(" ---")
        ):
            title = stripped[4:-4].strip()
            omitting = title in _IMMEDIATE_OMITTED_BRIDGE_SECTIONS
            if omitting:
                continue
        if not omitting:
            rendered.append(line)
    return "\n".join(rendered).strip()


def render_stage_prompt(request: StageRequest) -> str:
    json_repair_input = request.context.get("json_repair_input")
    if request.stage is Stage.JSON_REPAIR:
        return json_repair_input if isinstance(json_repair_input, str) else request.goal
    meditation_input = request.context.get("meditation_input")
    if request.stage is Stage.MEDITATION and isinstance(meditation_input, str):
        # All Meditation instructions live in its isolated system prompt.
        # The provider-facing user turn contains data only.
        return meditation_input
    dream_input = request.context.get("dream_input")
    if request.stage is Stage.DREAM and isinstance(dream_input, str):
        # Dream maintenance and Persona reporting use separate isolated system
        # contracts.  Their provider-facing user turns contain data only.
        return dream_input
    if request.stage is Stage.IMMEDIATE_RESPONSE:
        # The complete Immediate Response contract, including this same filtered
        # goal, is installed as its isolated system prompt.  Keep a non-empty
        # user turn for provider compatibility without introducing a second
        # prompt asset or a conflicting structured-output instruction.
        return _immediate_response_goal(request.goal)
    if request.stage is Stage.TRIAGE:
        return render_prompt_asset(
            "system_triage",
            goal=request.goal,
            schema=json.dumps(_SCHEMAS[Stage.TRIAGE], ensure_ascii=False, indent=2),
        )
    if request.stage is Stage.PLANNING:
        raw_habits = request.context.get("habits")
        habits = (
            [str(item) for item in raw_habits if str(item).strip()]
            if isinstance(raw_habits, (list, tuple))
            else []
        )
        return render_prompt_asset(
            "system_planning",
            goal=request.goal,
            classification=(
                request.classification.value if request.classification else ""
            ),
            all_active_habits="\n\n".join(habits) if habits else "[]",
            available_execution_tools=json.dumps(
                request.context.get("available_execution_tools") or [],
                ensure_ascii=False,
                indent=2,
            ),
            available_sub_agent_profiles=json.dumps(
                request.context.get("available_sub_agent_profiles") or [],
                ensure_ascii=False,
            ),
            schema=json.dumps(_SCHEMAS[Stage.PLANNING], ensure_ascii=False, indent=2),
        )
    if request.stage is Stage.REPLANNING:
        return render_prompt_asset(
            "system_replanning",
            goal=request.goal,
            classification=(
                request.classification.value if request.classification else ""
            ),
            active_plan=json.dumps(
                request.context.get("active_plan") or {},
                ensure_ascii=False,
                indent=2,
            ),
            plan_edit_history=json.dumps(
                request.context.get("plan_edit_history") or [],
                ensure_ascii=False,
                indent=2,
            ),
            workflow_state_and_evidence=json.dumps(
                request.context.get("workflow_state_and_evidence") or {},
                ensure_ascii=False,
                indent=2,
            ),
            available_execution_tools=json.dumps(
                request.context.get("available_execution_tools") or [],
                ensure_ascii=False,
                indent=2,
            ),
            available_sub_agent_profiles=json.dumps(
                request.context.get("available_sub_agent_profiles") or [],
                ensure_ascii=False,
            ),
            schema=json.dumps(_SCHEMAS[Stage.REPLANNING], ensure_ascii=False, indent=2),
        )
    if request.stage is Stage.EXECUTION:
        if not request.role.startswith("sub_agent:"):
            # Primary Execution owns a natural-language user response.  Its
            # complete contract, plan, tools, Persona, and goal are installed
            # as one isolated system prompt by the provider adapter.
            return request.goal
        active_plan = request.context.get("active_plan")
        sub_agent_results = request.context.get("sub_agent_results")
        active_plan_section = ""
        if active_plan is not None:
            active_plan_section = "\n\nHER v2 execution plan:\n" + json.dumps(
                active_plan, ensure_ascii=False, indent=2
            )
        sub_agent_results_section = ""
        if sub_agent_results:
            sub_agent_results_section = (
                "\n\nCompleted delegated execution inputs:\n"
                + json.dumps(sub_agent_results, ensure_ascii=False, indent=2)
            )
        assignment_section = ""
        if request.role.startswith("sub_agent:"):
            raw_definition = request.context.get("assignment_definition")
            definition = (
                dict(raw_definition)
                if isinstance(raw_definition, Mapping)
                else {
                    "id": request.context.get("assignment_id"),
                    "task": request.context.get("assigned_task"),
                    "profile": request.context.get("profile"),
                    "tools": request.context.get("delegated_tools") or [],
                    "attachment_ids": (
                        request.context.get("authorized_attachment_ids") or []
                    ),
                    "allow_side_effects": request.allow_side_effects,
                }
            )
            authority = request.context.get("authority")
            envelope: dict[str, Any] = {
                "plan_id": request.plan_id,
                "immutable_classification": (
                    request.classification.value if request.classification else None
                ),
                "assignment": definition,
                "delegated_capabilities": {
                    "tools": request.context.get("delegated_tools") or [],
                    "attachment_ids": (
                        request.context.get("authorized_attachment_ids") or []
                    ),
                    "attachment_manifest": (
                        request.context.get("authorized_attachment_manifest") or []
                    ),
                    "allow_tools": request.allow_tools,
                    "allow_side_effects": request.allow_side_effects,
                },
                "authority": (
                    dict(authority)
                    if isinstance(authority, Mapping)
                    else {
                        "scope": "bounded_execution_only",
                        "may_replan": False,
                        "may_contact_user": False,
                        "may_finalise": False,
                        "may_create_subagents": False,
                    }
                ),
            }
            continuation = request.context.get("replan_continuation")
            if isinstance(continuation, Mapping) and continuation:
                envelope["replan_continuation"] = dict(continuation)
                envelope["continuation_rules"] = dict(
                    request.context.get("continuation_rules")
                    if isinstance(request.context.get("continuation_rules"), Mapping)
                    else {}
                )
            assignment_section = (
                "\n\nBounded assignment and authority envelope:\n"
                + json.dumps(envelope, ensure_ascii=False, indent=2)
            )
        return render_prompt_asset(
            "execution_request",
            goal=request.goal,
            active_plan_section=active_plan_section,
            sub_agent_results_section=sub_agent_results_section,
            assignment_section=assignment_section,
            schema=json.dumps(_SCHEMAS[Stage.EXECUTION], ensure_ascii=False, indent=2),
        )
    if request.stage is Stage.FINALISATION:
        # The complete Finalisation contract and all evidence are rendered in
        # the isolated system prompt.  The raw goal is the backend's user turn.
        return request.goal
    context = {
        "turn_id": request.turn_id,
        "request_ref": request.request_ref,
        "stage": request.stage.value,
        "invocation_role": request.role,
        "attempt": request.attempt,
        "authoritative_user_goal": request.goal,
        "immutable_classification": (
            request.classification.value if request.classification else None
        ),
        "her_effort": request.effort.value,
        "active_plan_id": request.plan_id,
        "tools_authorised_for_this_stage": request.allow_tools,
        "external_side_effects_authorised_for_this_stage": request.allow_side_effects,
        "stage_context": dict(request.context),
    }
    reviewer_rule = ""
    if request.stage is Stage.REVIEW:
        reviewer_rule = (
            "You are an independent read-only assessor. You may call only the delegated "
            "read-only tools. Begin and end every evidence-backed assessment with "
            "workspace_inspect operation=snapshot. Cite only exact "
            "HASHI_EVIDENCE_RECEIPT values returned during this invocation. A tool start "
            "without a completed receipt is not evidence; a failed receipt can support "
            "only a failing (FAIL/FAILED) or INCONCLUSIVE outcome. If the before/after "
            "snapshot digests differ, "
            "return INCONCLUSIVE. Never contact the user, change the goal or "
            "classification, activate a plan, authorise side effects, mutate state, or "
            "write the final answer."
        )
    elif request.stage is Stage.VERIFICATION:
        reviewer_rule = (
            "You are an independent assessor. You may call only the delegated tools. "
            "verification_run is the sole mutating-capable tool and may run only a "
            "configured recipe or direct argv validation command in the authoritative "
            "current workspace; never perform remediation. It inherits HASHI's process "
            "identity, filesystem access, environment, HOME, and network, and HASHI "
            "automatically grows its timeout from cumulative Execution duration. Begin and "
            "end every evidence-backed assessment with workspace_inspect "
            "operation=snapshot. Cite only exact "
            "HASHI_EVIDENCE_RECEIPT values returned during this invocation. A tool start "
            "without a completed receipt is not evidence; a failed receipt can support "
            "only FAILED or INCONCLUSIVE. If the before/after snapshot digests differ, "
            "return INCONCLUSIVE. Never contact the user, change the goal or "
            "classification, activate a plan, authorise any other side effect, or write "
            "the final answer."
        )
    sub_agent_rule = (
        "You are a bounded sub-agent. Execute only the assigned task. You may not change "
        "the user goal, classification, or active plan; request Replanning; contact the "
        "user; create sub-agents; or author a final user response. Return evidence to the "
        "primary orchestrator only."
        if request.role.startswith("sub_agent:")
        else ""
    )
    output_schema = _SCHEMAS[request.stage]
    return render_prompt_asset(
        "stage_request",
        reviewer_rule=reviewer_rule,
        sub_agent_rule=sub_agent_rule,
        context=json.dumps(context, ensure_ascii=False, sort_keys=True),
        schema=json.dumps(output_schema, ensure_ascii=False, sort_keys=True),
    )


_SYSTEM_PROMPT_ASSETS = {
    Stage.EXECUTION: "system_execution",
    Stage.MEDITATION: "system_meditation",
    Stage.DREAM: "system_dream",
    Stage.JSON_REPAIR: "system_json_repair",
}

_COMPLETE_SYSTEM_PROMPT_STAGES = frozenset(
    {
        Stage.TRIAGE,
        Stage.PLANNING,
        Stage.REPLANNING,
        Stage.REVIEW,
        Stage.FINALISATION,
    }
)


def uses_complete_system_prompt(stage: Stage) -> bool:
    """Return whether one rendered asset is the stage's complete system prompt."""

    return stage in _COMPLETE_SYSTEM_PROMPT_STAGES


def render_internal_stage_system_prompt(request: StageRequest) -> str | None:
    """Render the tool/authority envelope for one internal HER v2 role."""

    if request.role.startswith("sub_agent:"):
        return load_prompt_asset("system_sub_agent")
    if (
        request.stage is Stage.JSON_REPAIR
        and request.context.get("repair_mode") == "verification_report"
    ):
        return load_prompt_asset("system_verification_report_repair")
    if request.stage is Stage.DREAM and request.context.get("dream_role") == "report":
        return load_prompt_asset("system_dream_report")
    if request.stage in {Stage.EXECUTION, Stage.REVIEW, Stage.FINALISATION}:
        # Primary Execution, Review, and Finalisation are rendered dynamically
        # after the adapter has assembled their invocation-specific inputs.
        return None
    if uses_complete_system_prompt(request.stage):
        return render_stage_prompt(request)
    asset_name = _SYSTEM_PROMPT_ASSETS.get(request.stage)
    return load_prompt_asset(asset_name) if asset_name else None


def _persona_guidance(*, guidance: str, display_name: str, usable: bool) -> str:
    if usable:
        return guidance
    return f"Agent display name: {display_name}. Use a polite tone and address the user as 您."


def render_finalisation_system_prompt(
    *,
    goal: str,
    draft_response: str,
    reviewer_findings: Mapping[str, Any] | None,
    completion_evidence: Mapping[str, Any],
    guidance: str,
    display_name: str,
    usable: bool,
    persona_block_begin: str,
    persona_block_end: str,
) -> str:
    return render_prompt_asset(
        "system_finalisation",
        goal=goal,
        draft_response=(
            str(draft_response).strip() or "No execution draft response was produced."
        ),
        reviewer_findings=(
            json.dumps(reviewer_findings, ensure_ascii=False, indent=2)
            if reviewer_findings is not None
            else "No Review findings were supplied."
        ),
        completion_evidence=json.dumps(
            dict(completion_evidence), ensure_ascii=False, indent=2
        ),
        persona_block_begin=persona_block_begin,
        persona_guidance=_persona_guidance(
            guidance=guidance, display_name=display_name, usable=usable
        ),
        persona_block_end=persona_block_end,
    )


def render_immediate_response_system_prompt(
    *,
    goal: str,
    guidance: str,
    display_name: str,
    usable: bool,
    persona_block_begin: str,
    persona_block_end: str,
) -> str:
    return render_prompt_asset(
        "system_immediate_response",
        goal=_immediate_response_goal(goal),
        persona_block_begin=persona_block_begin,
        persona_guidance=_persona_guidance(
            guidance=guidance, display_name=display_name, usable=usable
        ),
        persona_block_end=persona_block_end,
    )


def render_execution_system_prompt(
    *,
    goal: str,
    active_plan: Mapping[str, Any] | None,
    delegated_execution: Mapping[str, Any] | None,
    tool_catalogue: Sequence[Mapping[str, Any]],
    guidance: str,
    display_name: str,
    usable: bool,
    persona_block_begin: str,
    persona_block_end: str,
) -> str:
    """Render the complete primary Execution contract as one system prompt."""

    return render_prompt_asset(
        "system_execution",
        active_plan=(
            json.dumps(active_plan, ensure_ascii=False, indent=2)
            if active_plan is not None
            else "No active plan was supplied."
        ),
        delegated_execution=(
            json.dumps(dict(delegated_execution), ensure_ascii=False, indent=2)
            if delegated_execution is not None
            else "No delegated execution batch was attached."
        ),
        tool_catalogue=(
            json.dumps(list(tool_catalogue), ensure_ascii=False, indent=2)
            if tool_catalogue
            else "No tools are available for this invocation."
        ),
        persona_block_begin=persona_block_begin,
        persona_guidance=_persona_guidance(
            guidance=guidance, display_name=display_name, usable=usable
        ),
        persona_block_end=persona_block_end,
        goal=goal,
    )


def render_review_system_prompt(
    *,
    goal: str,
    active_plan: Mapping[str, Any] | None,
    draft_response: str,
    available_review_tools: Sequence[Mapping[str, Any]],
) -> str:
    """Render the complete independent Review contract as one system prompt."""

    return render_prompt_asset(
        "system_review",
        available_review_tools=(
            json.dumps(list(available_review_tools), ensure_ascii=False, indent=2)
            if available_review_tools
            else "No review tools are available for this invocation."
        ),
        goal=goal,
        active_plan=(
            json.dumps(active_plan, ensure_ascii=False, indent=2)
            if active_plan is not None
            else "No active plan was supplied."
        ),
        draft_response=(
            str(draft_response).strip()
            or "The execution agent returned no draft response."
        ),
    )


def render_persona_commentary_system_prompt(
    *,
    persona_guidance: str,
    persona_block_begin: str,
    persona_block_end: str,
) -> str:
    return (
        render_prompt_asset(
            "system_persona_commentary",
            persona_block_begin=persona_block_begin,
            persona_guidance=persona_guidance,
            persona_block_end=persona_block_end,
        )
        + "\n"
    )
