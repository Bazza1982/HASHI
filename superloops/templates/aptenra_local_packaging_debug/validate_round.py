from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


EXPECTED_PFJ_IDS = [f"PFJ-{index:03d}" for index in range(1, 37)]
EXPECTED_QUESTION = (
    "What mistake did I make last time, and how will I avoid it in the most straightforward way this round?"
)
PHASES = {"structure": 0, "prebuild": 1, "preinstall": 2, "round-close": 3}
BLOCK_STAGE = {"implementation": 1, "build": 1, "install": 2, "round_close": 3}


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
    findings: list[dict[str, str]] = []

    def error(code: str, message: str) -> None:
        findings.append({"severity": "error", "code": code, "message": message})

    state = _load_object(loop_dir / "state.json")
    registry_path = _resolve(
        loop_dir,
        state.get("known_failure_registry_path"),
        "known_failure_registry.json",
    )
    round_path = _resolve(loop_dir, state.get("active_round_path"), "round.json")
    registry = _load_object(registry_path)
    round_record = _load_object(round_path)

    if state.get("max_rounds") != 30:
        error("max_rounds", "max_rounds must be exactly 30.")
    current_round = state.get("current_round")
    if not isinstance(current_round, int) or not 1 <= current_round <= 30:
        error("current_round", "current_round must be an integer from 1 through 30.")
    if state.get("scheduler_auto_advance") is not False:
        error("scheduler_auto_advance", "Background scheduler auto-advance must be false.")

    policy = state.get("execution_policy") if isinstance(state.get("execution_policy"), dict) else {}
    if policy.get("mandatory_round_question") != EXPECTED_QUESTION:
        error("mandatory_question", "The mandatory last-mistake question is missing or altered.")
    if policy.get("known_failure_recurrence") != "immediate_block":
        error("recurrence_policy", "Known failure recurrence must immediately block.")
    if policy.get("actual_install_required_for_candidate_validation") is not True:
        error("actual_install_policy", "Actual installation must be required for candidate validation.")
    if policy.get("failed_candidate_uninstall_before_next_round") is not True:
        error("uninstall_policy", "Failed candidate Uninstall must be required before the next round.")

    liveness = state.get("liveness") if isinstance(state.get("liveness"), dict) else {}
    if liveness.get("mode") != "idle_nudge" or liveness.get("may_mutate_task_status") is not False:
        error("liveness_policy", "Liveness must use a non-mutating idle nudge.")

    required_ids = registry.get("required_ids")
    failure_entries = registry.get("failures")
    if required_ids != EXPECTED_PFJ_IDS:
        error("registry_required_ids", "Registry must list PFJ-001 through PFJ-036 exactly once and in order.")
    if not isinstance(failure_entries, list):
        error("registry_failures", "Registry failures must be a list.")
        failure_entries = []
    failure_ids = [entry.get("pfj_id") for entry in failure_entries if isinstance(entry, dict)]
    if failure_ids != EXPECTED_PFJ_IDS:
        error("registry_entries", "Registry entries must cover PFJ-001 through PFJ-036 exactly once and in order.")
    for entry in failure_entries:
        if not isinstance(entry, dict):
            continue
        for key in ("signature", "block_before", "verification_mode", "required_evidence"):
            if not _nonempty(entry.get(key)):
                error("registry_entry_incomplete", f"{entry.get('pfj_id')} is missing {key}.")

    reflection = (
        round_record.get("mandatory_reflection")
        if isinstance(round_record.get("mandatory_reflection"), dict)
        else {}
    )
    if reflection.get("question_1") != "What mistake did I make last time?":
        error("round_question_1", "Round question 1 is missing.")
    if reflection.get("question_2") != "How will I avoid it in the most straightforward way this round?":
        error("round_question_2", "Round question 2 is missing.")
    for key in ("answer_1", "answer_2", "material_difference_from_prior_round"):
        if not _nonempty(reflection.get(key)):
            error("round_reflection_incomplete", f"Mandatory reflection is missing {key}.")

    journal_review = (
        round_record.get("journal_review")
        if isinstance(round_record.get("journal_review"), dict)
        else {}
    )
    if journal_review.get("missing_registry_ids"):
        error("journal_registry_gap", "The Journal contains PFJ IDs missing from the registry.")
    if journal_review.get("known_signatures_detected"):
        error("known_signature_recurrence", "A known PFJ signature was detected; candidate is blocked immediately.")

    phase_rank = PHASES[phase]
    gates = round_record.get("gates") if isinstance(round_record.get("gates"), dict) else {}
    gate_results = (
        gates.get("known_failure_results")
        if isinstance(gates.get("known_failure_results"), dict)
        else {}
    )
    if phase_rank >= 1:
        for entry in failure_entries:
            if not isinstance(entry, dict):
                continue
            if BLOCK_STAGE.get(str(entry.get("block_before")), 3) > phase_rank:
                continue
            pfj_id = str(entry.get("pfj_id"))
            result = gate_results.get(pfj_id)
            if not isinstance(result, dict) or result.get("status") not in {"passed", "not_applicable"}:
                error("historical_gate_not_passed", f"{pfj_id} is not passed or evidenced non-applicable.")
                continue
            if result.get("status") == "not_applicable" and not _nonempty(result.get("rationale")):
                error("gate_waiver_without_rationale", f"{pfj_id} is non-applicable without rationale.")
            if not _nonempty(result.get("evidence")):
                error("historical_gate_no_evidence", f"{pfj_id} has no retained evidence.")

    if phase_rank >= 2:
        if gates.get("candidate_build_allowed") is not True:
            error("candidate_build_not_allowed", "Candidate build gate has not passed.")
        if gates.get("candidate_install_allowed") is not True:
            error("candidate_install_not_allowed", "Candidate install gate has not passed.")
        candidate = round_record.get("candidate") if isinstance(round_record.get("candidate"), dict) else {}
        for key in (
            "candidate_id",
            "product_code",
            "product_commit",
            "packaging_commit",
            "media_directory",
            "msi_sha256",
        ):
            if not _nonempty(candidate.get(key)):
                error("candidate_identity_incomplete", f"Candidate identity is missing {key}.")

    if phase_rank >= 3:
        actual = (
            round_record.get("actual_validation")
            if isinstance(round_record.get("actual_validation"), dict)
            else {}
        )
        if actual.get("actual_install_attempted") is not True:
            error("actual_install_missing", "Round close requires an actual installation attempt.")
        if actual.get("install_mode") != "human_gui_usecomputer":
            error("actual_install_mode", "Round close requires human_gui_usecomputer installation mode.")
        if actual.get("installed_shortcut_launch_attempted") is not True:
            error("actual_launch_missing", "Round close requires an actual installed shortcut launch.")
        if actual.get("uninstall_attempted") is not True:
            error("actual_uninstall_missing", "Round close requires candidate Uninstall.")
        if actual.get("cleanup_passed") is not True:
            error("cleanup_not_passed", "Round close requires a zero-residue cleanup pass.")
        if actual.get("original_debug_runtime_unchanged") is not True:
            error("debug_runtime_boundary", "Original Debug Runtime preservation is not proved.")
        if actual.get("user_visible_launch_result") == "success" and actual.get("basic_functions_attempted") is not True:
            error("basic_functions_missing", "Successful launch requires actual basic-function tests.")
        outcome = round_record.get("outcome") if isinstance(round_record.get("outcome"), dict) else {}
        if outcome.get("status") == "lifecycle_accepted" and actual.get("repair_attempted") is not True:
            error("repair_missing", "Lifecycle acceptance requires actual Repair and post-Repair validation.")

    return {
        "ok": not findings,
        "phase": phase,
        "loop_dir": str(loop_dir),
        "finding_count": len(findings),
        "findings": findings,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate an Aptenra packaging Superloop round.")
    parser.add_argument("--loop-dir", type=Path, required=True)
    parser.add_argument("--phase", choices=tuple(PHASES), default="structure")
    args = parser.parse_args()
    report = validate_round(args.loop_dir.resolve(), args.phase)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
