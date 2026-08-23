"""Provider-neutral stage envelopes for HER v2 model roles."""

from __future__ import annotations

import json

from .models import Stage, StageRequest
from .prompt_catalog import load_prompt_asset, render_prompt_asset

_SCHEMAS = {
    Stage.IMMEDIATE_RESPONSE: {
        "message": "direct response or short receipt acknowledgement"
    },
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
        "checkpoint_policy": (
            "STANDARD | HIGH_RISK for SIMPLE_TASK, COMPLEX_TASK, or "
            "HIGH_VOLUME_TASK; null otherwise"
        ),
        "checkpoint_reason": (
            "required non-empty risk reason for HIGH_RISK; null otherwise"
        ),
    },
    Stage.PLANNING: {
        "plan": ["ordered, concrete action"],
        "success_criteria": ["observable criterion"],
        "parallel_groups": [],
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
        "plan": ["replacement action based on current evidence"],
        "success_criteria": ["observable criterion"],
        "changed_because": "material evidence that invalidated the prior approach",
        "commentary": (
            "optional concise neutral user-facing update based only on this "
            "completed stage result; omit when no useful update exists"
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
    Stage.CHECKPOINT: {
        "decision": "CONTINUE | USER_INPUT_REQUIRED | HALT",
        "summary": "concise evidence-based checkpoint assessment",
        "question": (
            "one concrete question required only for USER_INPUT_REQUIRED; "
            "null otherwise"
        ),
    },
    Stage.REVIEW: {
        "outcome": "PASS | CONDITIONAL_PASS | FAIL | INCONCLUSIVE | UNAVAILABLE",
        "summary": "independent evidence-based review",
        "findings": [],
        "evidence_refs": [
            "exact HASHI_EVIDENCE_RECEIPT values from this Review invocation"
        ],
        "commentary": (
            "optional concise neutral user-facing update based only on this "
            "completed stage result; omit when no useful update exists"
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
    Stage.FINALISATION: {
        "execution_result": {
            "disposition": (
                "COMPLETED | COMPLETED_WITH_LIMITATIONS | FAILED | USER_INPUT_REQUIRED"
            ),
            "summary": "canonical truthful execution result",
            "work_performed": ["concrete action actually performed"],
            "verification": ["check actually run and its result"],
            "evidence_refs": [],
            "limitations": [],
            "remaining_work": [],
            "clarification": "required only for USER_INPUT_REQUIRED",
        },
        "final_message": (
            "final user-facing response rendered with the supplied Persona"
        ),
    },
    Stage.MEDITATION: {"actions": []},
    Stage.DREAM: {"groups": []},
}


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
    maintenance_prompt = request.context.get("maintenance_prompt")
    if request.stage in {Stage.MEDITATION, Stage.DREAM} and isinstance(
        maintenance_prompt, str
    ):
        return render_prompt_asset(
            "background_maintenance", maintenance_prompt=maintenance_prompt
        )
    if request.stage is Stage.IMMEDIATE_RESPONSE:
        return render_prompt_asset(
            "immediate_response_request",
            goal=_immediate_response_goal(request.goal),
        )
    if request.stage is Stage.TRIAGE:
        prompt = render_prompt_asset(
            "triage_request",
            goal=request.goal,
            schema=json.dumps(_SCHEMAS[Stage.TRIAGE], ensure_ascii=False, indent=2),
        )
        previous_error = request.context.get("previous_structure_error")
        if not isinstance(previous_error, dict):
            return prompt
        retry_feedback = {
            "attempt": previous_error.get("attempt"),
            "error": previous_error.get("error"),
        }
        return render_prompt_asset(
            "triage_retry",
            prompt=prompt,
            retry_feedback=json.dumps(
                retry_feedback, ensure_ascii=False, sort_keys=True
            ),
        )
    if request.stage is Stage.EXECUTION:
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
            assignment_keys = [
                "assignment_id",
                "assigned_task",
                "delegated_tools",
                "authorized_attachment_ids",
            ]
            if "authorized_attachment_manifest" in request.context:
                assignment_keys.append("authorized_attachment_manifest")
            assignment = {
                key: request.context.get(key) for key in assignment_keys
            }
            assignment_section = "\n\nBounded assignment:\n" + json.dumps(
                assignment, ensure_ascii=False, indent=2
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
        return render_prompt_asset(
            "finalisation_request",
            goal=request.goal,
            context=json.dumps(dict(request.context), ensure_ascii=False, indent=2),
            schema=json.dumps(
                _SCHEMAS[Stage.FINALISATION], ensure_ascii=False, indent=2
            ),
        )
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
    Stage.TRIAGE: "system_triage",
    Stage.PLANNING: "system_planning",
    Stage.EXECUTION: "system_execution",
    Stage.CHECKPOINT: "system_checkpoint",
    Stage.REPLANNING: "system_replanning",
    Stage.REVIEW: "system_review",
    Stage.VERIFICATION: "system_verification",
    Stage.MEDITATION: "system_meditation",
    Stage.DREAM: "system_dream",
}


def render_internal_stage_system_prompt(request: StageRequest) -> str | None:
    """Render the tool/authority envelope for one internal HER v2 role."""

    if request.role.startswith("sub_agent:"):
        return load_prompt_asset("system_sub_agent")
    asset_name = _SYSTEM_PROMPT_ASSETS.get(request.stage)
    return load_prompt_asset(asset_name) if asset_name else None


def _persona_guidance(*, guidance: str, display_name: str, usable: bool) -> str:
    if usable:
        return guidance
    return f"Agent display name: {display_name}. Use a polite tone and address the user as 您."


def render_finalisation_system_prompt(
    *,
    guidance: str,
    display_name: str,
    usable: bool,
    persona_block_begin: str,
    persona_block_end: str,
) -> str:
    return render_prompt_asset(
        "system_finalisation",
        persona_block_begin=persona_block_begin,
        persona_guidance=_persona_guidance(
            guidance=guidance, display_name=display_name, usable=usable
        ),
        persona_block_end=persona_block_end,
    )


def render_immediate_response_system_prompt(
    *,
    guidance: str,
    display_name: str,
    usable: bool,
    persona_block_begin: str,
    persona_block_end: str,
) -> str:
    return render_prompt_asset(
        "system_immediate_response",
        persona_block_begin=persona_block_begin,
        persona_guidance=_persona_guidance(
            guidance=guidance, display_name=display_name, usable=usable
        ),
        persona_block_end=persona_block_end,
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


def render_persona_required_message_system_prompt(
    *,
    message_kind: str,
    kind_rule: str,
    persona_guidance: str,
    persona_block_begin: str,
    persona_block_end: str,
) -> str:
    return (
        render_prompt_asset(
            "system_persona_required_message",
            message_kind=message_kind,
            kind_rule=kind_rule,
            persona_block_begin=persona_block_begin,
            persona_guidance=persona_guidance,
            persona_block_end=persona_block_end,
        )
        + "\n"
    )
