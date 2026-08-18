from __future__ import annotations

import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "superloops" / "templates" / "her_debug"


def _load_json(name: str):
    return json.loads((TEMPLATE / name).read_text(encoding="utf-8"))


def _validator_module():
    path = TEMPLATE / "validate_template.py"
    spec = importlib.util.spec_from_file_location("her_debug_validate_template", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _controller_module():
    path = ROOT / "scripts" / "her_debug_superloop.py"
    spec = importlib.util.spec_from_file_location("her_debug_superloop", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _complete_habit_layer_a_evidence() -> dict:
    return {
        "formation_observed": True,
        "formation_evidence_refs": ["lifecycle.json#formation"],
        "retrieval_observed": True,
        "retrieval_evidence_refs": ["lifecycle.json#retrieval"],
        "behavioral_use_observed": True,
        "behavioral_use_evidence_refs": ["lifecycle.json#behavioral-use"],
        "on_off_output_and_tool_side_effects": "PASS",
        "irrelevant_habit_prompt_noop": "PASS",
        "conflicting_habit_subordination": "PASS",
        "background_timeout_recovery": "PASS",
        "restart_backlog_over_batch_limit": "PASS",
        "no_change_claim_limit_acknowledged": True,
        "foreground_lock_wait_ms": 52.0,
        "foreground_lock_wait_limit_ms": 750.0,
    }


def test_template_validator_accepts_the_checked_in_design() -> None:
    report = _validator_module().validate_template(TEMPLATE)

    assert report == {
        "ok": True,
        "template": "her_debug",
        "stage_1_core_cells": 12,
        "total_core_cells": 12,
        "total_habit_wire_cells": 12,
        "total_habit_deep_cells": 2,
        "total_habit_fault_cells": 1,
        "total_live_work_items": 27,
        "finding_count": 0,
        "findings": [],
    }


def test_campaign_uses_only_official_flash_and_forbids_fallbacks() -> None:
    campaign = _load_json("campaign.template.json")
    stages = {stage["stage_id"]: stage for stage in campaign["stages"]}

    assert campaign["joint_campaign_version"] == 3
    assert stages["stage_1_flash"]["allowed_live_models"] == {
        "official_deepseek": "deepseek-v4-flash",
    }
    assert set(stages) == {"stage_1_flash"}
    assert campaign["expected_counts"]["total_core_cells"] == 12
    assert campaign["expected_counts"]["total_live_work_items"] == 27
    assert {"feature_profile", "habit_scenario"}.issubset(
        campaign["work_item_key_fields"]
    )
    assert all(value is False for value in campaign["fallback_policy"].values())


def test_nudge_belongs_to_controller_and_cannot_interrupt_ajiao() -> None:
    state = _load_json("state.template.json")
    nudge = (TEMPLATE / "liveness_nudge.template.md").read_text(encoding="utf-8")
    liveness = state["liveness"]

    assert liveness["nudge_owner_agent"] == "lin_yueru"
    assert liveness["observed_worker_agent"] == "ajiao"
    assert liveness["may_interrupt_running_worker"] is False
    assert liveness["may_dispatch_duplicate_active_packet"] is False
    assert liveness["may_continue_packets_for_in_progress_task"] is True
    assert liveness["must_not_require_new_start_authority_for_in_progress_task_packet"] is True
    assert liveness["must_follow_up_nonterminal_failure"] is False
    assert liveness["must_surface_stagnation"] is True
    assert liveness["stagnation_observation_limit"] == 1
    assert liveness["wait_requires_concrete_external_blocker"] is True
    assert liveness["idle_eligible_packet_dispatches_immediately"] is True
    assert liveness["operator_pause_requires_explicit_resume"] is True
    assert liveness["controller_transient_drain_auto_resumes"] is True
    assert liveness["max_nudges"] == 0
    assert "This idle nudge wakes `lin_yueru`" in nudge
    assert "It is not a nudge\nfor Ajiao" in nudge
    assert "If Ajiao is `running`, do not `/stop`" in nudge
    assert "A failed reply is not a campaign terminal result" in nudge
    assert '"pending task" means a phase task whose taskboard status is `pending`' in nudge
    assert "Do not set or retain `pending_non_nudge_start_authority`" in nudge
    assert "A controller-owned transient drain is not an operator pause" in nudge


def test_in_progress_packet_continuation_cannot_require_new_start_authority() -> None:
    controller = _controller_module()
    tasks = [{"task_id": "HD-003", "status": "in_progress"}]
    state = {
        "current_step": "HD-003",
        "active_dispatch_id": None,
        "active_wait_id": None,
        "selected_next_packet": {
            "work_item_id": "HER-LIVE-DS-FLASH-FIXED-LOW",
            "scenario": "C00",
            "started": False,
            "pending_non_nudge_start_authority": True,
        },
        "next_action": {
            "kind": "await_non_nudge_start_authority",
            "pending_non_nudge_start_authority": True,
        },
    }

    assert controller._in_progress_packet_start_authority_conflict(state, tasks) is True

    state["operator_execution_authority"] = {
        "status": "active",
        "scope": "campaign_until_terminal",
    }
    state["selected_next_packet"]["pending_non_nudge_start_authority"] = False
    state["next_action"] = {"kind": "dispatch_selected_packet"}

    assert controller._in_progress_packet_start_authority_conflict(state, tasks) is False

    tasks[0]["status"] = "pending"
    state["selected_next_packet"]["pending_non_nudge_start_authority"] = True
    state["next_action"] = {"kind": "await_non_nudge_start_authority"}

    assert controller._in_progress_packet_start_authority_conflict(state, tasks) is False


def test_null_selected_packet_cannot_hide_idle_in_progress_livelock() -> None:
    controller = _controller_module()
    tasks = [{"task_id": "HD-003", "status": "in_progress"}]
    campaign = {"status": "running"}
    state = {
        "status": "paused",
        "current_step": "HD-003",
        "active_dispatch_id": None,
        "active_request_id": None,
        "active_wait_id": None,
        "selected_next_packet": None,
        "operator_execution_authority": {"status": "active"},
        "next_action": {"kind": "await_operator_resume"},
        "control": {
            "pause": {
                "source": "controller_freeze_guard",
                "drain_complete": True,
            }
        },
    }

    assert controller._in_progress_idle_control_livelock(
        state, campaign, tasks
    ) is True

    state["control"]["pause"].update(
        {
            "kind": "operator_pause",
            "resume_policy": "explicit_operator",
        }
    )
    assert controller._in_progress_idle_control_livelock(
        state, campaign, tasks
    ) is False

    state["status"] = "running"
    state["control"] = {}
    state["next_action"] = {"kind": "await_non_nudge_start_authority"}
    assert controller._in_progress_idle_control_livelock(
        state, campaign, tasks
    ) is True

    state["next_action"] = {"kind": "dispatch_task", "task_id": "HD-003"}
    assert controller._in_progress_idle_control_livelock(
        state, campaign, tasks
    ) is False


def test_validation_rejects_state_campaign_status_mismatch(tmp_path: Path) -> None:
    controller = _controller_module()
    controller.SUPERLOOPS = tmp_path / "superloops"
    created = controller.instantiate()
    loop_id = created["loop_id"]
    loop_dir = controller.SUPERLOOPS / "loops" / loop_id
    campaign = json.loads((loop_dir / "campaign.json").read_text(encoding="utf-8"))
    campaign["status"] = "running"
    controller._atomic_json(loop_dir / "campaign.json", campaign)

    report = controller._custom_validation(loop_id)

    assert report["ok"] is False
    assert "state_campaign_status_mismatch" in {
        finding["code"] for finding in report["findings"]
    }

def test_joint_migration_start_clears_pause_interlock(tmp_path: Path) -> None:
    controller = _controller_module()
    controller.SUPERLOOPS = tmp_path / "superloops"
    controller.TASKS_PATH = tmp_path / "tasks.json"
    created = controller.instantiate()
    loop_id = created["loop_id"]
    controller.create_nudge(loop_id)
    loop_dir = controller.SUPERLOOPS / "loops" / loop_id

    state = json.loads((loop_dir / "state.json").read_text(encoding="utf-8"))
    state["control"] = {
        "requested_action": "pause",
        "pause_requested": True,
        "joint_migration": {
            "status": "complete",
            "resume_requires_explicit_operator_action": True,
        },
    }
    state["operator_execution_authority"] = {
        "status": "suspended_pending_explicit_resume",
        "scope": "campaign_until_terminal",
    }
    controller._atomic_json(loop_dir / "state.json", state)
    campaign = json.loads((loop_dir / "campaign.json").read_text(encoding="utf-8"))
    campaign["status"] = "paused"
    controller._atomic_json(loop_dir / "campaign.json", campaign)

    result = controller.start(loop_id)

    assert result["started"] is True
    resumed_state = json.loads((loop_dir / "state.json").read_text(encoding="utf-8"))
    control = resumed_state["control"]
    assert resumed_state["status"] == "running"
    assert resumed_state["operator_execution_authority"]["status"] == "active"
    assert "requested_action" not in control
    assert "pause_requested" not in control
    assert control["joint_migration"]["resume_requires_explicit_operator_action"] is False
    assert control["joint_migration"]["resumed_at"]
    resumed_campaign = json.loads(
        (loop_dir / "campaign.json").read_text(encoding="utf-8")
    )
    assert resumed_campaign["status"] == "running"


def test_create_nudge_refresh_reenables_existing_controller(tmp_path: Path) -> None:
    controller = _controller_module()
    controller.SUPERLOOPS = tmp_path / "superloops"
    controller.TASKS_PATH = tmp_path / "tasks.json"
    created = controller.instantiate()
    loop_id = created["loop_id"]
    first = controller.create_nudge(loop_id)
    nudge_id = first["job"]["id"]
    payload = json.loads(controller.TASKS_PATH.read_text(encoding="utf-8"))
    persisted = next(item for item in payload["nudges"] if item["id"] == nudge_id)
    persisted["enabled"] = False
    controller._atomic_json(controller.TASKS_PATH, payload)

    refreshed = controller.create_nudge(loop_id)

    assert refreshed["created"] is False
    assert refreshed["refreshed"] is True
    assert refreshed["job"]["enabled"] is True


def test_final_gate_depends_directly_on_complete_flash_gate() -> None:
    tasks = {task["task_id"]: task for task in _load_json("taskboard.template.json")}

    assert tasks["HD-006"]["phase"] == "stage_1_flash_gate"
    assert "HD-007" not in tasks
    assert "HD-008" not in tasks
    assert tasks["HD-009"]["depends_on"] == ["HD-006"]
    assert all(task["status"] == "pending" for task in tasks.values())


def test_only_pass_or_confirmed_funds_exhaustion_is_terminal() -> None:
    state = _load_json("state.template.json")
    campaign = _load_json("campaign.template.json")

    assert state["liveness"]["terminal_conditions"] == ["PASSED", "BLOCKED_FUNDS"]
    assert state["terminal_state_mapping"] == {
        "PASSED": "completed",
        "BLOCKED_FUNDS": "blocked",
    }
    assert campaign["funds_policy"]["required_live_routes"] == [
        "official_deepseek",
    ]
    assert campaign["funds_policy"][
        "terminal_on_confirmed_insufficient_funds_from_any_required_route"
    ] is True


def test_candidate_becomes_valid_only_after_joint_layer_a_passes(tmp_path: Path) -> None:
    controller = _controller_module()
    controller.SUPERLOOPS = tmp_path / "superloops"
    created = controller.instantiate()
    loop_id = created["loop_id"]
    loop_dir = controller.SUPERLOOPS / "loops" / loop_id

    state = json.loads((loop_dir / "state.json").read_text(encoding="utf-8"))
    state.update({"status": "running", "current_step": "HD-001"})
    controller._atomic_json(loop_dir / "state.json", state)
    tasks = json.loads((loop_dir / "taskboard.json").read_text(encoding="utf-8"))
    tasks[0]["status"] = "in_progress"
    controller._atomic_json(loop_dir / "taskboard.json", tasks)
    evidence = loop_dir / "evidence" / "HD-001-evidence.json"
    controller._atomic_json(evidence, {"verdict": "PASS"})

    result = controller.complete_local_task(
        loop_id,
        "HD-001",
        f"superloops/loops/{loop_id}/evidence/{evidence.name}",
        hashi_commit="a" * 40,
        hashi_build_sha256="d" * 64,
        her_source_commit="b" * 40,
        package_sha256="c" * 64,
        oracle_sha256="e" * 64,
    )

    assert result["current_step"] == "HD-002"
    assert result["next_action"] == {"kind": "dispatch_task", "task_id": "HD-002"}
    assert result["candidate"]["evidence_valid"] is None
    assert result["candidate"]["freeze_status"] == "joint_layer_a_validation_pending"
    assert result["candidate"]["hashi_build_sha256"] == "d" * 64
    assert result["candidate"]["oracle_sha256"] == "e" * 64
    updated_tasks = json.loads((loop_dir / "taskboard.json").read_text(encoding="utf-8"))
    assert updated_tasks[0]["status"] == "completed"
    assert updated_tasks[1]["status"] == "in_progress"

    layer_a_evidence = loop_dir / "evidence" / "HD-002-evidence.json"
    controller._atomic_json(
        layer_a_evidence,
        {
            "verdict": "PASS",
            "candidate_hash": result["candidate"]["hash"],
            "habit_evidence": _complete_habit_layer_a_evidence(),
        },
    )
    layer_a_result = controller.complete_layer_a_task(
        loop_id,
        f"superloops/loops/{loop_id}/evidence/{layer_a_evidence.name}",
    )

    assert layer_a_result["candidate"]["evidence_valid"] is True
    assert layer_a_result["candidate"]["freeze_status"] == "frozen_after_joint_layer_a"
    assert layer_a_result["current_step"] == "HD-003"
    assert layer_a_result["next_action"] == {
        "kind": "dispatch_task",
        "task_id": "HD-003",
    }
    final_state = json.loads((loop_dir / "state.json").read_text(encoding="utf-8"))
    assert final_state["gates"]["layer_a_offline"] == "passed"


def test_joint_migration_invalidates_legacy_candidate_and_expands_habit_tracks(
    tmp_path: Path,
) -> None:
    controller = _controller_module()
    controller.SUPERLOOPS = tmp_path / "superloops"
    created = controller.instantiate()
    loop_id = created["loop_id"]
    loop_dir = controller.SUPERLOOPS / "loops" / loop_id

    # Collapse the freshly instantiated fixture to the legacy 48-cell shape.
    campaign = json.loads((loop_dir / "campaign.json").read_text(encoding="utf-8"))
    campaign["joint_campaign_version"] = 1
    campaign["expected_counts"] = {
        "stage_1_core_cells": 24,
        "stage_2_core_cells": 24,
        "total_core_cells": 48,
        "scenario_groups_per_cell": 10,
        "total_core_scenario_groups": 480,
        "presentation_variants_per_cell": 8,
        "total_presentation_runs": 384,
    }
    campaign["progress"]["route_waits"] = [
        {
            "wait_id": "wait-legacy-route",
            "status": "pending",
            "provider": "openrouter",
        }
    ]
    controller._atomic_json(loop_dir / "campaign.json", campaign)
    items = json.loads((loop_dir / "work_items.json").read_text(encoding="utf-8"))
    legacy_items = [item for item in items if item["feature_profile"] == "core_off"]
    for item in legacy_items:
        item.pop("feature_profile")
        item.pop("habit_scenario")
    legacy_items[0]["status"] = "in_progress"
    legacy_items[0]["candidate_hash"] = "1" * 64
    legacy_items[0]["evidence_refs"] = ["historical/verdict.json"]
    controller._atomic_json(loop_dir / "work_items.json", legacy_items)
    state = json.loads((loop_dir / "state.json").read_text(encoding="utf-8"))
    state["candidate"] = {
        "hash": "1" * 64,
        "hashi_commit": "a" * 40,
        "her_source_commit": "b" * 40,
        "package_sha256": "c" * 64,
        "evidence_valid": True,
    }
    controller._atomic_json(loop_dir / "state.json", state)
    controller._atomic_json(
        loop_dir / "waits.json",
        [
            {
                "wait_id": "wait-legacy-route",
                "kind": "provider_authentication",
                "status": "pending",
                "details": {"task_id": "HD-003", "provider": "openrouter"},
            }
        ],
    )

    result = controller.migrate_joint_campaign(loop_id)

    assert result["migrated"] is True
    assert result["validation"]["ok"] is True
    assert result["validation"]["core_cells"] == 12
    assert result["validation"]["habit_wire_cells"] == 12
    assert result["validation"]["habit_deep_cells"] == 2
    assert result["validation"]["habit_fault_cells"] == 1
    migrated_state = json.loads((loop_dir / "state.json").read_text(encoding="utf-8"))
    assert migrated_state["status"] == "paused"
    assert migrated_state["candidate"]["evidence_valid"] is False
    assert migrated_state["candidate"]["supersedes_candidate_hash"] == "1" * 64
    assert migrated_state["gates"]["core_flash"] == "pending"
    assert migrated_state["gates"]["habit_flash"] == "pending"
    assert "stage_2_pro" not in migrated_state["gates"]
    migrated_waits = json.loads((loop_dir / "waits.json").read_text(encoding="utf-8"))
    assert migrated_waits[0]["status"] == "stale"
    assert migrated_waits[0]["prior_status"] == "pending"
    migrated_campaign = json.loads(
        (loop_dir / "campaign.json").read_text(encoding="utf-8")
    )
    assert migrated_campaign["joint_campaign_version"] == 3
    assert migrated_campaign["progress"]["route_waits"][0]["status"] == "stale"
    receipt = json.loads(
        (loop_dir / "evidence" / "HD-001-joint-campaign-migration.json").read_text(
            encoding="utf-8"
        )
    )
    assert receipt["historical_evidence_mutated"] is False
    assert receipt["historical_wait_records_retained"] is True
    assert receipt["stale_wait_ids"] == ["wait-legacy-route"]
    assert receipt["legacy_evidence_refs"] == ["historical/verdict.json"]
    assert receipt["post_migration_validation"]["ok"] is True
    assert receipt["post_migration_validation"]["finding_count"] == 0


def test_v2_habit_evidence_realignment_preserves_old_receipt_and_invalidates_candidate(
    tmp_path: Path,
) -> None:
    controller = _controller_module()
    controller.SUPERLOOPS = tmp_path / "superloops"
    created = controller.instantiate()
    loop_id = created["loop_id"]
    loop_dir = controller.SUPERLOOPS / "loops" / loop_id
    campaign = json.loads((loop_dir / "campaign.json").read_text(encoding="utf-8"))
    campaign["joint_campaign_version"] = 2
    controller._atomic_json(loop_dir / "campaign.json", campaign)
    state = json.loads((loop_dir / "state.json").read_text(encoding="utf-8"))
    state["candidate"] = {
        "hash": "2" * 64,
        "hashi_commit": "a" * 40,
        "hashi_build_sha256": "b" * 64,
        "her_source_commit": "c" * 40,
        "package_sha256": "d" * 64,
        "oracle_sha256": "e" * 64,
        "evidence_valid": True,
        "freeze_status": "frozen_after_joint_layer_a",
    }
    controller._atomic_json(loop_dir / "state.json", state)
    legacy_receipt = loop_dir / "evidence" / "HD-001-joint-campaign-migration.json"
    controller._atomic_json(legacy_receipt, {"immutable_marker": "keep-me"})

    result = controller.migrate_joint_campaign(loop_id)

    assert result["migrated"] is True
    assert result["validation"]["ok"] is True
    assert json.loads(legacy_receipt.read_text(encoding="utf-8")) == {
        "immutable_marker": "keep-me"
    }
    receipt_path = (
        loop_dir / "evidence" / "HD-001-habit-test-enhancement-realignment.json"
    )
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["verdict"] == "REALIGNED_PENDING_REVALIDATION"
    assert receipt["from_joint_campaign_version"] == 2
    assert receipt["to_joint_campaign_version"] == 3
    assert receipt["superseded_candidate"]["hash"] == "2" * 64
    assert receipt["candidate_evidence_valid"] is False
    migrated_campaign = json.loads(
        (loop_dir / "campaign.json").read_text(encoding="utf-8")
    )
    assert migrated_campaign["joint_campaign_version"] == 3


def test_joint_realign_clears_stale_runtime_ownership_without_overwriting_receipt(
    tmp_path: Path,
) -> None:
    controller = _controller_module()
    controller.SUPERLOOPS = tmp_path / "superloops"
    created = controller.instantiate()
    loop_id = created["loop_id"]
    loop_dir = controller.SUPERLOOPS / "loops" / loop_id
    original_receipt = (
        loop_dir / "evidence" / "HD-001-habit-test-enhancement-realignment.json"
    )
    controller._atomic_json(original_receipt, {"immutable_marker": "keep-me"})
    state = json.loads((loop_dir / "state.json").read_text(encoding="utf-8"))
    state.update(
        {
            "active_attempt_id": "HD-002-A003",
            "worker_runtime": {
                "agent": "ajiao",
                "status": "running",
                "request_id": "req-0001",
                "process": "alive",
            },
            "pause_closeout_ref": "evidence/stale-pause-closeout.json",
        }
    )
    controller._atomic_json(loop_dir / "state.json", state)

    result = controller.migrate_joint_campaign(loop_id)

    migrated = json.loads((loop_dir / "state.json").read_text(encoding="utf-8"))
    assert "active_attempt_id" not in migrated
    assert "worker_runtime" not in migrated
    assert "pause_closeout_ref" not in migrated
    assert json.loads(original_receipt.read_text(encoding="utf-8")) == {
        "immutable_marker": "keep-me"
    }
    assert result["receipt_ref"].endswith(
        "HD-001-habit-test-enhancement-realignment-a002.json"
    )
    receipt = json.loads(
        (loop_dir / "evidence" / Path(result["receipt_ref"]).name).read_text(
            encoding="utf-8"
        )
    )
    assert receipt["from_joint_campaign_version"] == 3
    assert receipt["to_joint_campaign_version"] == 3
    assert receipt["cleared_stale_runtime_keys"] == [
        "active_attempt_id",
        "pause_closeout_ref",
        "worker_runtime",
    ]


def test_habit_attempt_claim_validator_rejects_no_change_as_deep_lifecycle_credit(
) -> None:
    validator = _validator_module()
    attempt = _load_json("attempt.template.json")
    attempt["packet"]["habit_scenario"] = "habit_deep"
    attempt["evidence"].update(
        {
            "selected_habit_ids": ["selected-only"],
            "journal_timeline": ["pending", "running", "applying", "no_change"],
            "formation_observed": False,
            "retrieval_observed": True,
            "retrieval_evidence_refs": ["selection.json"],
            "behavioral_use_observed": True,
            "behavioral_use_evidence_refs": [],
            "no_change_claim_limit_acknowledged": False,
        }
    )

    findings = validator.validate_habit_attempt_evidence(attempt)

    assert {
        "behavioral_use_evidence_missing",
        "no_change_claim_limit",
        "habit_deep_formation",
    }.issubset({finding["code"] for finding in findings})


def test_habit_attempt_claim_validator_accepts_fully_evidenced_deep_lifecycle(
) -> None:
    validator = _validator_module()
    attempt = _load_json("attempt.template.json")
    attempt["packet"]["habit_scenario"] = "habit_deep"
    attempt["evidence"].update(
        {
            "journal_timeline": ["pending", "running", "applying", "completed"],
            "formation_observed": True,
            "formation_evidence_refs": ["formation.json"],
            "retrieval_observed": True,
            "retrieval_evidence_refs": ["retrieval.json"],
            "behavioral_use_observed": True,
            "behavioral_use_evidence_refs": ["behavior.json"],
            "no_change_claim_limit_acknowledged": False,
        }
    )

    assert validator.validate_habit_attempt_evidence(attempt) == []


def test_layer_a_rejects_selected_id_without_closed_loop_behavioral_evidence() -> None:
    controller = _controller_module()

    errors = controller._layer_a_habit_evidence_errors(
        {
            "habit_evidence": {
                "selected_habit_ids": ["selected-only"],
                "retrieval_observed": True,
                "retrieval_evidence_refs": ["selection.json"],
            }
        }
    )

    assert "formation_observed must be true" in errors
    assert "behavioral_use_observed must be true" in errors
    assert "behavioral_use_evidence_refs must be non-empty" in errors


def test_live_phase_requires_candidate_frozen_after_joint_layer_a(
    tmp_path: Path,
) -> None:
    controller = _controller_module()
    controller.SUPERLOOPS = tmp_path / "superloops"
    created = controller.instantiate()
    loop_id = created["loop_id"]
    loop_dir = controller.SUPERLOOPS / "loops" / loop_id
    state = json.loads((loop_dir / "state.json").read_text(encoding="utf-8"))
    state["current_phase"] = "stage_1_flash_cheap"
    state["current_step"] = "HD-003"
    state["candidate"]["hash"] = "1" * 64
    state["candidate"]["freeze_status"] = "joint_layer_a_validation_pending"
    state["candidate"]["evidence_valid"] = False
    controller._atomic_json(loop_dir / "state.json", state)

    report = controller._custom_validation(loop_id)

    assert report["ok"] is False
    assert "live_before_joint_layer_a_freeze" in {
        finding["code"] for finding in report["findings"]
    }
