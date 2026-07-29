from __future__ import annotations

import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "superloops" / "templates" / "aptenra_local_packaging_debug"


def _load_json(name: str):
    return json.loads((TEMPLATE / name).read_text(encoding="utf-8"))


def _validator_module():
    path = TEMPLATE / "validate_round.py"
    spec = importlib.util.spec_from_file_location("aptenra_packaging_validate_round", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_valid_structure(tmp_path: Path) -> Path:
    loop_dir = tmp_path / "superloops" / "loops" / "sl-aptenra-test"
    round_dir = loop_dir / "rounds" / "round-01"
    round_dir.mkdir(parents=True)

    registry = _load_json("known_failure_registry.template.json")
    (loop_dir / "known_failure_registry.json").write_text(
        json.dumps(registry, indent=2),
        encoding="utf-8",
    )
    state = {
        "loop_id": "sl-aptenra-test",
        "status": "running",
        "current_round": 1,
        "max_rounds": 30,
        "scheduler_auto_advance": False,
        "execution_policy": {
            "mandatory_round_question": (
                "What mistake did I make last time, and how will I avoid it in the most straightforward way this round?"
            ),
            "known_failure_recurrence": "immediate_block",
            "actual_install_required_for_candidate_validation": True,
            "failed_candidate_uninstall_before_next_round": True,
        },
        "liveness": {
            "mode": "idle_nudge",
            "may_mutate_task_status": False,
        },
        "known_failure_registry_path": "superloops/loops/sl-aptenra-test/known_failure_registry.json",
        "active_round_path": "superloops/loops/sl-aptenra-test/rounds/round-01/round.json",
    }
    (loop_dir / "state.json").write_text(json.dumps(state, indent=2), encoding="utf-8")
    round_record = _load_json("round_record.template.json")
    round_record["round_id"] = "R01"
    round_record["round_number"] = 1
    round_record["mandatory_reflection"]["answer_1"] = "Reused credentialed state hid a clean-first-run defect."
    round_record["mandatory_reflection"]["answer_2"] = "Require an actual empty-state launch before build."
    round_record["mandatory_reflection"]["material_difference_from_prior_round"] = (
        "This round blocks build until clean-state launch and rollback pass."
    )
    round_record["journal_review"]["journal_pfj_ids"] = list(registry["required_ids"])
    round_record["journal_review"]["registry_pfj_ids"] = list(registry["required_ids"])
    (round_dir / "round.json").write_text(json.dumps(round_record, indent=2), encoding="utf-8")
    return loop_dir


def test_template_encodes_user_required_round_and_actual_install_policy() -> None:
    state = _load_json("state.template.json")
    registry = _load_json("known_failure_registry.template.json")
    taskboard = _load_json("taskboard.template.json")
    readme = (TEMPLATE / "README.md").read_text(encoding="utf-8")
    liveness = (TEMPLATE / "liveness_nudge.template.md").read_text(encoding="utf-8")

    assert state["max_rounds"] == 30
    assert state["scheduler_auto_advance"] is False
    assert state["liveness"]["may_mutate_task_status"] is False
    assert state["execution_policy"]["known_failure_recurrence"] == "immediate_block"
    assert state["execution_policy"]["actual_install_required_for_candidate_validation"] is True
    assert state["execution_policy"]["failed_candidate_uninstall_before_next_round"] is True
    assert registry["required_ids"] == [f"PFJ-{index:03d}" for index in range(1, 36)]
    assert [item["pfj_id"] for item in registry["failures"]] == registry["required_ids"]
    assert any(task["phase"] == "actual_installed_validation" for task in taskboard)
    assert "actual GUI install" in readme
    assert "What mistake did I make last time" in liveness
    assert "Do not mark a pending task `in_progress` merely because the loop was idle." in liveness


def test_structure_validator_passes_with_complete_first_round_reflection(tmp_path: Path) -> None:
    loop_dir = _write_valid_structure(tmp_path)
    report = _validator_module().validate_round(loop_dir, "structure")
    assert report["ok"] is True
    assert report["findings"] == []


def test_known_failure_recurrence_blocks_on_first_detection(tmp_path: Path) -> None:
    loop_dir = _write_valid_structure(tmp_path)
    round_path = loop_dir / "rounds" / "round-01" / "round.json"
    round_record = json.loads(round_path.read_text(encoding="utf-8"))
    round_record["journal_review"]["known_signatures_detected"] = ["PFJ-033"]
    round_path.write_text(json.dumps(round_record, indent=2), encoding="utf-8")

    report = _validator_module().validate_round(loop_dir, "structure")

    assert report["ok"] is False
    assert any(item["code"] == "known_signature_recurrence" for item in report["findings"])


def test_round_close_rejects_theoretical_validation_without_install_and_uninstall(tmp_path: Path) -> None:
    loop_dir = _write_valid_structure(tmp_path)
    round_path = loop_dir / "rounds" / "round-01" / "round.json"
    round_record = json.loads(round_path.read_text(encoding="utf-8"))
    round_record["gates"]["candidate_build_allowed"] = True
    round_record["gates"]["candidate_install_allowed"] = True
    round_record["gates"]["known_failure_results"] = {
        f"PFJ-{index:03d}": {"status": "passed", "evidence": "test-evidence"}
        for index in range(1, 36)
    }
    round_record["candidate"] = {
        "candidate_id": "candidate-test",
        "product_code": "{00000000-0000-0000-0000-000000000001}",
        "product_commit": "abc",
        "packaging_commit": "def",
        "media_directory": "C:\\candidate-test",
        "msi_sha256": "0" * 64,
    }
    round_path.write_text(json.dumps(round_record, indent=2), encoding="utf-8")

    report = _validator_module().validate_round(loop_dir, "round-close")

    assert report["ok"] is False
    codes = {item["code"] for item in report["findings"]}
    assert "actual_install_missing" in codes
    assert "actual_launch_missing" in codes
    assert "actual_uninstall_missing" in codes
