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


def test_template_validator_accepts_the_checked_in_design() -> None:
    report = _validator_module().validate_template(TEMPLATE)

    assert report == {
        "ok": True,
        "template": "her_debug",
        "stage_1_core_cells": 24,
        "stage_2_core_cells": 24,
        "total_core_cells": 48,
        "finding_count": 0,
        "findings": [],
    }


def test_campaign_freezes_flash_before_pro_and_forbids_fallbacks() -> None:
    campaign = _load_json("campaign.template.json")
    stages = {stage["stage_id"]: stage for stage in campaign["stages"]}

    assert stages["stage_1_flash"]["allowed_live_models"] == {
        "official_deepseek": "deepseek-v4-flash",
        "openrouter": "deepseek/deepseek-v4-flash",
    }
    assert stages["stage_2_pro"]["allowed_live_models"] == {
        "official_deepseek": "deepseek-v4-pro",
        "openrouter": "deepseek/deepseek-v4-pro",
    }
    assert "stage_1_flash=passed" in stages["stage_2_pro"]["locked_until"]
    assert campaign["expected_counts"]["total_core_cells"] == 48
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
    assert liveness["must_follow_up_nonterminal_failure"] is True
    assert liveness["must_surface_stagnation"] is True
    assert liveness["stagnation_observation_limit"] == 3
    assert liveness["max_nudges"] == 0
    assert "This idle nudge wakes `lin_yueru`" in nudge
    assert "It is not a nudge\nfor Ajiao" in nudge
    assert "If Ajiao is `running`, do not `/stop`" in nudge
    assert "A failed reply is not a campaign terminal result" in nudge
    assert '"pending task" means a phase task whose taskboard status is `pending`' in nudge
    assert "Do not set or retain `pending_non_nudge_start_authority`" in nudge


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


def test_pro_task_depends_on_complete_flash_gate() -> None:
    tasks = {task["task_id"]: task for task in _load_json("taskboard.template.json")}

    assert tasks["HD-006"]["phase"] == "stage_1_flash_gate"
    assert tasks["HD-007"]["phase"] == "stage_2_pro_cheap"
    assert tasks["HD-007"]["depends_on"] == ["HD-006"]
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
        "openrouter",
    ]
    assert campaign["funds_policy"][
        "terminal_on_confirmed_insufficient_funds_from_any_required_route"
    ] is True


def test_complete_local_task_freezes_candidate_and_advances_to_worker(tmp_path: Path) -> None:
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
        her_source_commit="b" * 40,
        package_sha256="c" * 64,
    )

    assert result["current_step"] == "HD-002"
    assert result["next_action"] == {"kind": "dispatch_task", "task_id": "HD-002"}
    assert result["candidate"]["evidence_valid"] is True
    updated_tasks = json.loads((loop_dir / "taskboard.json").read_text(encoding="utf-8"))
    assert updated_tasks[0]["status"] == "completed"
    assert updated_tasks[1]["status"] == "in_progress"
