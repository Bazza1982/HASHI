from __future__ import annotations

import json
from pathlib import Path

from tools.windows_use_evaluation import (
    build_windows_usecomputer_eval_plan,
    prepare_windows_usecomputer_eval_workspace,
    write_windows_usecomputer_eval_bundle,
)


def test_prepare_windows_usecomputer_eval_workspace_creates_safe_dragdrop_layout(tmp_path: Path) -> None:
    paths = prepare_windows_usecomputer_eval_workspace(tmp_path)

    source = Path(paths["dragdrop_source_dir"])
    target = Path(paths["dragdrop_target_dir"])
    word_output = Path(paths["word_output_dir"])
    excel_output = Path(paths["excel_output_dir"])
    powerpoint_output = Path(paths["powerpoint_output_dir"])
    word_source = Path(paths["word_source_dir"])
    excel_source = Path(paths["excel_source_dir"])
    powerpoint_source = Path(paths["powerpoint_source_dir"])
    assert source.exists()
    assert target.exists()
    assert word_output.exists()
    assert excel_output.exists()
    assert powerpoint_output.exists()
    assert word_source.exists()
    assert excel_source.exists()
    assert powerpoint_source.exists()
    assert (source / "alpha.txt").read_text(encoding="utf-8").startswith("alpha")
    assert (source / "bravo.txt").read_text(encoding="utf-8").startswith("bravo")
    assert Path(paths["advanced_word_notes_path"]).read_text(encoding="utf-8").startswith("Quarterly Operations Review")
    assert "Region,Rep,Quarter" in Path(paths["regional_sales_csv_path"]).read_text(encoding="utf-8")
    assert "Quarterly Business Briefing" in Path(paths["powerpoint_outline_path"]).read_text(encoding="utf-8")
    assert (tmp_path / "workspace" / "README.txt").exists()


def test_build_windows_usecomputer_eval_plan_covers_common_windows_tasks(tmp_path: Path) -> None:
    plan = build_windows_usecomputer_eval_plan(tmp_path)

    scenario_ids = {item["id"] for item in plan["scenarios"]}
    assert plan["mode"] == "windows_usecomputer_manual_evaluation"
    assert "preflight_readiness" in scenario_ids
    assert "explorer_navigation" in scenario_ids
    assert "dragdrop_folders" in scenario_ids
    assert "word_basic_editing" in scenario_ids
    assert "excel_data_entry_formula" in scenario_ids
    assert "word_advanced_report_formatting" in scenario_ids
    assert "excel_advanced_analysis_dashboard" in scenario_ids
    assert "office_integrated_excel_word_pdf" in scenario_ids
    assert "powerpoint_complex_deck_rehearsal" in scenario_ids
    assert "multi_window_switching" in scenario_ids
    assert "scroll_context_and_rename" in scenario_ids

    prompts = "\n".join(item["prompt"] for item in plan["scenarios"])
    assert "/usecomputer" in prompts
    assert "Word" in prompts
    assert "Excel" in prompts
    assert "drag" in prompts.lower()
    assert "conditional formatting" in prompts
    assert "page numbering" in prompts
    assert "PDF" in prompts
    assert "PowerPoint" in prompts
    assert "speaker notes" in prompts

    rubric_metrics = set(plan["rubric"]["metrics"])
    assert "task_success" in rubric_metrics
    assert "manual_intervention_count" in rubric_metrics
    assert "wrong_click_count" in rubric_metrics
    assert "formula_correctness" in rubric_metrics
    assert "formatting_fidelity" in rubric_metrics


def test_write_windows_usecomputer_eval_bundle_persists_plan_report_and_readme(tmp_path: Path) -> None:
    bundle = write_windows_usecomputer_eval_bundle(tmp_path)

    plan_path = tmp_path / "state" / "windows_usecomputer_eval_plan.json"
    report_path = tmp_path / "state" / "windows_usecomputer_eval_report_template.json"
    readme_path = tmp_path / "README.md"

    saved_plan = json.loads(plan_path.read_text(encoding="utf-8"))
    saved_report = json.loads(report_path.read_text(encoding="utf-8"))

    assert saved_plan["mode"] == bundle["mode"]
    assert saved_report["status"] == "not_run"
    assert len(saved_report["results"]) == len(saved_plan["scenarios"])
    assert "Windows /usecomputer Evaluation Bundle" in readme_path.read_text(encoding="utf-8")
