from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


PHASES = ("structure", "prebuild", "preinstall", "round-close")


def _load_object(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"Expected JSON object: {path}")
    return value


def _resolve(loop_dir: Path, raw: Any, fallback: str) -> Path:
    value = str(raw or "").strip()
    if not value:
        return loop_dir / fallback
    candidate = Path(value).expanduser()
    if candidate.is_absolute():
        return candidate
    project_root = loop_dir
    while project_root.name != "superloops" and project_root.parent != project_root:
        project_root = project_root.parent
    if project_root.name == "superloops":
        return project_root.parent / candidate
    return loop_dir / candidate


def _nonempty(value: Any) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, dict)):
        return bool(value)
    return value is not None


def validate_round(loop_dir: Path, phase: str) -> dict[str, Any]:
    """Audit the four factual rules without authorising Build or Install.

    The structure, prebuild, and preinstall phases intentionally perform no
    candidate-readiness checks. They exist only for compatibility with older
    callers. The round-close audit prevents theoretical results from being
    recorded as installed results and verifies failure cleanup facts.
    """

    findings: list[dict[str, str]] = []

    def finding(code: str, message: str) -> None:
        findings.append({"severity": "error", "code": code, "message": message})

    state = _load_object(loop_dir / "state.json")
    round_path = _resolve(loop_dir, state.get("active_round_path"), "round.json")
    round_record = _load_object(round_path)

    if state.get("max_rounds") != 30:
        finding("max_rounds", "max_rounds must be exactly 30.")
    current_round = state.get("current_round")
    if not isinstance(current_round, int) or not 1 <= current_round <= 30:
        finding("current_round", "current_round must be an integer from 1 through 30.")
    if state.get("scheduler_auto_advance") is not False:
        finding("scheduler_auto_advance", "The scheduler must not edit task status.")

    policy = state.get("execution_policy") if isinstance(state.get("execution_policy"), dict) else {}
    expected_policy = {
        "validation_source": "installed_candidate_only",
        "failure_journal_update": "after_every_failure",
        "failed_candidate_cleanup": "uninstall_before_next_round",
        "environment_boundary": "candidate_only_preserve_user_environment_and_debug_runtime",
        "prebuild_checks": "advisory_non_blocking",
        "provider_credentials_required": False,
        "success_condition": "stable_setup_native_launchers_clean_user_complete_lifecycle",
    }
    for key, expected in expected_policy.items():
        if policy.get(key) != expected:
            finding("fact_policy", f"execution_policy.{key} must be {expected!r}.")

    liveness = state.get("liveness") if isinstance(state.get("liveness"), dict) else {}
    if liveness.get("mode") != "idle_nudge":
        finding("liveness_mode", "Liveness must use the idle nudge.")
    if liveness.get("may_mutate_task_status") is not False:
        finding("liveness_mutation", "The nudge must not edit task status.")
    if liveness.get("must_continue_until_terminal") is not True:
        finding("liveness_continuation", "The nudge must continue until a terminal result.")

    # Nothing before closeout is allowed to block Build or Install.
    if phase != "round-close":
        return {
            "ok": not findings,
            "mode": "non_blocking_structure_audit",
            "phase": phase,
            "loop_dir": str(loop_dir),
            "finding_count": len(findings),
            "findings": findings,
        }

    actual = (
        round_record.get("actual_validation")
        if isinstance(round_record.get("actual_validation"), dict)
        else {}
    )
    if actual.get("source_or_unpacked_substituted") is not False:
        finding("theoretical_substitution", "Source or unpacked results cannot replace installed validation.")

    boundary = (
        round_record.get("environment_boundary")
        if isinstance(round_record.get("environment_boundary"), dict)
        else {}
    )
    if boundary.get("user_environment_unchanged") is not True:
        finding("user_environment_boundary", "The unrelated user environment was not proved unchanged.")
    if boundary.get("original_debug_runtime_unchanged") is not True:
        finding("debug_runtime_boundary", "The original Debug Runtime was not proved unchanged.")

    outcome = round_record.get("outcome") if isinstance(round_record.get("outcome"), dict) else {}
    if outcome.get("status") == "stable_lifecycle_accepted":
        if actual.get("actual_install_attempted") is not True:
            finding("actual_install_missing", "A successful installed lifecycle requires a real installation attempt.")
        if actual.get("install_mode") != "human_gui_usecomputer":
            finding("actual_install_mode", "The installed lifecycle must use human_gui_usecomputer.")
        if actual.get("validation_source") != "installed_msi":
            finding("installed_source_missing", "The validation source must be the installed MSI.")
        if actual.get("aptenra_shortcut_launch_attempted") is not True:
            finding("aptenra_launch_missing", "The installed Aptenra shortcut launch was not attempted.")
        if actual.get("workbench_shortcut_launch_attempted") is not True:
            finding("workbench_launch_missing", "The installed Workbench shortcut launch was not attempted.")
        if actual.get("aptenra_user_visible_launch_result") != "success":
            finding("aptenra_launch_not_successful", "Acceptance requires visible installed Aptenra launch.")
        if actual.get("workbench_user_visible_launch_result") != "success":
            finding("workbench_launch_not_successful", "Acceptance requires visible installed Workbench launch.")
        if actual.get("basic_functions_result") != "success":
            finding("basic_functions_not_successful", "Acceptance requires the recorded basic-function test.")
        if actual.get("baseline_agent_count") != 5:
            finding("baseline_agents_missing", "Acceptance requires five baseline agents.")
        if actual.get("baseline_session_count") != 5:
            finding("baseline_sessions_missing", "Acceptance requires five baseline sessions.")
        if actual.get("ordered_stop_before_repair") != "success":
            finding("pre_repair_stop_missing", "Acceptance requires ordered Stop before Repair.")
        if actual.get("repair_attempted") is not True or actual.get("repair_result") != "success":
            finding("repair_missing", "Acceptance requires successful visible Repair.")
        if actual.get("post_repair_dual_launch_result") != "success":
            finding("post_repair_launch_missing", "Acceptance requires post-Repair installed dual launch.")
        if actual.get("ordered_stop_before_uninstall") != "success":
            finding("pre_uninstall_stop_missing", "Acceptance requires ordered Stop before Uninstall.")
        if actual.get("uninstall_attempted") is not True or actual.get("uninstall_result") != "success":
            finding("uninstall_missing", "Acceptance requires successful visible Uninstall.")
        if actual.get("final_product_state") != -1:
            finding("final_product_state", "Acceptance requires final ProductState -1.")
        if actual.get("final_cleanup_result") != "success":
            finding("final_cleanup_missing", "Acceptance requires zero-residue cleanup and restoration.")
    else:
        failure = (
            round_record.get("failure_handling")
            if isinstance(round_record.get("failure_handling"), dict)
            else {}
        )
        if failure.get("candidate_failed") is not True:
            finding("failure_not_classified", "A non-successful installed round must classify the candidate as failed.")
        if failure.get("journal_updated") is not True or not _nonempty(failure.get("journal_entry")):
            finding("failure_journal_missing", "Every failure must update the Failure Journal.")
        if actual.get("actual_install_attempted") is True:
            if failure.get("uninstall_completed") is not True:
                finding("failed_candidate_uninstall_missing", "The failed installed candidate must be uninstalled.")
            if failure.get("cleanup_passed") is not True:
                finding("failed_candidate_cleanup_missing", "Failed-candidate cleanup must be recorded.")
        elif not _nonempty(outcome.get("candidate_disposition")):
            finding(
                "never_installed_disposition_missing",
                "A pre-install failure must record that there was no installation to remove.",
            )

    return {
        "ok": not findings,
        "mode": "post_fact_audit",
        "phase": phase,
        "loop_dir": str(loop_dir),
        "finding_count": len(findings),
        "findings": findings,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit an Aptenra packaging fast-loop record.")
    parser.add_argument("--loop-dir", type=Path, required=True)
    parser.add_argument("--phase", choices=PHASES, default="structure")
    args = parser.parse_args()
    report = validate_round(args.loop_dir.resolve(), args.phase)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
