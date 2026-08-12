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


def _candidate_fingerprint(*, hashi_commit: str, her_source_commit: str, package_sha256: str) -> str:
    identity = {
        "hashi_commit": hashi_commit,
        "her_source_commit": her_source_commit,
        "package_sha256": package_sha256,
    }
    encoded = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _effort_code(effort: str) -> str:
    return effort.upper().replace("+", "PLUS")


def _provider_code(provider: str) -> str:
    return "DS" if provider == "official_deepseek" else "OR"


def _presentation_runs(cell_id: str) -> list[dict[str, Any]]:
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
                        "status": "pending",
                        "attempt_refs": [],
                    }
                )
    return runs


def _build_work_items() -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for stage_index, (stage_id, model_code, model_map) in enumerate(STAGES, start=1):
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
                            "status": "locked" if stage_id == "stage_2_pro" else "pending",
                            "candidate_hash": None,
                            "scenario_groups": [
                                {
                                    "scenario_id": scenario,
                                    "packet_key": f"{stage_id}/{provider}/{model_map[provider]}/{mode}/{effort}/scenario/{scenario}/default",
                                    "status": "locked" if stage_id == "stage_2_pro" else "pending",
                                    "attempt_refs": [],
                                }
                                for scenario in SCENARIOS
                            ],
                            "presentation_runs": _presentation_runs(cell_id),
                            "native_boundary": {"status": "locked" if stage_id == "stage_2_pro" else "pending", "attempt_refs": []},
                            "cold_exactness_repeats": [
                                {"repeat": repeat, "status": "locked" if stage_id == "stage_2_pro" else "pending", "attempt_refs": []}
                                for repeat in range(1, 4)
                            ],
                            "warm_repeat": {"status": "locked" if stage_id == "stage_2_pro" else "pending", "attempt_refs": []},
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
    require(state.get("owner_agent") == "lin_yueru", "owner", "Controller owner must be lin_yueru.")
    require(state.get("scheduler_auto_advance") is False, "scheduler", "Scheduler auto-advance must stay disabled.")
    require(len(work_items) == 48, "cell_count", "Exactly 48 core cells must be expanded.")
    require(len({item.get("work_item_id") for item in work_items}) == 48, "cell_ids", "Core cell IDs must be unique.")
    require(sum(len(item.get("scenario_groups", [])) for item in work_items) == 480, "scenario_count", "Exactly 480 scenario groups must be expanded.")
    require(sum(len(item.get("presentation_runs", [])) for item in work_items) == 384, "presentation_count", "Exactly 384 presentation runs must be expanded.")
    require(sum(item.get("stage") == "stage_1_flash" for item in work_items) == 24, "flash_count", "Stage 1 must contain 24 cells.")
    require(sum(item.get("stage") == "stage_2_pro" for item in work_items) == 24, "pro_count", "Stage 2 must contain 24 cells.")
    for item in work_items:
        stage_models = dict(STAGES[0][2] if item.get("stage") == "stage_1_flash" else STAGES[1][2])
        require(item.get("model") == stage_models.get(item.get("provider")), "route_model", f"Illegal model for {item.get('work_item_id')}.")
        if item.get("stage") == "stage_2_pro" and state.get("gates", {}).get("stage_1_flash") != "passed":
            require(item.get("status") == "locked", "pro_lock", f"Pro cell is unlocked early: {item.get('work_item_id')}.")
    require(len(tasks) == 10, "task_count", "The phase taskboard must contain ten tasks.")
    require(campaign.get("expected_counts", {}).get("total_core_cells") == 48, "campaign_count", "Campaign count changed.")
    require(
        not _in_progress_packet_start_authority_conflict(state, tasks),
        "in_progress_packet_authority_livelock",
        "The current in-progress task has an unstarted packet that incorrectly requires fresh start authority.",
    )

    generic = validate_loop(SuperloopStore(SUPERLOOPS), loop_id, closeout=False)
    require(generic.get("summary", {}).get("errors", 0) == 0, "generic_validation", "Generic Superloop validation has errors.")
    return {
        "ok": not findings,
        "loop_id": loop_id,
        "core_cells": len(work_items),
        "scenario_groups": sum(len(item.get("scenario_groups", [])) for item in work_items),
        "presentation_runs": sum(len(item.get("presentation_runs", [])) for item in work_items),
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
    _atomic_json(loop_dir / "template_snapshot.json", {"captured_at": now, "files": _template_hashes()})
    (loop_dir / "dispatches.jsonl").touch(exist_ok=False)
    (loop_dir / "attempts").mkdir()
    (loop_dir / "evidence").mkdir()
    policy = (TEMPLATE / "liveness_nudge.template.md").read_text(encoding="utf-8")
    (loop_dir / "controller_policy.md").write_text(policy.replace("{{LOOP_ID}}", loop_id), encoding="utf-8")
    store.append_loop_event(
        loop_id,
        event_type="campaign.expanded",
        data={"core_cells": 48, "scenario_groups": 480, "presentation_runs": 384},
        actor=CONTROLLER_ACTOR,
    )
    report = _custom_validation(loop_id)
    _atomic_json(loop_dir / "activation_validation.json", report)
    if not report.get("ok"):
        raise RuntimeError(f"instantiated loop failed validation: {report}")
    return {"ok": True, "loop_id": loop_id, "campaign_id": campaign_id, "status": "paused", "validation": report}


def create_nudge(loop_id: str) -> dict[str, Any]:
    loop_dir = SUPERLOOPS / "loops" / loop_id
    state = _load_json(loop_dir / "state.json")
    existing_id = state.get("liveness", {}).get("nudge_job_id")
    manager = SkillManager(project_root=ROOT, tasks_path=TASKS_PATH)
    if existing_id:
        existing = manager.get_job("nudge", str(existing_id))
        if existing:
            return {"ok": True, "created": False, "job": existing}

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
    if state.get("status") == "running":
        return {"ok": True, "started": False, "reason": "already_running", "state": state}
    if state.get("status") != "paused":
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
    state["status"] = "running"
    state["operator_execution_authority"] = {
        "status": "active",
        "scope": "campaign_until_terminal",
        "granted_at": _utc_now(),
        "revoked_at": None,
    }
    state["current_phase"] = "preflight_and_lab"
    state["current_step"] = "HD-001"
    state["next_action"] = {"kind": "run_task", "task_id": "HD-001"}
    state["started_at"] = _utc_now()
    store.save_loop_state(loop_id, state)
    store.append_loop_event(loop_id, event_type="loop.started", data={"task_id": "HD-001"}, actor=CONTROLLER_ACTOR)
    return {"ok": True, "started": True, "loop_id": loop_id, "nudge_job_id": nudge_id, "current_step": "HD-001"}


def complete_local_task(
    loop_id: str,
    task_id: str,
    evidence_ref: str,
    *,
    hashi_commit: str | None = None,
    her_source_commit: str | None = None,
    package_sha256: str | None = None,
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

    supplied_identity = (hashi_commit, her_source_commit, package_sha256)
    if any(supplied_identity) and not all(supplied_identity):
        raise ValueError("candidate identity requires HASHI commit, HER source commit, and package SHA-256 together")

    taskboard_path = loop_dir / "taskboard.json"
    tasks = _load_json(taskboard_path)
    task = next((item for item in tasks if item.get("task_id") == task_id), None)
    if task is None:
        raise ValueError(f"unknown task: {task_id}")
    if task.get("execution_mode") != "local_self" or task.get("owner_agent") != "lin_yueru":
        raise ValueError(f"task {task_id} is not a controller-owned local task")
    if task.get("status") != "in_progress":
        raise ValueError(f"task {task_id} must be in_progress, got {task.get('status')!r}")

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
        assert hashi_commit is not None and her_source_commit is not None and package_sha256 is not None
        state["candidate"] = {
            "hash": _candidate_fingerprint(
                hashi_commit=hashi_commit,
                her_source_commit=her_source_commit,
                package_sha256=package_sha256,
            ),
            "hashi_commit": hashi_commit,
            "her_source_commit": her_source_commit,
            "package_sha256": package_sha256,
            "evidence_valid": True,
            "frozen_at": now,
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


def status(loop_id: str) -> dict[str, Any]:
    loop_dir = SUPERLOOPS / "loops" / loop_id
    state = _load_json(loop_dir / "state.json")
    taskboard = _load_json(loop_dir / "taskboard.json")
    work_items = _load_json(loop_dir / "work_items.json")
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
            value: sum(item.get("status") == value for item in work_items)
            for value in ("locked", "pending", "in_progress", "passed", "failed", "inconclusive")
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
    for command in ("create-nudge", "start", "validate", "status"):
        sub = subparsers.add_parser(command)
        sub.add_argument("loop_id", nargs="?", default="latest")
    complete = subparsers.add_parser("complete-local")
    complete.add_argument("loop_id")
    complete.add_argument("task_id")
    complete.add_argument("evidence_ref")
    complete.add_argument("--hashi-commit")
    complete.add_argument("--her-source-commit")
    complete.add_argument("--package-sha256")
    args = parser.parse_args(argv)
    try:
        if args.command == "instantiate":
            result = instantiate()
        else:
            loop_id = _resolve_loop_id(args.loop_id)
            if args.command == "create-nudge":
                result = create_nudge(loop_id)
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
                    her_source_commit=args.her_source_commit,
                    package_sha256=args.package_sha256,
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
