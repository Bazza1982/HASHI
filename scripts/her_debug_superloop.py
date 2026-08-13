#!/usr/bin/env python3
"""Instantiate and operate the persisted two-stage HER debug Superloop."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from orchestrator.skill_manager import SkillManager
from orchestrator.superloop_runner import SuperloopRunner
from orchestrator.superloop_store import SuperloopStore, agent_actor
from orchestrator.superloop_validator import validate_loop


TEMPLATE = ROOT / "superloops" / "templates" / "her_debug"
SUPERLOOPS = ROOT / "superloops"
TASKS_PATH = ROOT / "tasks.json"
PROVIDER_ORDER = ("official_deepseek", "openrouter")
MODE_ORDER = ("fixed", "flex")
EFFORT_ORDER = ("low", "medium", "high", "xhigh", "max", "max+")
SCENARIOS = tuple(f"C{number:02d}" for number in range(10))
CORE_OFF = "core_off"
HABIT_ON = "habit_on"
HABIT_WIRE = "habit_wire"
HABIT_DEEP = "habit_deep"
HABIT_FAULT = "habit_fault"
EXPECTED_CORE_CELLS = 48
EXPECTED_HABIT_WIRE_CELLS = 48
EXPECTED_HABIT_DEEP_CELLS = 8
EXPECTED_HABIT_FAULT_CELLS = 4
CURRENT_JOINT_CAMPAIGN_VERSION = 3
EXPECTED_LIVE_WORK_ITEMS = (
    EXPECTED_CORE_CELLS
    + EXPECTED_HABIT_WIRE_CELLS
    + EXPECTED_HABIT_DEEP_CELLS
    + EXPECTED_HABIT_FAULT_CELLS
)
STAGES = (
    ("stage_1_flash", "FLASH", {"official_deepseek": "deepseek-v4-flash", "openrouter": "deepseek/deepseek-v4-flash"}),
    ("stage_2_pro", "PRO", {"official_deepseek": "deepseek-v4-pro", "openrouter": "deepseek/deepseek-v4-pro"}),
)
CONTROLLER_ACTOR = agent_actor("lin_yueru", instance="HASHI2", source="her_debug_controller")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _atomic_json(path: Path, payload: Any, *, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + f".tmp-{os.getpid()}")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    if mode is not None:
        temporary.chmod(mode)
    temporary.replace(path)


def _render(value: Any, replacements: dict[str, str]) -> Any:
    if isinstance(value, str):
        for source, target in replacements.items():
            value = value.replace(source, target)
        return value
    if isinstance(value, list):
        return [_render(item, replacements) for item in value]
    if isinstance(value, dict):
        return {key: _render(item, replacements) for key, item in value.items()}
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _next_evidence_name(loop_dir: Path, preferred_name: str) -> str:
    """Return a non-overwriting evidence filename for a repeated transition."""
    preferred = loop_dir / "evidence" / preferred_name
    if not preferred.exists():
        return preferred_name
    stem = Path(preferred_name).stem
    suffix = Path(preferred_name).suffix
    ordinal = 2
    while True:
        candidate = f"{stem}-a{ordinal:03d}{suffix}"
        if not (loop_dir / "evidence" / candidate).exists():
            return candidate
        ordinal += 1


def _candidate_fingerprint(
    *,
    hashi_commit: str,
    hashi_build_sha256: str,
    her_source_commit: str,
    package_sha256: str,
    oracle_sha256: str,
) -> str:
    identity = {
        "hashi_commit": hashi_commit,
        "hashi_build_sha256": hashi_build_sha256,
        "her_source_commit": her_source_commit,
        "package_sha256": package_sha256,
        "oracle_sha256": oracle_sha256,
    }
    encoded = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _effort_code(effort: str) -> str:
    return effort.upper().replace("+", "PLUS")


def _provider_code(provider: str) -> str:
    return "DS" if provider == "official_deepseek" else "OR"


def _presentation_runs(cell_id: str, *, status: str) -> list[dict[str, Any]]:
    runs: list[dict[str, Any]] = []
    ordinal = 0
    for thinking in (False, True):
        for verbose in (False, True):
            for typing in (False, True):
                ordinal += 1
                runs.append(
                    {
                        "run_id": f"{cell_id}-P{ordinal:02d}",
                        "thinking": thinking,
                        "verbose": verbose,
                        "typing": typing,
                        "status": status,
                        "attempt_refs": [],
                    }
                )
    return runs


def _build_work_items() -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for stage_index, (stage_id, model_code, model_map) in enumerate(STAGES, start=1):
        stage_status = "locked" if stage_id == "stage_2_pro" else "pending"
        for effort_index, effort in enumerate(EFFORT_ORDER):
            # Rotate provider order between effort batches while retaining a frozen manifest.
            providers = PROVIDER_ORDER if effort_index % 2 == 0 else tuple(reversed(PROVIDER_ORDER))
            for provider in providers:
                for mode in MODE_ORDER:
                    cell_id = (
                        f"HER-LIVE-{_provider_code(provider)}-{model_code}-"
                        f"{mode.upper()}-{_effort_code(effort)}"
                    )
                    items.append(
                        {
                            "work_item_id": cell_id,
                            "stage": stage_id,
                            "stage_ordinal": stage_index,
                            "provider": provider,
                            "model": model_map[provider],
                            "mode": mode,
                            "effort": effort,
                            "feature_profile": CORE_OFF,
                            "habit_scenario": "none",
                            "status": stage_status,
                            "candidate_hash": None,
                            "scenario_groups": [
                                {
                                    "scenario_id": scenario,
                                    "packet_key": f"{stage_id}/{provider}/{model_map[provider]}/{mode}/{effort}/{CORE_OFF}/none/core_scenario/{scenario}/default",
                                    "status": stage_status,
                                    "attempt_refs": [],
                                }
                                for scenario in SCENARIOS
                            ],
                            "presentation_runs": _presentation_runs(
                                cell_id,
                                status=stage_status,
                            ),
                            "native_boundary": {"status": stage_status, "attempt_refs": []},
                            "cold_exactness_repeats": [
                                {"repeat": repeat, "status": stage_status, "attempt_refs": []}
                                for repeat in range(1, 4)
                            ],
                            "warm_repeat": {"status": stage_status, "attempt_refs": []},
                            "verdict": None,
                            "evidence_refs": [],
                        }
                    )

                    habit_wire_id = f"{cell_id}-HABIT-WIRE"
                    items.append(
                        {
                            "work_item_id": habit_wire_id,
                            "stage": stage_id,
                            "stage_ordinal": stage_index,
                            "provider": provider,
                            "model": model_map[provider],
                            "mode": mode,
                            "effort": effort,
                            "feature_profile": HABIT_ON,
                            "habit_scenario": HABIT_WIRE,
                            "status": stage_status,
                            "candidate_hash": None,
                            "scenario_groups": [
                                {
                                    "scenario_id": "HW00",
                                    "packet_key": f"{stage_id}/{provider}/{model_map[provider]}/{mode}/{effort}/{HABIT_ON}/{HABIT_WIRE}/cheap/HW00/default",
                                    "status": stage_status,
                                    "attempt_refs": [],
                                }
                            ],
                            "presentation_runs": [],
                            "native_boundary": {"status": "not_applicable", "attempt_refs": []},
                            "cold_exactness_repeats": [],
                            "warm_repeat": {"status": "not_applicable", "attempt_refs": []},
                            "verdict": None,
                            "evidence_refs": [],
                        }
                    )

        # The deep lifecycle is exercised once per provider/model/mode at high.
        for provider in PROVIDER_ORDER:
            for mode in MODE_ORDER:
                cell_id = (
                    f"HER-LIVE-{_provider_code(provider)}-{model_code}-"
                    f"{mode.upper()}-HIGH-HABIT-DEEP"
                )
                items.append(
                    {
                        "work_item_id": cell_id,
                        "stage": stage_id,
                        "stage_ordinal": stage_index,
                        "provider": provider,
                        "model": model_map[provider],
                        "mode": mode,
                        "effort": "high",
                        "feature_profile": HABIT_ON,
                        "habit_scenario": HABIT_DEEP,
                        "status": stage_status,
                        "candidate_hash": None,
                        "scenario_groups": [
                            {
                                "scenario_id": "HD00",
                                "packet_key": f"{stage_id}/{provider}/{model_map[provider]}/{mode}/high/{HABIT_ON}/{HABIT_DEEP}/lifecycle/HD00/default",
                                "status": stage_status,
                                "attempt_refs": [],
                            }
                        ],
                        "presentation_runs": [],
                        "native_boundary": {"status": "not_applicable", "attempt_refs": []},
                        "cold_exactness_repeats": [],
                        "warm_repeat": {"status": "not_applicable", "attempt_refs": []},
                        "verdict": None,
                        "evidence_refs": [],
                    }
                )

        # One real restart/fault unit per provider/model is sufficient; the
        # mode twins are already covered by HABIT-DEEP.
        for provider in PROVIDER_ORDER:
            cell_id = (
                f"HER-LIVE-{_provider_code(provider)}-{model_code}-"
                "FIXED-HIGH-HABIT-FAULT"
            )
            items.append(
                {
                    "work_item_id": cell_id,
                    "stage": stage_id,
                    "stage_ordinal": stage_index,
                    "provider": provider,
                    "model": model_map[provider],
                    "mode": "fixed",
                    "effort": "high",
                    "feature_profile": HABIT_ON,
                    "habit_scenario": HABIT_FAULT,
                    "status": stage_status,
                    "candidate_hash": None,
                    "scenario_groups": [
                        {
                            "scenario_id": "HF00",
                            "packet_key": f"{stage_id}/{provider}/{model_map[provider]}/fixed/high/{HABIT_ON}/{HABIT_FAULT}/fault/HF00/default",
                            "status": stage_status,
                            "attempt_refs": [],
                        }
                    ],
                    "presentation_runs": [],
                    "native_boundary": {"status": "not_applicable", "attempt_refs": []},
                    "cold_exactness_repeats": [],
                    "warm_repeat": {"status": "not_applicable", "attempt_refs": []},
                    "verdict": None,
                    "evidence_refs": [],
                }
            )
    return items


def _template_hashes() -> dict[str, str]:
    return {
        path.name: _sha256(path)
        for path in sorted(TEMPLATE.iterdir())
        if path.is_file() and path.name != "__pycache__"
    }


def _in_progress_packet_start_authority_conflict(
    state: dict[str, Any],
    tasks: list[dict[str, Any]],
) -> bool:
    """Detect the authority contradiction that can livelock packet continuation."""

    current_task_id = str(state.get("current_step") or "")
    current_task = next(
        (task for task in tasks if str(task.get("task_id") or "") == current_task_id),
        None,
    )
    if not isinstance(current_task, dict) or current_task.get("status") != "in_progress":
        return False
    if state.get("active_dispatch_id") or state.get("active_wait_id"):
        return False
    selected = state.get("selected_next_packet")
    if not isinstance(selected, dict) or selected.get("started") is not False:
        return False
    next_action = state.get("next_action") if isinstance(state.get("next_action"), dict) else {}
    return bool(
        selected.get("pending_non_nudge_start_authority") is True
        or next_action.get("pending_non_nudge_start_authority") is True
        or next_action.get("kind") == "await_non_nudge_start_authority"
    )


def _in_progress_idle_control_livelock(
    state: dict[str, Any],
    campaign: dict[str, Any],
    tasks: list[dict[str, Any]],
) -> bool:
    """Detect an idle in-progress task hidden by an invalid control wait."""

    current_task_id = str(state.get("current_step") or "")
    current_task = next(
        (task for task in tasks if str(task.get("task_id") or "") == current_task_id),
        None,
    )
    if not isinstance(current_task, dict) or current_task.get("status") != "in_progress":
        return False
    if any(
        state.get(key)
        for key in ("active_dispatch_id", "active_request_id", "active_wait_id")
    ):
        return False
    authority = (
        state.get("operator_execution_authority")
        if isinstance(state.get("operator_execution_authority"), dict)
        else {}
    )
    if authority.get("status") != "active":
        return False

    status = str(state.get("status") or "").strip().lower()
    control = state.get("control") if isinstance(state.get("control"), dict) else {}
    pause = control.get("pause") if isinstance(control.get("pause"), dict) else {}
    if status == "paused":
        explicit_operator_pause = (
            pause.get("kind") == "operator_pause"
            and pause.get("resume_policy") == "explicit_operator"
        )
        return bool(
            not explicit_operator_pause
            and pause.get("drain_complete") is True
            and str(campaign.get("status") or "").strip().lower() == "running"
        )
    if status != "running":
        return False

    selected = state.get("selected_next_packet")
    if isinstance(selected, dict):
        return False
    next_action = state.get("next_action") if isinstance(state.get("next_action"), dict) else {}
    action_kind = str(next_action.get("kind") or "")
    if action_kind == "await_controller_drain" and isinstance(
        control.get("controller_drain"), dict
    ):
        return False
    return action_kind in {
        "await_operator_resume",
        "await_non_nudge_start_authority",
        "await_controller_drain",
    }


def _custom_validation(loop_id: str) -> dict[str, Any]:
    loop_dir = SUPERLOOPS / "loops" / loop_id
    findings: list[dict[str, str]] = []

    def require(condition: bool, code: str, message: str) -> None:
        if not condition:
            findings.append({"code": code, "message": message})

    try:
        state = _load_json(loop_dir / "state.json")
        campaign = _load_json(loop_dir / "campaign.json")
        work_items = _load_json(loop_dir / "work_items.json")
        tasks = _load_json(loop_dir / "taskboard.json")
    except (OSError, json.JSONDecodeError) as exc:
        return {"ok": False, "finding_count": 1, "findings": [{"code": "load", "message": str(exc)}]}

    require(state.get("template") == "her_debug", "template", "Loop must use her_debug.")
    require(
        campaign.get("joint_campaign_version") == CURRENT_JOINT_CAMPAIGN_VERSION,
        "joint_campaign_version",
        f"Campaign must use joint version {CURRENT_JOINT_CAMPAIGN_VERSION}.",
    )
    require(state.get("owner_agent") == "lin_yueru", "owner", "Controller owner must be lin_yueru.")
    require(state.get("scheduler_auto_advance") is False, "scheduler", "Scheduler auto-advance must stay disabled.")
    core_items = [
        item for item in work_items if item.get("feature_profile") == CORE_OFF
    ]
    habit_wire_items = [
        item
        for item in work_items
        if item.get("feature_profile") == HABIT_ON
        and item.get("habit_scenario") == HABIT_WIRE
    ]
    habit_deep_items = [
        item
        for item in work_items
        if item.get("feature_profile") == HABIT_ON
        and item.get("habit_scenario") == HABIT_DEEP
    ]
    habit_fault_items = [
        item
        for item in work_items
        if item.get("feature_profile") == HABIT_ON
        and item.get("habit_scenario") == HABIT_FAULT
    ]
    require(
        len(work_items) == EXPECTED_LIVE_WORK_ITEMS,
        "work_item_count",
        "The joint campaign must expand 108 live work items.",
    )
    require(
        len({item.get("work_item_id") for item in work_items})
        == EXPECTED_LIVE_WORK_ITEMS,
        "work_item_ids",
        "Live work-item IDs must be unique.",
    )
    require(
        len(core_items) == EXPECTED_CORE_CELLS,
        "core_cell_count",
        "Exactly 48 CORE-OFF cells must be expanded.",
    )
    require(
        sum(len(item.get("scenario_groups", [])) for item in core_items) == 480,
        "core_scenario_count",
        "Exactly 480 CORE-OFF scenario groups must be expanded.",
    )
    require(
        sum(len(item.get("presentation_runs", [])) for item in core_items) == 384,
        "core_presentation_count",
        "Exactly 384 CORE-OFF presentation runs must be expanded.",
    )
    require(
        len(habit_wire_items) == EXPECTED_HABIT_WIRE_CELLS,
        "habit_wire_count",
        "Exactly 48 HABIT-WIRE cells must be expanded.",
    )
    require(
        len(habit_deep_items) == EXPECTED_HABIT_DEEP_CELLS,
        "habit_deep_count",
        "Exactly eight HABIT-DEEP cells must be expanded.",
    )
    require(
        len(habit_fault_items) == EXPECTED_HABIT_FAULT_CELLS,
        "habit_fault_count",
        "Exactly four HABIT-FAULT cells must be expanded.",
    )
    require(
        sum(
            item.get("stage") == "stage_1_flash"
            and item.get("feature_profile") == CORE_OFF
            for item in work_items
        )
        == 24,
        "flash_core_count",
        "Stage 1 must contain 24 CORE-OFF cells.",
    )
    require(
        sum(
            item.get("stage") == "stage_2_pro"
            and item.get("feature_profile") == CORE_OFF
            for item in work_items
        )
        == 24,
        "pro_core_count",
        "Stage 2 must contain 24 CORE-OFF cells.",
    )
    for item in work_items:
        stage_models = dict(STAGES[0][2] if item.get("stage") == "stage_1_flash" else STAGES[1][2])
        require(item.get("model") == stage_models.get(item.get("provider")), "route_model", f"Illegal model for {item.get('work_item_id')}.")
        require(
            item.get("feature_profile") in {CORE_OFF, HABIT_ON},
            "feature_profile",
            f"Invalid feature profile for {item.get('work_item_id')}.",
        )
        if item.get("feature_profile") == CORE_OFF:
            require(
                item.get("habit_scenario") == "none",
                "core_habit_scenario",
                f"CORE-OFF item has a Habit scenario: {item.get('work_item_id')}.",
            )
        else:
            require(
                item.get("habit_scenario")
                in {HABIT_WIRE, HABIT_DEEP, HABIT_FAULT},
                "habit_scenario",
                f"Invalid Habit scenario for {item.get('work_item_id')}.",
            )
        if item.get("stage") == "stage_2_pro" and (
            state.get("gates", {}).get("core_flash") != "passed"
            or state.get("gates", {}).get("habit_flash") != "passed"
        ):
            require(item.get("status") == "locked", "pro_lock", f"Pro cell is unlocked early: {item.get('work_item_id')}.")
    require(len(tasks) == 10, "task_count", "The phase taskboard must contain ten tasks.")
    require(campaign.get("expected_counts", {}).get("total_core_cells") == 48, "campaign_count", "Campaign count changed.")
    require(
        campaign.get("expected_counts", {}).get("total_live_work_items")
        == EXPECTED_LIVE_WORK_ITEMS,
        "campaign_joint_count",
        "Campaign joint live count changed.",
    )
    live_phase_active = str(state.get("current_step") or "") in {
        f"HD-{number:03d}" for number in range(3, 11)
    } or any(
        item.get("status") not in {"pending", "locked"} for item in work_items
    )
    if live_phase_active:
        require(
            state.get("candidate", {}).get("evidence_valid") is True
            and state.get("candidate", {}).get("freeze_status")
            == "frozen_after_joint_layer_a"
            and state.get("gates", {}).get("layer_a_offline") == "passed",
            "live_before_joint_layer_a_freeze",
            "Live work cannot start before the composite candidate passes joint Layer A.",
        )
    require(
        not _in_progress_packet_start_authority_conflict(state, tasks),
        "in_progress_packet_authority_livelock",
        "The current in-progress task has an unstarted packet that incorrectly requires fresh start authority.",
    )
    require(
        not _in_progress_idle_control_livelock(state, campaign, tasks),
        "in_progress_idle_control_livelock",
        "The current in-progress task is idle behind an invalid control wait or ambiguous drained pause.",
    )

    generic = validate_loop(SuperloopStore(SUPERLOOPS), loop_id, closeout=False)
    require(generic.get("summary", {}).get("errors", 0) == 0, "generic_validation", "Generic Superloop validation has errors.")
    return {
        "ok": not findings,
        "loop_id": loop_id,
        "core_cells": len(core_items),
        "habit_wire_cells": len(habit_wire_items),
        "habit_deep_cells": len(habit_deep_items),
        "habit_fault_cells": len(habit_fault_items),
        "live_work_items": len(work_items),
        "scenario_groups": sum(len(item.get("scenario_groups", [])) for item in core_items),
        "presentation_runs": sum(len(item.get("presentation_runs", [])) for item in core_items),
        "generic": generic,
        "finding_count": len(findings),
        "findings": findings,
    }


def instantiate() -> dict[str, Any]:
    from superloops.templates.her_debug.validate_template import validate_template

    template_report = validate_template(TEMPLATE)
    if not template_report.get("ok"):
        raise RuntimeError(f"her_debug template validation failed: {template_report}")

    store = SuperloopStore(SUPERLOOPS)
    loop_id = store.generate_loop_id()
    campaign_id = f"her-debug-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}-{loop_id[-4:]}"
    replacements = {"{{LOOP_ID}}": loop_id, "{{CAMPAIGN_ID}}": campaign_id}
    state = _render(_load_json(TEMPLATE / "state.template.json"), replacements)
    campaign = _render(_load_json(TEMPLATE / "campaign.template.json"), replacements)
    tasks = _render(_load_json(TEMPLATE / "taskboard.template.json"), replacements)
    roles = _render(_load_json(TEMPLATE / "roles.template.json"), replacements)
    now = _utc_now()
    state.update({"created_at": now, "updated_at": now, "campaign_id": campaign_id})
    campaign.update({"created_at": now, "updated_at": now})
    for task in tasks:
        task["execution_mode"] = "hchat_agent" if task.get("owner_agent") == "ajiao" else "local_self"
        task["dispatch_refs"] = []
        task["receipt_refs"] = []
        task["receipt_sources"] = []
        task["evidence_refs"] = []

    work_items = _build_work_items()
    campaign["progress"]["outstanding_work_item_ids"] = [item["work_item_id"] for item in work_items]
    summary = (
        "# HER debug live campaign\n\n"
        f"- Loop: `{loop_id}`\n"
        f"- Campaign: `{campaign_id}`\n"
        "- Controller: `lin_yueru@HASHI2`\n"
        "- Worker: `ajiao@HASHI2`\n"
        "- Terminal results: `PASSED` or `BLOCKED_FUNDS` only\n"
        "- Oracle: `docs/HER_COMPREHENSIVE_TEST_PLAN.md`\n"
        "- Policy: `controller_policy.md`\n"
    )
    store.create_compiled_loop(
        loop_id=loop_id,
        loop_state=state,
        taskboard=tasks,
        issues=[],
        waits=[],
        operator_summary=summary,
        event_data={"template": "her_debug", "campaign_id": campaign_id},
        actor=CONTROLLER_ACTOR,
    )
    loop_dir = store.loop_dir(loop_id)
    _atomic_json(loop_dir / "campaign.json", campaign)
    _atomic_json(loop_dir / "roles.json", roles)
    _atomic_json(loop_dir / "work_items.json", work_items)
    _atomic_json(
        loop_dir / "template_snapshot.json",
        {
            "captured_at": now,
            "files": _template_hashes(),
            "oracle_sha256": _sha256(ROOT / "docs" / "HER_COMPREHENSIVE_TEST_PLAN.md"),
        },
    )
    (loop_dir / "dispatches.jsonl").touch(exist_ok=False)
    (loop_dir / "attempts").mkdir()
    (loop_dir / "evidence").mkdir()
    policy = (TEMPLATE / "liveness_nudge.template.md").read_text(encoding="utf-8")
    (loop_dir / "controller_policy.md").write_text(policy.replace("{{LOOP_ID}}", loop_id), encoding="utf-8")
    store.append_loop_event(
        loop_id,
        event_type="campaign.expanded",
        data={
            "core_cells": EXPECTED_CORE_CELLS,
            "habit_wire_cells": EXPECTED_HABIT_WIRE_CELLS,
            "habit_deep_cells": EXPECTED_HABIT_DEEP_CELLS,
            "habit_fault_cells": EXPECTED_HABIT_FAULT_CELLS,
            "live_work_items": EXPECTED_LIVE_WORK_ITEMS,
            "scenario_groups": 480,
            "presentation_runs": 384,
        },
        actor=CONTROLLER_ACTOR,
    )
    report = _custom_validation(loop_id)
    _atomic_json(loop_dir / "activation_validation.json", report)
    if not report.get("ok"):
        raise RuntimeError(f"instantiated loop failed validation: {report}")
    return {"ok": True, "loop_id": loop_id, "campaign_id": campaign_id, "status": "paused", "validation": report}


def migrate_joint_campaign(loop_id: str) -> dict[str, Any]:
    """Realign one paused HER campaign to the current joint oracle.

    Existing evidence files and append-only event/dispatch ledgers are never
    rewritten. The mutable candidate, task, campaign, and work-item projections
    are invalidated and regenerated so no pre-migration result can satisfy the
    new release gates.
    """

    from superloops.templates.her_debug.validate_template import validate_template

    template_report = validate_template(TEMPLATE)
    if not template_report.get("ok"):
        raise RuntimeError(f"her_debug template validation failed: {template_report}")

    store = SuperloopStore(SUPERLOOPS)
    loop_dir = store.loop_dir(loop_id)
    state = _load_json(loop_dir / "state.json")
    campaign = _load_json(loop_dir / "campaign.json")
    old_tasks = _load_json(loop_dir / "taskboard.json")
    old_work_items = _load_json(loop_dir / "work_items.json")
    old_waits = _load_json(loop_dir / "waits.json")
    source_version = int(campaign.get("joint_campaign_version") or 1)
    if state.get("status") != "paused":
        raise RuntimeError("joint campaign migration requires a paused loop")
    if state.get("active_dispatch_id") or state.get("active_request_id"):
        raise RuntimeError("joint campaign migration requires a drained loop")
    stale_runtime_keys = {
        key for key in ("active_attempt_id", "worker_runtime", "pause_closeout_ref")
        if key in state
    }
    if (
        source_version == CURRENT_JOINT_CAMPAIGN_VERSION
        and len(old_work_items) == EXPECTED_LIVE_WORK_ITEMS
        and not stale_runtime_keys
    ):
        return {
            "ok": True,
            "migrated": False,
            "reason": f"already_joint_campaign_v{CURRENT_JOINT_CAMPAIGN_VERSION}",
            "loop_id": loop_id,
            "validation": _custom_validation(loop_id),
        }
    if source_version > CURRENT_JOINT_CAMPAIGN_VERSION:
        raise RuntimeError(
            f"cannot downgrade joint campaign v{source_version} to "
            f"v{CURRENT_JOINT_CAMPAIGN_VERSION}"
        )

    now = _utc_now()
    campaign_id = str(campaign.get("campaign_id") or state.get("campaign_id") or "")
    replacements = {"{{LOOP_ID}}": loop_id, "{{CAMPAIGN_ID}}": campaign_id}
    preferred_receipt_name = (
        "HD-001-joint-campaign-migration.json"
        if source_version <= 1
        else "HD-001-habit-test-enhancement-realignment.json"
    )
    receipt_name = _next_evidence_name(loop_dir, preferred_receipt_name)
    receipt_ref = f"superloops/loops/{loop_id}/evidence/{receipt_name}"
    receipt_path = loop_dir / "evidence" / receipt_name
    transition_reason = (
        "joint HER/Habit campaign migration and product repair"
        if source_version <= 1
        else (
            "operator-approved Habit evidence enhancement and candidate realignment"
            if source_version < CURRENT_JOINT_CAMPAIGN_VERSION
            else "joint campaign runtime-ownership repair and candidate realignment"
        )
    )
    pre_paths = (
        "state.json",
        "campaign.json",
        "taskboard.json",
        "work_items.json",
        "waits.json",
        "template_snapshot.json",
    )
    pre_hashes = {
        name: _sha256(loop_dir / name)
        for name in pre_paths
        if (loop_dir / name).is_file()
    }
    old_candidate = deepcopy(state.get("candidate") or {})
    old_status_counts: dict[str, int] = {}
    for item in old_work_items:
        key = str(item.get("status") or "unknown")
        old_status_counts[key] = old_status_counts.get(key, 0) + 1
    old_evidence_refs = sorted(
        {
            str(ref)
            for item in old_work_items
            for ref in item.get("evidence_refs", [])
            if str(ref).strip()
        }
    )
    new_waits = deepcopy(old_waits)
    stale_wait_ids: list[str] = []
    for wait in new_waits:
        prior_status = str(wait.get("status") or "")
        if prior_status not in {"pending", "open"}:
            continue
        wait["status"] = "stale"
        wait["prior_status"] = prior_status
        wait["stale_at"] = now
        wait["stale_reason"] = (
            "joint campaign migration superseded the candidate and live packet; "
            "revalidate this condition when its provider phase is reached"
        )
        stale_wait_ids.append(str(wait.get("wait_id") or ""))

    new_work_items = _build_work_items()
    new_campaign = _render(
        _load_json(TEMPLATE / "campaign.template.json"),
        replacements,
    )
    new_campaign.update(
        {
            "status": "paused",
            "created_at": campaign.get("created_at") or state.get("created_at") or now,
            "updated_at": now,
        }
    )
    new_campaign["progress"]["outstanding_work_item_ids"] = [
        item["work_item_id"] for item in new_work_items
    ]
    old_history = list(campaign.get("candidate_history", []))
    if old_candidate.get("hash") and not any(
        item.get("candidate_hash") == old_candidate.get("hash")
        for item in old_history
        if isinstance(item, dict)
    ):
        old_history.append(
            {
                "candidate_hash": old_candidate.get("hash"),
                "hashi_commit": old_candidate.get("hashi_commit"),
                "her_source_commit": old_candidate.get("her_source_commit"),
                "package_sha256": old_candidate.get("package_sha256"),
                "valid_until": now,
                "superseded_reason": transition_reason,
                "supersession_evidence_ref": receipt_ref,
            }
        )
    new_campaign["candidate_history"] = old_history
    old_progress = campaign.get("progress", {})
    new_campaign["progress"]["historical_pre_joint_migration"] = {
        "source_projection_sha256": pre_hashes.get("work_items.json"),
        "status_counts": old_status_counts,
        "evidence_ref_count": len(old_evidence_refs),
        "completed_attempt_ids": list(old_progress.get("completed_attempt_ids", [])),
        "classified_reply_refs": list(old_progress.get("classified_reply_refs", [])),
        "receipt_ref": receipt_ref,
    }
    defect_gates = deepcopy(old_progress.get("defect_gates", {}))
    defect_gates["HER-20260812-020"] = {
        "status": "fixed_joint_verification_pending",
        "regression": "test_reused_hashi_request_id_creates_distinct_meditation_jobs",
        "next_action": "run_joint_layer_a_then_refreeze_composite_candidate",
        "pro_remains_locked": True,
    }
    defect_gates["HER-20260812-022"] = {
        "status": "fixed_joint_verification_pending",
        "regression": "test_meditation_timeout_bounds_foreground_lock_wait_and_stays_silent",
        "next_action": "run_joint_layer_a_then_revalidate_habit_fault_timeout",
        "pro_remains_locked": True,
    }
    defect_gates["HER-20260812-023"] = {
        "status": "fixed_joint_verification_pending",
        "regression": "test_joint_realign_clears_stale_runtime_ownership_without_overwriting_receipt",
        "next_action": "prove_idle_ownership_then_dispatch_replacement_joint_layer_a",
        "pro_remains_locked": True,
    }
    new_campaign["progress"]["defect_gates"] = defect_gates
    if old_progress.get("route_waits"):
        route_waits = deepcopy(old_progress["route_waits"])
        for wait in route_waits:
            if wait.get("status") not in {"pending", "open"}:
                continue
            wait["prior_status"] = wait.get("status")
            wait["status"] = "stale"
            wait["stale_at"] = now
            wait["stale_reason"] = "joint_campaign_candidate_superseded"
        new_campaign["progress"]["route_waits"] = route_waits

    new_tasks = _render(
        _load_json(TEMPLATE / "taskboard.template.json"),
        replacements,
    )
    old_task_map = {
        str(task.get("task_id") or ""): task
        for task in old_tasks
        if isinstance(task, dict)
    }
    for task in new_tasks:
        task_id = str(task.get("task_id") or "")
        task["execution_mode"] = (
            "hchat_agent" if task.get("owner_agent") == "ajiao" else "local_self"
        )
        task["dispatch_refs"] = []
        task["receipt_refs"] = []
        task["receipt_sources"] = []
        task["evidence_refs"] = []
        task["artifact_refs"] = []
        prior = old_task_map.get(task_id, {})
        task["historical_pre_joint_migration"] = {
            "status": prior.get("status"),
            "artifact_refs": list(prior.get("artifact_refs", [])),
            "evidence_refs": list(prior.get("evidence_refs", [])),
            "receipt_refs": list(prior.get("receipt_refs", [])),
            "stale_reason": "oracle_and_candidate_changed_for_current_joint_HER_Habit_certification",
            "migration_receipt_ref": receipt_ref,
        }

    new_state = deepcopy(state)
    rendered_state = _render(
        _load_json(TEMPLATE / "state.template.json"),
        replacements,
    )
    new_state.update(
        {
            "title": rendered_state["title"],
            "status": "paused",
            "terminal_result": None,
            "current_phase": "preflight_and_lab",
            "current_step": "HD-001",
            "next_action": {
                "kind": "await_operator_resume",
                "reason": "joint_layer_a_and_composite_candidate_refreeze_required",
            },
            "active_wait_id": None,
            "active_dispatch_id": None,
            "active_request_id": None,
            "selected_next_packet": None,
            "candidate": {
                "hash": None,
                "hashi_commit": None,
                "hashi_build_sha256": None,
                "her_source_commit": None,
                "package_sha256": None,
                "oracle_sha256": None,
                "evidence_valid": False,
                "freeze_status": "pending_joint_layer_a",
                "supersedes_candidate_hash": old_candidate.get("hash"),
                "supersession_evidence_ref": receipt_ref,
            },
            "gates": rendered_state["gates"],
            "execution_policy": rendered_state["execution_policy"],
            "updated_at": now,
            "control": {
                "requested_action": "pause",
                "pause_requested": True,
                "joint_migration": {
                    "status": "complete",
                    "migrated_at": now,
                    "receipt_ref": receipt_ref,
                    "from_joint_campaign_version": source_version,
                    "to_joint_campaign_version": CURRENT_JOINT_CAMPAIGN_VERSION,
                    "transition_reason": transition_reason,
                    "resume_requires_explicit_operator_action": True,
                },
            },
        }
    )
    # These fields describe one superseded worker execution, not durable
    # campaign history. Keeping them after a drained realignment can make the
    # nudge treat an idle worker as busy and livelock the replacement packet.
    for stale_runtime_key in (
        "active_attempt_id",
        "worker_runtime",
        "pause_closeout_ref",
    ):
        new_state.pop(stale_runtime_key, None)
    new_state["liveness"] = {
        **rendered_state["liveness"],
        "nudge_job_id": state.get("liveness", {}).get("nudge_job_id"),
        "nudge_verified_at": state.get("liveness", {}).get("nudge_verified_at"),
    }
    previous_authority = deepcopy(state.get("operator_execution_authority") or {})
    previous_authority.update(
        {
            "status": "suspended_pending_explicit_resume",
            "suspended_at": now,
            "suspension_reason": "joint_campaign_oracle_realignment",
        }
    )
    new_state["operator_execution_authority"] = previous_authority

    new_roles = _render(_load_json(TEMPLATE / "roles.template.json"), replacements)
    new_snapshot = {
        "captured_at": now,
        "files": _template_hashes(),
        "oracle_sha256": _sha256(
            ROOT / "docs" / "HER_COMPREHENSIVE_TEST_PLAN.md"
        ),
        "migration": {
            "from_joint_campaign_version": source_version,
            "to_joint_campaign_version": CURRENT_JOINT_CAMPAIGN_VERSION,
            "pre_migration_hashes": pre_hashes,
            "receipt_ref": receipt_ref,
        },
    }
    receipt = {
        "schema_version": 1,
        "verdict": (
            "MIGRATED_PENDING_REVALIDATION"
            if source_version <= 1
            else "REALIGNED_PENDING_REVALIDATION"
        ),
        "loop_id": loop_id,
        "campaign_id": campaign_id,
        "migrated_at": now,
        "from_joint_campaign_version": source_version,
        "to_joint_campaign_version": CURRENT_JOINT_CAMPAIGN_VERSION,
        "new_oracle_sha256": new_snapshot["oracle_sha256"],
        "pre_migration_hashes": pre_hashes,
        "superseded_candidate": old_candidate,
        "legacy_work_item_status_counts": old_status_counts,
        "legacy_evidence_refs": old_evidence_refs,
        "new_counts": {
            "core_off": EXPECTED_CORE_CELLS,
            "habit_wire": EXPECTED_HABIT_WIRE_CELLS,
            "habit_deep": EXPECTED_HABIT_DEEP_CELLS,
            "habit_fault": EXPECTED_HABIT_FAULT_CELLS,
            "total_live_work_items": EXPECTED_LIVE_WORK_ITEMS,
        },
        "candidate_evidence_valid": False,
        "layer_a_offline": "pending",
        "core_flash": "pending",
        "habit_flash": "pending",
        "stage_2_pro": "locked",
        "historical_evidence_mutated": False,
        "historical_wait_records_retained": True,
        "stale_wait_ids": stale_wait_ids,
        "defect_refs": [
            "HER-20260812-020",
            "HER-20260812-022",
            "HER-20260812-023",
        ],
        "transition_reason": transition_reason,
        "cleared_stale_runtime_keys": sorted(stale_runtime_keys),
        "next_action": "explicitly_resume_at_HD-001_then_run_joint_layer_a_and_refreeze",
    }

    _atomic_json(receipt_path, receipt)
    _atomic_json(loop_dir / "campaign.json", new_campaign)
    _atomic_json(loop_dir / "taskboard.json", new_tasks)
    _atomic_json(loop_dir / "work_items.json", new_work_items)
    _atomic_json(loop_dir / "waits.json", new_waits)
    _atomic_json(loop_dir / "roles.json", new_roles)
    _atomic_json(loop_dir / "template_snapshot.json", new_snapshot)
    _atomic_json(loop_dir / "state.json", new_state)
    policy = (TEMPLATE / "liveness_nudge.template.md").read_text(encoding="utf-8")
    (loop_dir / "controller_policy.md").write_text(
        policy.replace("{{LOOP_ID}}", loop_id),
        encoding="utf-8",
    )
    summary = (
        "# HER debug joint live campaign\n\n"
        f"- Loop: `{loop_id}`\n"
        f"- Campaign: `{campaign_id}`\n"
        f"- Status: paused after joint HER/Habit oracle realignment to v{CURRENT_JOINT_CAMPAIGN_VERSION}\n"
        "- Controller: `lin_yueru@HASHI2`\n"
        "- Worker: `ajiao@HASHI2`\n"
        "- CORE-OFF: 48 cells\n"
        "- HABIT-WIRE / DEEP / FAULT: 48 / 8 / 4 live items\n"
        "- Pro gate: locked until both Flash subgates pass\n"
        f"- Realignment receipt: `{receipt_ref}`\n"
        "- Historical evidence: retained but stale for release credit\n"
        "- Oracle: `docs/HER_COMPREHENSIVE_TEST_PLAN.md`\n"
        "- Policy: `controller_policy.md`\n"
    )
    (loop_dir / "README.md").write_text(summary, encoding="utf-8")

    store.refresh_loop_stats(loop_id)
    report = _custom_validation(loop_id)
    _atomic_json(loop_dir / "activation_validation.json", report)
    receipt["post_migration_validation"] = {
        "ok": report.get("ok"),
        "finding_count": report.get("finding_count"),
        "core_cells": report.get("core_cells"),
        "habit_wire_cells": report.get("habit_wire_cells"),
        "habit_deep_cells": report.get("habit_deep_cells"),
        "habit_fault_cells": report.get("habit_fault_cells"),
        "live_work_items": report.get("live_work_items"),
    }
    _atomic_json(receipt_path, receipt)
    if not report.get("ok"):
        raise RuntimeError(f"joint campaign migration failed validation: {report}")
    store.append_loop_event(
        loop_id,
        event_type=(
            f"campaign.migrated_joint_habit_v{CURRENT_JOINT_CAMPAIGN_VERSION}"
            if source_version <= 1
            else f"campaign.realigned_habit_evidence_v{CURRENT_JOINT_CAMPAIGN_VERSION}"
        ),
        data={
            "receipt_ref": receipt_ref,
            "superseded_candidate_hash": old_candidate.get("hash"),
            "live_work_items": EXPECTED_LIVE_WORK_ITEMS,
            "stale_wait_ids": stale_wait_ids,
            "pro_gate": "locked",
        },
        actor=CONTROLLER_ACTOR,
    )
    return {
        "ok": True,
        "migrated": True,
        "loop_id": loop_id,
        "receipt_ref": receipt_ref,
        "superseded_candidate_hash": old_candidate.get("hash"),
        "validation": report,
    }


def create_nudge(loop_id: str) -> dict[str, Any]:
    loop_dir = SUPERLOOPS / "loops" / loop_id
    state = _load_json(loop_dir / "state.json")
    existing_id = state.get("liveness", {}).get("nudge_job_id")
    manager = SkillManager(project_root=ROOT, tasks_path=TASKS_PATH)
    if existing_id:
        existing = manager.get_job("nudge", str(existing_id))
        if existing:
            policy = (loop_dir / "controller_policy.md").read_text(
                encoding="utf-8"
            )
            rendered_policy = policy.replace("{{NUDGE_ID}}", str(existing_id))
            tasks_payload = _load_json(TASKS_PATH)
            persisted = next(
                (
                    item
                    for item in tasks_payload.get("nudges", [])
                    if item.get("id") == existing_id
                ),
                None,
            )
            if persisted is None:
                raise RuntimeError("persisted controller nudge is missing from tasks.json")
            persisted["prompt"] = rendered_policy
            persisted["note"] = f"HER debug joint controller for {loop_id}"
            persisted["superloop_id"] = loop_id
            persisted["owner_instance"] = "HASHI2"
            _atomic_json(TASKS_PATH, tasks_payload)
            refreshed = SkillManager(
                project_root=ROOT,
                tasks_path=TASKS_PATH,
            ).get_job("nudge", str(existing_id))
            return {
                "ok": True,
                "created": False,
                "refreshed": True,
                "job": refreshed,
            }

    exit_condition = f"her_debug {loop_id} has evidenced terminal_result PASSED or BLOCKED_FUNDS"
    job = manager.create_nudge_job(
        agent_name="lin_yueru",
        interval_minutes=1,
        exit_condition=exit_condition,
        max_nudges=0,
    )
    policy = (loop_dir / "controller_policy.md").read_text(encoding="utf-8")
    rendered_policy = policy.replace("{{NUDGE_ID}}", job["id"])
    tasks_payload = _load_json(TASKS_PATH)
    persisted = next(item for item in tasks_payload.get("nudges", []) if item.get("id") == job["id"])
    persisted["prompt"] = rendered_policy
    persisted["note"] = f"HER debug controller for {loop_id}"
    persisted["superloop_id"] = loop_id
    persisted["owner_instance"] = "HASHI2"
    _atomic_json(TASKS_PATH, tasks_payload)

    verified = SkillManager(project_root=ROOT, tasks_path=TASKS_PATH).get_job("nudge", job["id"])
    if not verified or verified.get("agent") != "lin_yueru" or not verified.get("enabled"):
        raise RuntimeError("controller nudge did not persist with the required owner and enabled state")
    if loop_id not in str(verified.get("prompt")) or f"NUDGE_COMPLETE:{job['id']}" not in str(verified.get("prompt")):
        raise RuntimeError("controller policy was not loaded into the nudge prompt")

    state["liveness"]["nudge_job_id"] = job["id"]
    state["liveness"]["nudge_verified_at"] = _utc_now()
    state["next_action"] = {"kind": "await_operator_start"}
    SuperloopStore(SUPERLOOPS).save_loop_state(loop_id, state)
    SuperloopStore(SUPERLOOPS).append_loop_event(
        loop_id,
        event_type="nudge.created",
        data={"nudge_job_id": job["id"], "owner_agent": "lin_yueru", "interval_minutes": 1, "max_nudges": 0},
        actor=CONTROLLER_ACTOR,
    )
    return {"ok": True, "created": True, "job": verified}


def start(loop_id: str) -> dict[str, Any]:
    store = SuperloopStore(SUPERLOOPS)
    loop_dir = store.loop_dir(loop_id)
    state = store.load_loop_state(loop_id)
    was_running = state.get("status") == "running"
    control = state.get("control") if isinstance(state.get("control"), dict) else {}
    joint_migration = (
        control.get("joint_migration")
        if isinstance(control.get("joint_migration"), dict)
        else {}
    )
    migration_resume_pending = (
        joint_migration.get("resume_requires_explicit_operator_action") is True
    )
    if was_running and not migration_resume_pending:
        return {"ok": True, "started": False, "reason": "already_running", "state": state}
    if state.get("status") != "paused" and not was_running:
        raise RuntimeError(f"loop must be paused before start; got {state.get('status')!r}")
    report = _custom_validation(loop_id)
    if not report.get("ok"):
        raise RuntimeError(f"loop validation failed before start: {report}")

    nudge_id = state.get("liveness", {}).get("nudge_job_id")
    nudge = SkillManager(project_root=ROOT, tasks_path=TASKS_PATH).get_job("nudge", str(nudge_id or ""))
    if not nudge or nudge.get("agent") != "lin_yueru" or not nudge.get("enabled"):
        raise RuntimeError("the required enabled controller-owned nudge is missing")
    if loop_id not in str(nudge.get("prompt")):
        raise RuntimeError("the controller nudge is not bound to this loop")

    taskboard = _load_json(loop_dir / "taskboard.json")
    first = next(task for task in taskboard if task.get("task_id") == "HD-001")
    if first.get("status") == "pending":
        first["status"] = "in_progress"
        first["started_at"] = _utc_now()
    _atomic_json(loop_dir / "taskboard.json", taskboard)
    now = _utc_now()
    state["status"] = "running"
    state["operator_execution_authority"] = {
        "status": "active",
        "scope": "campaign_until_terminal",
        "granted_at": (
            state.get("operator_execution_authority", {}).get("granted_at")
            if was_running
            else now
        )
        or now,
        "revoked_at": None,
    }
    control = dict(control)
    control.pop("pause_requested", None)
    if str(control.get("requested_action") or "").lower() in {"pause", "drain"}:
        control.pop("requested_action", None)
    if joint_migration:
        joint_migration = dict(joint_migration)
        joint_migration["resume_requires_explicit_operator_action"] = False
        joint_migration["resumed_at"] = now
        control["joint_migration"] = joint_migration
    if control:
        state["control"] = control
    else:
        state.pop("control", None)
    state.pop("pause_requested", None)
    state["current_phase"] = "preflight_and_lab"
    state["current_step"] = "HD-001"
    state["next_action"] = {"kind": "run_task", "task_id": "HD-001"}
    state["started_at"] = state.get("started_at") or now
    store.save_loop_state(loop_id, state)
    campaign = _load_json(loop_dir / "campaign.json")
    campaign["status"] = "running"
    campaign["updated_at"] = now
    _atomic_json(loop_dir / "campaign.json", campaign)
    event_type = "loop.resume_interlock_cleared" if was_running else "loop.started"
    store.append_loop_event(
        loop_id,
        event_type=event_type,
        data={"task_id": "HD-001", "joint_migration_resume": bool(joint_migration)},
        actor=CONTROLLER_ACTOR,
    )
    return {
        "ok": True,
        "started": not was_running,
        "reconciled_running_resume": was_running,
        "loop_id": loop_id,
        "nudge_job_id": nudge_id,
        "current_step": "HD-001",
    }


def complete_local_task(
    loop_id: str,
    task_id: str,
    evidence_ref: str,
    *,
    hashi_commit: str | None = None,
    hashi_build_sha256: str | None = None,
    her_source_commit: str | None = None,
    package_sha256: str | None = None,
    oracle_sha256: str | None = None,
) -> dict[str, Any]:
    """Complete one controller-owned task only after its evidence exists and passes."""

    store = SuperloopStore(SUPERLOOPS)
    loop_dir = store.loop_dir(loop_id).resolve()
    evidence_path = store.resolve_loop_path(loop_id, evidence_ref, evidence_ref).resolve()
    if loop_dir not in evidence_path.parents or not evidence_path.is_file():
        raise ValueError("evidence_ref must name an existing file inside the loop directory")
    evidence = _load_json(evidence_path)
    if not isinstance(evidence, dict) or evidence.get("verdict") != "PASS":
        raise ValueError("local task evidence must be a JSON object with verdict=PASS")

    supplied_identity = (
        hashi_commit,
        hashi_build_sha256,
        her_source_commit,
        package_sha256,
        oracle_sha256,
    )
    if any(supplied_identity) and not all(supplied_identity):
        raise ValueError(
            "candidate identity requires HASHI commit/build SHA-256, HER source "
            "commit/package SHA-256, and oracle SHA-256 together"
        )

    taskboard_path = loop_dir / "taskboard.json"
    tasks = _load_json(taskboard_path)
    task = next((item for item in tasks if item.get("task_id") == task_id), None)
    if task is None:
        raise ValueError(f"unknown task: {task_id}")
    if task.get("execution_mode") != "local_self" or task.get("owner_agent") != "lin_yueru":
        raise ValueError(f"task {task_id} is not a controller-owned local task")
    if task.get("status") != "in_progress":
        raise ValueError(f"task {task_id} must be in_progress, got {task.get('status')!r}")
    if all(supplied_identity) and task_id != "HD-001":
        raise ValueError("only HD-001 may bind a proposed composite candidate")

    now = _utc_now()
    required = [str(item) for item in task.get("required_evidence", [])]
    task["artifact_refs"] = list(dict.fromkeys([*task.get("artifact_refs", []), evidence_ref]))
    task["evidence_refs"] = list(dict.fromkeys([*task.get("evidence_refs", []), evidence_ref]))
    task["completion_evidence"] = {name: evidence_ref for name in required}
    task["status"] = "completed"
    task["updated_at"] = now
    task["completed_at"] = now
    _atomic_json(taskboard_path, tasks)

    state = store.load_loop_state(loop_id)
    if all(supplied_identity):
        assert (
            hashi_commit is not None
            and hashi_build_sha256 is not None
            and her_source_commit is not None
            and package_sha256 is not None
            and oracle_sha256 is not None
        )
        state["candidate"] = {
            "hash": _candidate_fingerprint(
                hashi_commit=hashi_commit,
                hashi_build_sha256=hashi_build_sha256,
                her_source_commit=her_source_commit,
                package_sha256=package_sha256,
                oracle_sha256=oracle_sha256,
            ),
            "hashi_commit": hashi_commit,
            "hashi_build_sha256": hashi_build_sha256,
            "her_source_commit": her_source_commit,
            "package_sha256": package_sha256,
            "oracle_sha256": oracle_sha256,
            "evidence_valid": None,
            "freeze_status": "joint_layer_a_validation_pending",
            "proposed_at": now,
            "proposal_evidence_ref": evidence_ref,
        }
        store.save_loop_state(loop_id, state)
    store.refresh_loop_stats(loop_id)
    store.append_loop_event(
        loop_id,
        event_type="task.completed",
        data={"task_id": task_id, "evidence_ref": evidence_ref},
        actor=CONTROLLER_ACTOR,
    )

    advanced = SuperloopRunner(store).next_action(loop_id)
    state = store.load_loop_state(loop_id)
    current_id = state.get("current_step")
    current_task = next((item for item in _load_json(taskboard_path) if item.get("task_id") == current_id), None)
    if current_task is not None:
        state["current_phase"] = current_task.get("phase")
        action_kind = "dispatch_task" if current_task.get("execution_mode") == "hchat_agent" else "run_task"
        state["next_action"] = {"kind": action_kind, "task_id": current_id}
        store.save_loop_state(loop_id, state)
    return {
        "ok": True,
        "loop_id": loop_id,
        "completed_task": task_id,
        "evidence_ref": evidence_ref,
        "candidate": state.get("candidate"),
        "advanced": advanced,
        "current_step": state.get("current_step"),
        "next_action": state.get("next_action"),
    }


def _layer_a_habit_evidence_errors(evidence: dict[str, Any]) -> list[str]:
    habit = evidence.get("habit_evidence")
    if not isinstance(habit, dict):
        return ["habit_evidence must be an object"]
    errors: list[str] = []
    for claim in ("formation", "retrieval", "behavioral_use"):
        if habit.get(f"{claim}_observed") is not True:
            errors.append(f"{claim}_observed must be true")
        refs = habit.get(f"{claim}_evidence_refs")
        if not isinstance(refs, list) or not any(str(ref).strip() for ref in refs):
            errors.append(f"{claim}_evidence_refs must be non-empty")
    for verdict in (
        "on_off_output_and_tool_side_effects",
        "irrelevant_habit_prompt_noop",
        "conflicting_habit_subordination",
        "background_timeout_recovery",
        "restart_backlog_over_batch_limit",
    ):
        if habit.get(verdict) != "PASS":
            errors.append(f"{verdict} must be PASS")
    if habit.get("no_change_claim_limit_acknowledged") is not True:
        errors.append("no_change_claim_limit_acknowledged must be true")
    measured = habit.get("foreground_lock_wait_ms")
    limit = habit.get("foreground_lock_wait_limit_ms")
    if not all(
        isinstance(value, (int, float)) and not isinstance(value, bool)
        for value in (measured, limit)
    ) or measured < 0 or limit <= 0 or measured > limit:
        errors.append("foreground lock wait must be measured within its positive limit")
    return errors


def complete_layer_a_task(
    loop_id: str,
    evidence_ref: str,
) -> dict[str, Any]:
    """Verify joint Layer A and make the proposed candidate release-valid."""

    store = SuperloopStore(SUPERLOOPS)
    loop_dir = store.loop_dir(loop_id).resolve()
    evidence_path = store.resolve_loop_path(loop_id, evidence_ref, evidence_ref).resolve()
    if loop_dir not in evidence_path.parents or not evidence_path.is_file():
        raise ValueError("evidence_ref must name an existing file inside the loop directory")
    evidence = _load_json(evidence_path)
    if not isinstance(evidence, dict) or evidence.get("verdict") != "PASS":
        raise ValueError("Layer A evidence must be a JSON object with verdict=PASS")

    state = store.load_loop_state(loop_id)
    candidate = deepcopy(state.get("candidate") or {})
    if not candidate.get("hash") or candidate.get("evidence_valid") is not None:
        raise ValueError("Layer A requires one proposed, not-yet-valid composite candidate")
    if candidate.get("freeze_status") != "joint_layer_a_validation_pending":
        raise ValueError("candidate is not awaiting joint Layer A validation")
    if evidence.get("candidate_hash") != candidate.get("hash"):
        raise ValueError("Layer A evidence does not match the proposed candidate hash")
    habit_errors = _layer_a_habit_evidence_errors(evidence)
    if habit_errors:
        raise ValueError(
            "Layer A Habit evidence is incomplete: " + "; ".join(habit_errors)
        )

    taskboard_path = loop_dir / "taskboard.json"
    tasks = _load_json(taskboard_path)
    task = next((item for item in tasks if item.get("task_id") == "HD-002"), None)
    if task is None or task.get("status") != "in_progress":
        raise ValueError("HD-002 must be in_progress before Layer A can be verified")

    now = _utc_now()
    task["status"] = "completed"
    task["updated_at"] = now
    task["completed_at"] = now
    task["artifact_refs"] = list(
        dict.fromkeys([*task.get("artifact_refs", []), evidence_ref])
    )
    task["evidence_refs"] = list(
        dict.fromkeys([*task.get("evidence_refs", []), evidence_ref])
    )
    task["completion_evidence"] = {
        str(name): evidence_ref for name in task.get("required_evidence", [])
    }
    _atomic_json(taskboard_path, tasks)

    candidate.update(
        {
            "evidence_valid": True,
            "freeze_status": "frozen_after_joint_layer_a",
            "frozen_at": now,
            "evidence_ref": evidence_ref,
        }
    )
    state["candidate"] = candidate
    state.setdefault("gates", {})["layer_a_offline"] = "passed"
    store.save_loop_state(loop_id, state)
    store.refresh_loop_stats(loop_id)
    store.append_loop_event(
        loop_id,
        event_type="candidate.frozen_after_joint_layer_a",
        data={
            "task_id": "HD-002",
            "candidate_hash": candidate["hash"],
            "evidence_ref": evidence_ref,
        },
        actor=CONTROLLER_ACTOR,
    )

    advanced = SuperloopRunner(store).next_action(loop_id)
    state = store.load_loop_state(loop_id)
    current_id = state.get("current_step")
    current_task = next(
        (
            item
            for item in _load_json(taskboard_path)
            if item.get("task_id") == current_id
        ),
        None,
    )
    if current_task is not None:
        state["current_phase"] = current_task.get("phase")
        action_kind = (
            "dispatch_task"
            if current_task.get("execution_mode") == "hchat_agent"
            else "run_task"
        )
        state["next_action"] = {"kind": action_kind, "task_id": current_id}
        store.save_loop_state(loop_id, state)
    return {
        "ok": True,
        "loop_id": loop_id,
        "completed_task": "HD-002",
        "evidence_ref": evidence_ref,
        "candidate": state.get("candidate"),
        "advanced": advanced,
        "current_step": state.get("current_step"),
        "next_action": state.get("next_action"),
    }


def status(loop_id: str) -> dict[str, Any]:
    loop_dir = SUPERLOOPS / "loops" / loop_id
    state = _load_json(loop_dir / "state.json")
    taskboard = _load_json(loop_dir / "taskboard.json")
    work_items = _load_json(loop_dir / "work_items.json")
    statuses = (
        "locked",
        "pending",
        "in_progress",
        "passed",
        "failed",
        "inconclusive",
    )
    core_items = [
        item for item in work_items if item.get("feature_profile") == CORE_OFF
    ]
    return {
        "ok": True,
        "loop_id": loop_id,
        "status": state.get("status"),
        "terminal_result": state.get("terminal_result"),
        "current_phase": state.get("current_phase"),
        "current_step": state.get("current_step"),
        "next_action": state.get("next_action"),
        "nudge_job_id": state.get("liveness", {}).get("nudge_job_id"),
        "tasks": {task.get("task_id"): task.get("status") for task in taskboard},
        "cells": {
            value: sum(item.get("status") == value for item in core_items)
            for value in statuses
        },
        "work_items": {
            value: sum(item.get("status") == value for item in work_items)
            for value in statuses
        },
        "profiles": {
            "core_off": len(core_items),
            "habit_wire": sum(
                item.get("habit_scenario") == HABIT_WIRE for item in work_items
            ),
            "habit_deep": sum(
                item.get("habit_scenario") == HABIT_DEEP for item in work_items
            ),
            "habit_fault": sum(
                item.get("habit_scenario") == HABIT_FAULT for item in work_items
            ),
        },
        "validation": _custom_validation(loop_id),
    }


def latest_loop() -> str:
    candidates: list[tuple[float, str]] = []
    for path in (SUPERLOOPS / "loops").glob("sl-*"):
        state_path = path / "state.json"
        try:
            state = _load_json(state_path)
        except Exception:
            continue
        if state.get("template") == "her_debug":
            candidates.append((state_path.stat().st_mtime, path.name))
    if not candidates:
        raise RuntimeError("no instantiated her_debug loop exists")
    return max(candidates)[1]


def _resolve_loop_id(raw: str | None) -> str:
    value = str(raw or "").strip()
    if not value or value == "latest":
        return latest_loop()
    if not re.fullmatch(r"sl-[A-Za-z0-9_-]+", value):
        raise ValueError(f"invalid loop id: {value!r}")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("instantiate")
    for command in (
        "create-nudge",
        "migrate-joint",
        "start",
        "validate",
        "status",
    ):
        sub = subparsers.add_parser(command)
        sub.add_argument("loop_id", nargs="?", default="latest")
    complete = subparsers.add_parser("complete-local")
    complete.add_argument("loop_id")
    complete.add_argument("task_id")
    complete.add_argument("evidence_ref")
    complete.add_argument("--hashi-commit")
    complete.add_argument("--hashi-build-sha256")
    complete.add_argument("--her-source-commit")
    complete.add_argument("--package-sha256")
    complete.add_argument("--oracle-sha256")
    layer_a = subparsers.add_parser("complete-layer-a")
    layer_a.add_argument("loop_id")
    layer_a.add_argument("evidence_ref")
    args = parser.parse_args(argv)
    try:
        if args.command == "instantiate":
            result = instantiate()
        else:
            loop_id = _resolve_loop_id(args.loop_id)
            if args.command == "create-nudge":
                result = create_nudge(loop_id)
            elif args.command == "migrate-joint":
                result = migrate_joint_campaign(loop_id)
            elif args.command == "start":
                result = start(loop_id)
            elif args.command == "validate":
                result = _custom_validation(loop_id)
            elif args.command == "complete-local":
                result = complete_local_task(
                    loop_id,
                    args.task_id,
                    args.evidence_ref,
                    hashi_commit=args.hashi_commit,
                    hashi_build_sha256=args.hashi_build_sha256,
                    her_source_commit=args.her_source_commit,
                    package_sha256=args.package_sha256,
                    oracle_sha256=args.oracle_sha256,
                )
            elif args.command == "complete-layer-a":
                result = complete_layer_a_task(
                    loop_id,
                    args.evidence_ref,
                )
            else:
                result = status(loop_id)
    except Exception as exc:
        print(json.dumps({"ok": False, "error_type": type(exc).__name__, "error": str(exc)}, ensure_ascii=False, indent=2))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
