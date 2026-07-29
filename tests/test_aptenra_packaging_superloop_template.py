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

    state = _load_json("state.template.json")
    state["loop_id"] = "sl-aptenra-test"
    state["status"] = "running"
    state["active_round_path"] = (
        "superloops/loops/sl-aptenra-test/rounds/round-01/round.json"
    )
    (loop_dir / "state.json").write_text(json.dumps(state, indent=2), encoding="utf-8")

    round_record = _load_json("round_record.template.json")
    round_record["round_id"] = "R01"
    round_record["round_number"] = 1
    round_record["round_focus"]["latest_relevant_failure"] = "Installed Aptenra did not launch."
    round_record["round_focus"]["smallest_direct_avoidance"] = "Repair the installed entry point."
    round_record["round_focus"]["failure_journal_path"] = "Failure Journal"
    (round_dir / "round.json").write_text(json.dumps(round_record, indent=2), encoding="utf-8")
    return loop_dir


def test_template_encodes_four_facts_without_historical_blockers() -> None:
    state = _load_json("state.template.json")
    taskboard = _load_json("taskboard.template.json")
    readme = (TEMPLATE / "README.md").read_text(encoding="utf-8")
    liveness = (TEMPLATE / "liveness_nudge.template.md").read_text(encoding="utf-8")

    assert state["max_rounds"] == 30
    assert state["scheduler_auto_advance"] is False
    assert state["execution_policy"] == {
        "validation_source": "installed_candidate_only",
        "failure_journal_update": "after_every_failed_install_or_launch",
        "failed_candidate_cleanup": "uninstall_before_next_round",
        "environment_boundary": "candidate_only_preserve_user_environment_and_debug_runtime",
        "prebuild_checks": "advisory_non_blocking",
        "provider_credentials_required": False,
        "success_condition": "installed_aptenra_and_workbench_launch",
    }
    assert state["liveness"]["interval_minutes"] == 1
    assert len(taskboard) == 4
    assert all("depends_on" not in task for task in taskboard)
    assert all("required_evidence" not in task for task in taskboard)
    assert all("gates" not in json.dumps(task).casefold() for task in taskboard)
    assert not (TEMPLATE / "known_failure_registry.template.json").exists()
    assert "no PFJ entry can delay a new build" in readme
    assert "All prebuild checks are advisory and non-blocking" in liveness
    assert "Do not answer with status alone" in liveness


def test_structure_audit_passes_without_candidate_or_history_matrix(tmp_path: Path) -> None:
    loop_dir = _write_valid_structure(tmp_path)
    report = _validator_module().validate_round(loop_dir, "structure")
    assert report["ok"] is True
    assert report["mode"] == "non_blocking_structure_audit"
    assert report["findings"] == []


def test_prebuild_and_preinstall_do_not_require_candidate_approval(tmp_path: Path) -> None:
    loop_dir = _write_valid_structure(tmp_path)

    for phase in ("prebuild", "preinstall"):
        report = _validator_module().validate_round(loop_dir, phase)
        assert report["ok"] is True
        assert report["findings"] == []


def test_round_close_rejects_theoretical_validation_without_install(tmp_path: Path) -> None:
    loop_dir = _write_valid_structure(tmp_path)
    report = _validator_module().validate_round(loop_dir, "round-close")

    assert report["ok"] is False
    codes = {item["code"] for item in report["findings"]}
    assert "actual_install_missing" in codes
    assert "installed_source_missing" in codes
    assert "aptenra_launch_missing" in codes
    assert "workbench_launch_missing" in codes


def test_failed_round_requires_journal_update_and_uninstall(tmp_path: Path) -> None:
    loop_dir = _write_valid_structure(tmp_path)
    round_path = loop_dir / "rounds" / "round-01" / "round.json"
    round_record = json.loads(round_path.read_text(encoding="utf-8"))
    round_record["actual_validation"].update(
        {
            "actual_install_attempted": True,
            "install_mode": "human_gui_usecomputer",
            "validation_source": "installed_msi",
            "aptenra_shortcut_launch_attempted": True,
            "workbench_shortcut_launch_attempted": True,
        }
    )
    round_record["environment_boundary"] = {
        "user_environment_unchanged": True,
        "original_debug_runtime_unchanged": True,
    }
    round_record["failure_handling"]["candidate_failed"] = True
    round_record["outcome"]["status"] = "failed"
    round_path.write_text(json.dumps(round_record, indent=2), encoding="utf-8")

    report = _validator_module().validate_round(loop_dir, "round-close")

    assert report["ok"] is False
    codes = {item["code"] for item in report["findings"]}
    assert "failure_journal_missing" in codes
    assert "failed_candidate_uninstall_missing" in codes


def test_successful_installed_dual_launch_does_not_require_uninstall(tmp_path: Path) -> None:
    loop_dir = _write_valid_structure(tmp_path)
    round_path = loop_dir / "rounds" / "round-01" / "round.json"
    round_record = json.loads(round_path.read_text(encoding="utf-8"))
    round_record["actual_validation"] = {
        "actual_install_attempted": True,
        "install_mode": "human_gui_usecomputer",
        "validation_source": "installed_msi",
        "source_or_unpacked_substituted": False,
        "aptenra_shortcut_launch_attempted": True,
        "aptenra_user_visible_launch_result": "success",
        "workbench_shortcut_launch_attempted": True,
        "workbench_user_visible_launch_result": "success",
        "basic_functions_result": "success",
    }
    round_record["environment_boundary"] = {
        "user_environment_unchanged": True,
        "original_debug_runtime_unchanged": True,
    }
    round_record["outcome"]["status"] = "install_dual_launch_accepted"
    round_path.write_text(json.dumps(round_record, indent=2), encoding="utf-8")

    report = _validator_module().validate_round(loop_dir, "round-close")

    assert report["ok"] is True
    assert report["findings"] == []
