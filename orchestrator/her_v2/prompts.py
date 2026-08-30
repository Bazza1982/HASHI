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
        "real_goal": (
            "concise resolved operational goal; null only when "
            "CONFIRMATION_REQUIRED because the goal cannot be resolved reliably"
        ),
        "selected_strategy_cards": [
            "exact Strategy Card ID selected from the supplied Playbook"
        ],
        "relevant_habits": [
            "exact unchanged entry selected from the supplied habit_catalogue"
        ],
        "execution_brief": {
            "strategy": "overall strategic approach; empty for non-work paths",
            "stages": ["major strategic stage"],
            "dependencies": ["important sequence, prerequisite, or parallelism"],
            "verification": ["verification approach or evidence standard"],
            "success_criteria": ["observable completion condition"],
            "replan_conditions": ["condition that should trigger replanning"],
        },
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
    Stage.MEDITATION: {"actions": []},
    Stage.DREAM: {"groups": []},
}


def _compact_tool_catalogue(
    items: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Keep model guidance once; function parameters remain in the API schema."""

    compact: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, Mapping):
            continue
        function = item.get("function")
        source = function if isinstance(function, Mapping) else item
        name = str(source.get("name") or "").strip()
        if not name:
            continue
        entry: dict[str, Any] = {
            "name": name,
            "description": str(source.get("description") or "").strip(),
        }
        if "hashi_read_only" in item:
            entry["hashi_read_only"] = bool(item.get("hashi_read_only"))
        compact.append(entry)
    return compact


_JSON_REPAIR_STAGES = frozenset(
    {
        Stage.TRIAGE,
        Stage.PLANNING,
        Stage.REPLANNING,
        Stage.REVIEW,
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


def extract_authoritative_current_request(goal: str) -> str:
    """Return only the current user request from a bridge-managed PCM prompt.

    A text-to-audio transport adaptation must never speak the Persona, prior
    conversation, or other PCM sections.  Prompts without the typed marker are
    already request-scoped and therefore remain unchanged.
    """

    rendered = str(goal or "")
    marker_index = rendered.find(_BRIDGE_CURRENT_REQUEST_MARKER)
    if marker_index < 0:
        return rendered.strip()

    current_lines: list[str] = []
    remainder = rendered[
        marker_index + len(_BRIDGE_CURRENT_REQUEST_MARKER) :
    ]
    for line in remainder.splitlines():
        stripped = line.strip()
        if stripped.startswith("--- ") and stripped.endswith(" ---"):
            break
        current_lines.append(line)
    return "\n".join(current_lines).strip()


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
    if request.stage is Stage.MEDITATION:
        # All Meditation instructions live in its isolated system prompt.
        # The provider-facing user turn contains data only.
        return meditation_input if isinstance(meditation_input, str) else request.goal
    dream_input = request.context.get("dream_input")
    if request.stage is Stage.DREAM:
        # Dream maintenance and Persona reporting use separate isolated system
        # contracts.  Their provider-facing user turns contain data only.
        return dream_input if isinstance(dream_input, str) else request.goal
    if request.stage is Stage.IMMEDIATE_RESPONSE:
        # The complete Immediate Response contract, including this same filtered
        # goal, is installed as its isolated system prompt.  Keep a non-empty
        # user turn for provider compatibility without introducing a second
        # prompt asset or a conflicting structured-output instruction.
        return _immediate_response_goal(request.goal)
    if request.stage is Stage.DIRECT:
        # Direct's complete contract, tools, advisory catalogues, Persona, and
        # authoritative goal live in one isolated system prompt.
        return request.goal
    if request.stage is Stage.TRIAGE:
        raw_catalogue = request.context.get("habit_catalogue")
        habit_catalogue = (
            [str(item) for item in raw_catalogue if str(item).strip()]
            if isinstance(raw_catalogue, (list, tuple))
            else []
        )
        return render_prompt_asset(
            "system_strategy",
            goal=request.goal,
            strategy_cards=json.dumps(
                request.context.get("strategy_cards") or {},
                ensure_ascii=False,
                indent=2,
            ),
            habit_catalogue=json.dumps(
                habit_catalogue, ensure_ascii=False, indent=2
            ),
            execution_capabilities=json.dumps(
                request.context.get("execution_capabilities") or {},
                ensure_ascii=False,
                indent=2,
            ),
            request_resources=json.dumps(
                request.context.get("request_resources") or {},
                ensure_ascii=False,
                indent=2,
            ),
            schema_v3=json.dumps(
                _SCHEMAS[Stage.TRIAGE], ensure_ascii=False, indent=2
            ),
        )
    if request.stage is Stage.PLANNING:
        raw_habits = request.context.get("relevant_habits")
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
            relevant_habits=json.dumps(habits, ensure_ascii=False, indent=2),
            available_execution_tools=json.dumps(
                _compact_tool_catalogue(
                    request.context.get("available_execution_tools") or []
                ),
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
            relevant_habits=json.dumps(
                request.context.get("relevant_habits") or [],
                ensure_ascii=False,
                indent=2,
            ),
            available_execution_tools=json.dumps(
                _compact_tool_catalogue(
                    request.context.get("available_execution_tools") or []
                ),
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
        # The complete bounded contract is installed from system_sub_agent.txt.
        # Keep the user turn as data only so there is a single prompt document
        # defining sub-agent behaviour.
        return request.goal
    if request.stage is Stage.REVIEW:
        # system_review.txt owns the complete Reviewer contract and every
        # invocation-specific Review input.  The user turn is data only.
        return request.goal
    if request.stage is Stage.FINALISATION:
        # The complete Finalisation contract and all evidence are rendered in
        # the isolated system prompt.  The raw goal is the backend's user turn.
        return request.goal
    raise ValueError(f"unsupported HER v2 stage: {request.stage!r}")


_SYSTEM_PROMPT_ASSETS = {
    Stage.EXECUTION: "system_execution",
    Stage.MEDITATION: "system_meditation",
    Stage.DREAM: "system_dream",
    Stage.JSON_REPAIR: "system_json_repair",
}

_COMPLETE_SYSTEM_PROMPT_STAGES = frozenset(
    {
        Stage.DIRECT,
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
        return render_prompt_asset(
            "system_sub_agent",
            real_goal=str(request.context.get("real_goal") or request.goal),
            relevant_habits=json.dumps(
                request.context.get("relevant_habits") or [],
                ensure_ascii=False,
                indent=2,
            ),
            active_plan=json.dumps(
                request.context.get("active_plan") or {},
                ensure_ascii=False,
                indent=2,
            ),
            sub_agent_results=json.dumps(
                request.context.get("sub_agent_results") or [],
                ensure_ascii=False,
                indent=2,
            ),
            assignment=json.dumps(envelope, ensure_ascii=False, indent=2),
            schema=json.dumps(_SCHEMAS[Stage.EXECUTION], ensure_ascii=False, indent=2),
        )
    if request.stage is Stage.DREAM and request.context.get("dream_role") == "report":
        return load_prompt_asset("system_dream_report")
    if request.stage in {
        Stage.DIRECT,
        Stage.EXECUTION,
        Stage.REVIEW,
        Stage.FINALISATION,
    }:
        # Direct, primary Execution, Review, and Finalisation are rendered
        # dynamically after the adapter has assembled invocation-specific inputs.
        return None
    if uses_complete_system_prompt(request.stage):
        return render_stage_prompt(request)
    asset_name = _SYSTEM_PROMPT_ASSETS.get(request.stage)
    return load_prompt_asset(asset_name) if asset_name else None


def _persona_guidance(*, guidance: str, display_name: str, usable: bool) -> str:
    if usable:
        return guidance
    return (
        f"Agent display name: {display_name}. "
        "Use the configured default language and a respectful tone."
    )


def render_finalisation_system_prompt(
    *,
    goal: str,
    relevant_habits: Sequence[str],
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
        relevant_habits=json.dumps(
            list(relevant_habits), ensure_ascii=False, indent=2
        ),
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


def render_direct_system_prompt(
    *,
    goal: str,
    habit_catalogue: Sequence[str],
    skills_catalogue: Sequence[Mapping[str, Any]],
    tool_catalogue: Sequence[Mapping[str, Any]],
    guidance: str,
    display_name: str,
    usable: bool,
    persona_block_begin: str,
    persona_block_end: str,
) -> str:
    """Render the complete zero-orchestration Direct contract."""

    return render_prompt_asset(
        "system_direct",
        goal=goal,
        habit_catalogue=(
            json.dumps(list(habit_catalogue), ensure_ascii=False, indent=2)
            if habit_catalogue
            else "[]"
        ),
        skills_catalogue=(
            json.dumps(list(skills_catalogue), ensure_ascii=False, indent=2)
            if skills_catalogue
            else "[]"
        ),
        tool_catalogue=(
            json.dumps(
                _compact_tool_catalogue(tool_catalogue),
                ensure_ascii=False,
                indent=2,
            )
            if tool_catalogue
            else "No tools are available for this invocation."
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
    relevant_habits: Sequence[str],
    active_plan: Mapping[str, Any] | None,
    delegated_execution: Mapping[str, Any] | None,
    strategy_handoff: Mapping[str, Any] | None,
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
        relevant_habits=json.dumps(
            list(relevant_habits), ensure_ascii=False, indent=2
        ),
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
        strategy_handoff=(
            json.dumps(dict(strategy_handoff), ensure_ascii=False, indent=2)
            if strategy_handoff is not None
            else "No Strategy handoff was supplied for this execution path."
        ),
        tool_catalogue=(
            json.dumps(
                _compact_tool_catalogue(tool_catalogue),
                ensure_ascii=False,
                indent=2,
            )
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
    relevant_habits: Sequence[str],
    active_plan_id: str | None,
    active_plan: Mapping[str, Any] | None,
    draft_response: str,
    execution_record: Mapping[str, Any] | None,
    evidence_refs: Sequence[str],
    review_kind: str,
    findings_to_close: Sequence[str],
    available_review_tools: Sequence[Mapping[str, Any]],
) -> str:
    """Render the complete independent Review contract as one system prompt."""

    return render_prompt_asset(
        "system_review",
        available_review_tools=(
            json.dumps(
                _compact_tool_catalogue(available_review_tools),
                ensure_ascii=False,
                indent=2,
            )
            if available_review_tools
            else "No review tools are available for this invocation."
        ),
        review_context=json.dumps(
            {
                "review_kind": str(review_kind or "").strip() or "independent",
                "active_plan_id": str(active_plan_id or "").strip() or None,
                "findings_to_close": [
                    str(item) for item in findings_to_close if str(item).strip()
                ],
            },
            ensure_ascii=False,
            indent=2,
        ),
        goal=goal,
        draft_response=(
            str(draft_response).strip()
            or "The execution agent returned no draft response."
        ),
        execution_evidence=json.dumps(
            {
                "execution_record": (
                    dict(execution_record) if execution_record is not None else None
                ),
                "evidence_refs": [
                    str(item) for item in evidence_refs if str(item).strip()
                ],
            },
            ensure_ascii=False,
            indent=2,
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
