from __future__ import annotations

import json
from pathlib import Path
from typing import Any


EXPECTED_PROVIDERS = {"official_deepseek", "openrouter"}
EXPECTED_MODES = ["fixed", "flex"]
EXPECTED_EFFORTS = ["low", "medium", "high", "xhigh", "max", "max+"]
EXPECTED_FLASH_MODELS = {
    "official_deepseek": "deepseek-v4-flash",
    "openrouter": "deepseek/deepseek-v4-flash",
}
EXPECTED_PRO_MODELS = {
    "official_deepseek": "deepseek-v4-pro",
    "openrouter": "deepseek/deepseek-v4-pro",
}


def _load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def validate_template(template_dir: Path) -> dict[str, Any]:
    findings: list[dict[str, str]] = []

    def require(condition: bool, code: str, message: str) -> None:
        if not condition:
            findings.append({"code": code, "message": message})

    required_files = (
        "README.md",
        "state.template.json",
        "campaign.template.json",
        "roles.template.json",
        "taskboard.template.json",
        "attempt.template.json",
        "liveness_nudge.template.md",
        "evidence.schema.md",
    )
    for name in required_files:
        require((template_dir / name).is_file(), "required_file", f"Missing {name}.")
    if findings:
        return {"ok": False, "finding_count": len(findings), "findings": findings}

    state = _load_json(template_dir / "state.template.json")
    campaign = _load_json(template_dir / "campaign.template.json")
    roles = _load_json(template_dir / "roles.template.json")
    tasks = _load_json(template_dir / "taskboard.template.json")
    attempt = _load_json(template_dir / "attempt.template.json")
    nudge = (template_dir / "liveness_nudge.template.md").read_text(encoding="utf-8")

    require(state.get("template") == "her_debug", "template_id", "Template ID must be her_debug.")
    require(state.get("owner_agent") == "lin_yueru", "controller_owner", "Lin Yueru must own the loop.")
    require(
        state.get("controller", {}).get("agent") == "lin_yueru",
        "controller_identity",
        "The controller must be Lin Yueru.",
    )
    require(state.get("worker", {}).get("agent") == "ajiao", "worker_identity", "Ajiao must be the worker.")
    require(state.get("scheduler_auto_advance") is False, "scheduler_authority", "Scheduler auto-advance must be off.")
    require(
        state.get("terminal_state_mapping") == {"PASSED": "completed", "BLOCKED_FUNDS": "blocked"},
        "terminal_state_mapping",
        "Terminal results must map to contract-compatible loop states.",
    )

    liveness = state.get("liveness", {})
    require(
        liveness.get("nudge_owner_agent") == "lin_yueru",
        "nudge_owner",
        "The nudge must belong to the controller, not Ajiao.",
    )
    require(
        liveness.get("nudge_template_path") == "superloops/templates/her_debug/liveness_nudge.template.md",
        "nudge_template_path",
        "Loop state must point to the controller nudge policy.",
    )
    require(
        liveness.get("observed_worker_agent") == "ajiao",
        "nudge_observed_worker",
        "The nudge must observe Ajiao's dispatch state.",
    )
    require(
        liveness.get("may_interrupt_running_worker") is False,
        "nudge_interrupt",
        "The nudge must not interrupt a running worker.",
    )
    require(
        liveness.get("may_continue_packets_for_in_progress_task") is True,
        "nudge_in_progress_continuation",
        "The nudge must continue packets owned by the current in-progress task.",
    )
    require(
        liveness.get("must_not_require_new_start_authority_for_in_progress_task_packet") is True,
        "nudge_packet_authority",
        "An in-progress task's next packet must not require fresh start authority.",
    )
    require(
        liveness.get("may_dispatch_duplicate_active_packet") is False,
        "nudge_duplicate",
        "The nudge must not duplicate an active packet.",
    )
    require(
        liveness.get("must_follow_up_nonterminal_failure") is True,
        "nudge_follow_up",
        "Every nonterminal failure must receive a follow-up.",
    )
    require(
        liveness.get("must_surface_stagnation") is True,
        "nudge_stagnation",
        "The nudge must surface rather than repeat a functional livelock.",
    )
    require(
        liveness.get("stagnation_observation_limit") == 3,
        "nudge_stagnation_limit",
        "The selected-packet stagnation limit must be three observations.",
    )
    require(liveness.get("max_nudges") == 0, "nudge_limit", "The controller nudge must be unlimited.")
    require(
        set(liveness.get("terminal_conditions", [])) == {"PASSED", "BLOCKED_FUNDS"},
        "nudge_terminal",
        "Only PASSED and BLOCKED_FUNDS may complete the nudge.",
    )

    policy = state.get("execution_policy", {})
    for key in (
        "allow_unlisted_live_provider",
        "allow_unlisted_live_model",
        "allow_cross_route_substitution",
        "allow_live_cell_sampling",
        "worker_failed_reply_is_terminal",
    ):
        require(policy.get(key) is False, "execution_policy", f"execution_policy.{key} must be false.")
    require(
        policy.get("stage_2_requires_stage_1_pass") is True,
        "stage_lock_policy",
        "Stage 2 must require a passed Stage 1 gate.",
    )

    dimensions = campaign.get("dimensions", {})
    providers = dimensions.get("providers", {})
    modes = dimensions.get("modes", [])
    efforts = dimensions.get("efforts", [])
    require(set(providers) == EXPECTED_PROVIDERS, "providers", "Only Official DeepSeek and OpenRouter are allowed.")
    require(modes == EXPECTED_MODES, "modes", "The mode dimension must be fixed then flex.")
    require(efforts == EXPECTED_EFFORTS, "efforts", "All six effort levels must be ordered and present.")
    expected_stage_cells = len(providers) * len(modes) * len(efforts)
    require(expected_stage_cells == 24, "stage_cell_math", "Each model stage must contain 24 core cells.")

    stages = {stage.get("stage_id"): stage for stage in campaign.get("stages", [])}
    stage_1 = stages.get("stage_1_flash", {})
    stage_2 = stages.get("stage_2_pro", {})
    require(
        stage_1.get("allowed_live_models") == EXPECTED_FLASH_MODELS,
        "stage_1_models",
        "Stage 1 must contain only the two Flash slugs.",
    )
    require(
        stage_2.get("allowed_live_models") == EXPECTED_PRO_MODELS,
        "stage_2_models",
        "Stage 2 must contain only the two Pro slugs.",
    )
    require(stage_1.get("expected_core_cells") == 24, "stage_1_count", "Stage 1 count must be 24.")
    require(stage_2.get("expected_core_cells") == 24, "stage_2_count", "Stage 2 count must be 24.")
    require(
        "stage_1_flash=passed" in stage_2.get("locked_until", []),
        "stage_2_lock",
        "Stage 2 must remain locked until Stage 1 is passed.",
    )

    counts = campaign.get("expected_counts", {})
    require(counts.get("total_core_cells") == 48, "total_cells", "The campaign must require all 48 core cells.")
    require(
        counts.get("total_core_scenario_groups") == 480,
        "scenario_count",
        "The campaign must require 480 core scenario groups.",
    )
    require(
        counts.get("total_presentation_runs") == 384,
        "presentation_count",
        "The campaign must require 384 presentation runs.",
    )

    fallback = campaign.get("fallback_policy", {})
    require(fallback and all(value is False for value in fallback.values()), "fallback", "Every fallback flag must be false.")
    funds = campaign.get("funds_policy", {})
    require(
        set(funds.get("required_live_routes", [])) == EXPECTED_PROVIDERS,
        "funds_routes",
        "Both live routes must be required.",
    )
    require(
        funds.get("terminal_on_confirmed_insufficient_funds_from_any_required_route") is True,
        "funds_terminal",
        "Confirmed funds exhaustion on either required route must be terminal.",
    )
    require(funds.get("terminal_status") == "BLOCKED_FUNDS", "funds_status", "Funds terminal status must be BLOCKED_FUNDS.")

    require(isinstance(tasks, list) and len(tasks) == 10, "taskboard_shape", "The taskboard must contain ten phase gates.")
    task_map = {task.get("task_id"): task for task in tasks if isinstance(task, dict)}
    require(len(task_map) == len(tasks), "task_ids", "Task IDs must be unique.")
    require(
        task_map.get("HD-007", {}).get("depends_on") == ["HD-006"],
        "pro_task_lock",
        "The first Pro task must depend directly on the Flash gate.",
    )
    for task_id, task in task_map.items():
        for dependency in task.get("depends_on", []):
            require(dependency in task_map, "task_dependency", f"{task_id} has unknown dependency {dependency}.")

    role_map = roles.get("roles", {})
    require(
        role_map.get("orchestrator", {}).get("default_agent") == "lin_yueru",
        "role_controller",
        "The orchestrator role must default to Lin Yueru.",
    )
    require(
        role_map.get("test_and_repair_worker", {}).get("default_agent") == "ajiao",
        "role_worker",
        "The worker role must default to Ajiao.",
    )

    dispatch = attempt.get("dispatch", {})
    require(dispatch.get("controller_agent") == "lin_yueru", "attempt_controller", "Attempt controller must be Lin Yueru.")
    require(dispatch.get("worker_agent") == "ajiao", "attempt_worker", "Attempt worker must be Ajiao.")
    require(
        attempt.get("controller_decision", {}).get("campaign_terminal") is False,
        "attempt_default_terminal",
        "A planned attempt must default to non-terminal.",
    )

    required_nudge_phrases = (
        "This idle nudge wakes `lin_yueru`",
        "Never transfer, recreate, or aim this nudge at Ajiao.",
        "If Ajiao is `running`, do not `/stop`",
        "A failed reply is not a campaign terminal result.",
        '"pending task" means a phase task whose taskboard status is `pending`.',
        "must continue the\npacket queue of the current `in_progress` task",
        "Do not set or retain `pending_non_nudge_start_authority`",
        "Moving only the next\n   check timestamp is forbidden.",
        "Never use another API or model as fallback.",
        "NUDGE_COMPLETE:{{NUDGE_ID}}",
    )
    for phrase in required_nudge_phrases:
        require(phrase in nudge, "nudge_contract_text", f"Nudge template is missing: {phrase}")

    return {
        "ok": not findings,
        "template": "her_debug",
        "stage_1_core_cells": 24,
        "stage_2_core_cells": 24,
        "total_core_cells": 48,
        "finding_count": len(findings),
        "findings": findings,
    }


def main() -> int:
    template_dir = Path(__file__).resolve().parent
    report = validate_template(template_dir)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
