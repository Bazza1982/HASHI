from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _write_file(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def prepare_windows_usecomputer_eval_workspace(root_dir: Path) -> dict[str, str]:
    workspace = root_dir / "workspace"
    dragdrop_source = workspace / "dragdrop" / "source"
    dragdrop_target = workspace / "dragdrop" / "target"
    word_output = workspace / "word_output"
    excel_output = workspace / "excel_output"
    powerpoint_output = workspace / "powerpoint_output"
    word_source = workspace / "word_source"
    excel_source = workspace / "excel_source"
    powerpoint_source = workspace / "powerpoint_source"
    evidence = root_dir / "logs" / "artifacts"

    dragdrop_source.mkdir(parents=True, exist_ok=True)
    dragdrop_target.mkdir(parents=True, exist_ok=True)
    word_output.mkdir(parents=True, exist_ok=True)
    excel_output.mkdir(parents=True, exist_ok=True)
    powerpoint_output.mkdir(parents=True, exist_ok=True)
    word_source.mkdir(parents=True, exist_ok=True)
    excel_source.mkdir(parents=True, exist_ok=True)
    powerpoint_source.mkdir(parents=True, exist_ok=True)
    evidence.mkdir(parents=True, exist_ok=True)
    (root_dir / "state").mkdir(parents=True, exist_ok=True)

    _write_file(
        dragdrop_source / "alpha.txt",
        "alpha file for drag and drop evaluation\n",
    )
    _write_file(
        dragdrop_source / "bravo.txt",
        "bravo file for drag and drop evaluation\n",
    )
    _write_file(
        workspace / "README.txt",
        (
            "Windows /usecomputer evaluation workspace.\n"
            "Use this directory for safe Explorer, drag/drop, Word, and Excel tasks.\n"
        ),
    )
    _write_file(
        word_source / "advanced_report_notes.txt",
        (
            "Quarterly Operations Review\n"
            "\n"
            "Executive summary: Delivery quality improved, but support response time needs attention.\n"
            "Key metrics:\n"
            "- Revenue: 128000\n"
            "- Gross margin: 0.37\n"
            "- Open support tickets: 42\n"
            "- Customer satisfaction: 4.4 / 5\n"
            "\n"
            "Risks:\n"
            "1. Support queue growth\n"
            "2. Training coverage gaps\n"
            "3. Forecast volatility\n"
            "\n"
            "Recommended actions:\n"
            "- Add one support rotation\n"
            "- Review onboarding checklist\n"
            "- Reforecast next quarter by region\n"
        ),
    )
    _write_file(
        excel_source / "regional_sales.csv",
        (
            "Region,Rep,Quarter,Product,Units,Unit Price,Discount,Customer Rating\n"
            "North,Ada,Q1,Core,38,1200,0.05,4.6\n"
            "North,Ada,Q2,Core,44,1200,0.04,4.7\n"
            "North,Ben,Q1,Plus,29,1750,0.08,4.2\n"
            "South,Cia,Q1,Core,41,1190,0.03,4.5\n"
            "South,Cia,Q2,Plus,25,1780,0.07,4.1\n"
            "East,Dan,Q1,Enterprise,12,4200,0.10,4.8\n"
            "East,Eli,Q2,Enterprise,14,4150,0.09,4.9\n"
            "West,Fay,Q1,Core,36,1210,0.02,4.3\n"
            "West,Fay,Q2,Plus,31,1760,0.06,4.4\n"
            "West,Gus,Q2,Enterprise,9,4300,0.12,4.0\n"
        ),
    )
    _write_file(
        powerpoint_source / "quarterly_briefing_outline.txt",
        (
            "Quarterly Business Briefing\n"
            "\n"
            "Audience: senior operations team\n"
            "Goal: explain regional performance and recommend next-quarter actions\n"
            "\n"
            "Key facts:\n"
            "- North has the highest total net revenue.\n"
            "- West is close behind and has a strong enterprise opportunity.\n"
            "- South has lower net revenue and two lower customer-rating signals.\n"
            "- Ratings below 4.3 should be highlighted as service risk.\n"
            "\n"
            "Required deck structure:\n"
            "1. Title slide\n"
            "2. Agenda\n"
            "3. Regional performance summary\n"
            "4. Risks and service signals\n"
            "5. Recommended actions\n"
            "6. Closing slide\n"
            "\n"
            "Presenter notes:\n"
            "- Open with the reason the analysis matters.\n"
            "- Explain that revenue and customer rating need to be read together.\n"
            "- Spend extra time on South risk and West opportunity.\n"
            "- Close with three concrete actions.\n"
        ),
    )

    return {
        "workspace_dir": str(workspace),
        "dragdrop_source_dir": str(dragdrop_source),
        "dragdrop_target_dir": str(dragdrop_target),
        "word_output_dir": str(word_output),
        "excel_output_dir": str(excel_output),
        "powerpoint_output_dir": str(powerpoint_output),
        "word_source_dir": str(word_source),
        "excel_source_dir": str(excel_source),
        "powerpoint_source_dir": str(powerpoint_source),
        "advanced_word_notes_path": str(word_source / "advanced_report_notes.txt"),
        "regional_sales_csv_path": str(excel_source / "regional_sales.csv"),
        "powerpoint_outline_path": str(powerpoint_source / "quarterly_briefing_outline.txt"),
        "artifact_dir": str(evidence),
    }


def _scenario(
    *,
    scenario_id: str,
    title: str,
    category: str,
    prompt: str,
    setup: list[str],
    success_criteria: list[str],
    evidence: list[str],
    tags: list[str],
    optional: bool = False,
) -> dict[str, Any]:
    return {
        "id": scenario_id,
        "title": title,
        "category": category,
        "optional": optional,
        "prompt": prompt,
        "setup": setup,
        "success_criteria": success_criteria,
        "evidence": evidence,
        "tags": tags,
    }


def build_windows_usecomputer_eval_plan(root_dir: Path) -> dict[str, Any]:
    paths = prepare_windows_usecomputer_eval_workspace(root_dir)
    artifact_dir = paths["artifact_dir"]

    scenarios = [
        _scenario(
            scenario_id="preflight_readiness",
            title="Readiness And Basic Driving",
            category="general_driving",
            prompt=(
                "/usecomputer Warm up the Windows desktop control stack, confirm screenshot works, "
                "list visible windows, move the mouse, click a safe empty area, and report what happened."
            ),
            setup=[
                "Keep the desktop unlocked.",
                "Close or minimize sensitive windows before testing.",
                "Allow saving screenshots under the evaluation artifact directory.",
            ],
            success_criteria=[
                "A screenshot is captured successfully.",
                "The assistant can enumerate or identify visible windows.",
                "Mouse movement and a safe click complete without getting stuck.",
                "The agent reports a concise readiness summary.",
            ],
            evidence=[
                f"Screenshot saved under {artifact_dir}",
                "Operator note about any jitter, lag, or wrong click",
            ],
            tags=["preflight", "screenshot", "mouse", "click", "window_list"],
        ),
        _scenario(
            scenario_id="explorer_navigation",
            title="Explorer Navigation And Selection",
            category="general_driving",
            prompt=(
                f"/usecomputer Open File Explorer to {paths['workspace_dir']}, navigate into the dragdrop "
                "source folder, select files, go back, and summarize exactly what was selected."
            ),
            setup=[
                "Use the generated evaluation workspace only.",
                f"Evaluation workspace root: {paths['workspace_dir']}",
            ],
            success_criteria=[
                "Explorer opens the requested folder.",
                "The assistant reaches the source folder without manual correction.",
                "At least one file selection action is completed correctly.",
                "The agent can describe the current Explorer location and selected file names.",
            ],
            evidence=[
                "Screenshot showing Explorer in the expected folder",
                "Operator note on navigation mistakes or accidental double-clicks",
            ],
            tags=["explorer", "navigation", "selection", "clicking"],
        ),
        _scenario(
            scenario_id="dragdrop_folders",
            title="Folder Drag And Drop",
            category="drag_drop",
            prompt=(
                f"/usecomputer In File Explorer, drag alpha.txt from {paths['dragdrop_source_dir']} "
                f"into {paths['dragdrop_target_dir']}, then verify whether the target folder contains the file."
            ),
            setup=[
                f"Source folder: {paths['dragdrop_source_dir']}",
                f"Target folder: {paths['dragdrop_target_dir']}",
                "Reset the workspace before rerunning if the file already moved.",
            ],
            success_criteria=[
                "The assistant performs an actual drag gesture rather than keyboard-only fallback.",
                "The target folder contains alpha.txt after the task.",
                "The agent states whether it interpreted the action as move or copy.",
                "No unrelated files are moved accidentally.",
            ],
            evidence=[
                "Before/after screenshot of source and target folders",
                "Operator note on drag precision, drop accuracy, and retries",
            ],
            tags=["drag", "drop", "explorer", "precision"],
        ),
        _scenario(
            scenario_id="word_basic_editing",
            title="Word Basic Editing",
            category="word",
            optional=True,
            prompt=(
                f"/usecomputer Open Microsoft Word, create a blank document, type a heading and a short bullet list, "
                f"make the heading bold, and save the document into {paths['word_output_dir']}."
            ),
            setup=[
                "Microsoft Word should be installed for this optional scenario.",
                f"Save output into {paths['word_output_dir']}",
            ],
            success_criteria=[
                "Word launches and reaches a blank editable document.",
                "Typed text appears in the expected place.",
                "A formatting action such as bold succeeds.",
                "The document saves into the requested folder with the expected filename.",
            ],
            evidence=[
                "Saved .docx file in the evaluation workspace",
                "Screenshot showing the formatted document before save",
                "Operator note on ribbon navigation, focus issues, or modal interruptions",
            ],
            tags=["word", "typing", "formatting", "save_as", "shortcuts"],
        ),
        _scenario(
            scenario_id="excel_data_entry_formula",
            title="Excel Data Entry And Formula",
            category="excel",
            optional=True,
            prompt=(
                f"/usecomputer Open Microsoft Excel, create a small 2-column table with four rows of numbers, "
                f"enter a SUM formula for the total, and save the workbook into {paths['excel_output_dir']}."
            ),
            setup=[
                "Microsoft Excel should be installed for this optional scenario.",
                f"Save output into {paths['excel_output_dir']}",
            ],
            success_criteria=[
                "Excel launches and reaches a blank workbook.",
                "Cell navigation is correct for the intended entries.",
                "The formula result appears in the expected total cell.",
                "The workbook saves into the requested folder.",
            ],
            evidence=[
                "Saved .xlsx file in the evaluation workspace",
                "Screenshot showing the filled grid and total cell",
                "Operator note on cell targeting, focus drift, or wrong-sheet behavior",
            ],
            tags=["excel", "grid", "formula", "typing", "save_as"],
        ),
        _scenario(
            scenario_id="word_advanced_report_formatting",
            title="Word Advanced Report Formatting",
            category="word",
            optional=True,
            prompt=(
                f"/usecomputer Open Microsoft Word and create a polished operations review report from "
                f"{paths['advanced_word_notes_path']}. Use a title, subtitle, two heading levels, a "
                "formatted 4-row metrics table, a numbered risk list, a bulleted action list, bold key "
                "metric labels, add page numbering in the footer, and save the document as "
                f"{Path(paths['word_output_dir']) / 'advanced_operations_review.docx'}."
            ),
            setup=[
                "Microsoft Word should be installed for this optional scenario.",
                f"Source notes: {paths['advanced_word_notes_path']}",
                f"Save output into {paths['word_output_dir']}",
                "The assistant should use real Word UI interactions, shortcuts, ribbon controls, or context menus.",
            ],
            success_criteria=[
                "The document contains the source content without material omissions.",
                "The title, subtitle, heading hierarchy, lists, and table are visually distinct.",
                "The metrics table has labels and numeric values in separate columns.",
                "At least one meaningful formatting operation is applied beyond plain text, such as bold, table styling, or page numbering.",
                "The document saves with the requested filename in the Word output directory.",
                "No unrelated existing document is overwritten.",
            ],
            evidence=[
                "Saved advanced_operations_review.docx in the evaluation workspace",
                "Screenshot showing the formatted report body",
                "Screenshot or operator note showing footer/page numbering if visible",
                "Operator note on ribbon navigation, selection accuracy, and save reliability",
            ],
            tags=["word", "advanced_formatting", "tables", "lists", "footer", "save_as"],
        ),
        _scenario(
            scenario_id="excel_advanced_analysis_dashboard",
            title="Excel Advanced Analysis Dashboard",
            category="excel",
            optional=True,
            prompt=(
                f"/usecomputer Open Microsoft Excel and analyze the data in {paths['regional_sales_csv_path']}. "
                "Create a workbook with the source data, calculated Revenue and Net Revenue columns, a summary "
                "section by Region using formulas, identify the top Net Revenue row, apply conditional formatting "
                "to highlight ratings below 4.3, sort or filter the data to inspect performance, create a simple "
                "chart from the regional summary, and save the workbook as "
                f"{Path(paths['excel_output_dir']) / 'advanced_sales_analysis.xlsx'}."
            ),
            setup=[
                "Microsoft Excel should be installed for this optional scenario.",
                f"Source CSV: {paths['regional_sales_csv_path']}",
                f"Save output into {paths['excel_output_dir']}",
                "The assistant should use real Excel UI interactions, formulas, clipboard entry, ribbon controls, and save dialogs.",
            ],
            success_criteria=[
                "The workbook contains the source data with all original columns.",
                "Revenue is calculated as Units multiplied by Unit Price for each row.",
                "Net Revenue is calculated after applying Discount for each row.",
                "A regional summary computes total Net Revenue for North, South, East, and West.",
                "A top performer or top revenue row is identified using a formula, sort, or filter.",
                "Conditional formatting visibly marks Customer Rating values below 4.3.",
                "A chart based on the regional summary is present.",
                "The workbook saves with the requested filename in the Excel output directory.",
            ],
            evidence=[
                "Saved advanced_sales_analysis.xlsx in the evaluation workspace",
                "Screenshot showing formulas or computed result cells",
                "Screenshot showing conditional formatting and/or filter/sort state",
                "Screenshot showing the chart",
                "Operator note on formula correctness, cell targeting, and save reliability",
            ],
            tags=["excel", "advanced_analysis", "formulas", "conditional_formatting", "chart", "sort_filter", "save_as"],
        ),
        _scenario(
            scenario_id="office_integrated_excel_word_pdf",
            title="Integrated Excel Analysis To Word Report And PDF",
            category="office_integrated",
            optional=True,
            prompt=(
                f"/usecomputer Build an integrated Office deliverable. In Excel, analyze "
                f"{paths['regional_sales_csv_path']} with formulas, a regional summary, filters, conditional "
                "formatting, and a chart. Then create a Word report that includes a title, table of contents, "
                "executive summary, metrics table, pasted or inserted Excel chart, conclusions, and footer page "
                "numbering. Save the workbook, save the Word report, and export the Word report as PDF under "
                f"{paths['workspace_dir']}."
            ),
            setup=[
                "Microsoft Excel and Word should be installed for this optional scenario.",
                f"Source CSV: {paths['regional_sales_csv_path']}",
                f"Excel output directory: {paths['excel_output_dir']}",
                f"Word output directory: {paths['word_output_dir']}",
                "The assistant should use real desktop UI operations, switching between Excel, Word, and File Explorer as needed.",
            ],
            success_criteria=[
                "Excel workbook contains source data, calculated columns, regional summary, conditional formatting, filter controls, and a chart.",
                "Word report contains a title, generated or manually inserted table of contents section, executive summary, metrics table, Excel chart or chart screenshot, conclusions, and footer page number.",
                "The workflow includes real cross-application transfer from Excel into Word, such as chart copy/paste or chart screenshot insertion.",
                "The Excel workbook, Word report, and PDF export all save under the evaluation workspace.",
                "The final files can be reopened or inspected for expected structure and content.",
            ],
            evidence=[
                "Saved integrated workbook .xlsx",
                "Saved integrated report .docx",
                "Saved integrated report .pdf",
                "Screenshot showing Excel analysis and chart",
                "Screenshot showing Word report with chart and page numbering",
                "Operator note on cross-app switching, chart transfer, and PDF export reliability",
            ],
            tags=["office_integrated", "excel", "word", "pdf_export", "chart_transfer", "cross_app"],
        ),
        _scenario(
            scenario_id="powerpoint_complex_deck_rehearsal",
            title="PowerPoint Complex Deck Creation And Rehearsal",
            category="powerpoint",
            optional=True,
            prompt=(
                f"/usecomputer Open Microsoft PowerPoint and build a polished 6-slide business briefing from "
                f"{paths['powerpoint_outline_path']}. Create a title slide, agenda, regional performance summary, "
                "risks slide, recommended actions slide, and closing slide. Apply a coherent theme, use at least "
                "one inserted chart or chart screenshot from the Excel analysis if available, add speaker notes "
                "to at least three slides, rearrange two slides after creating them, run Slide Show presentation "
                "mode, advance through the deck, exit presentation mode, and save the file as "
                f"{Path(paths['powerpoint_output_dir']) / 'quarterly_business_briefing.pptx'}."
            ),
            setup=[
                "Microsoft PowerPoint should be installed for this optional scenario.",
                f"Source outline: {paths['powerpoint_outline_path']}",
                f"Save output into {paths['powerpoint_output_dir']}",
                "If an Excel chart is available, transfer it through the desktop clipboard or insert a chart screenshot.",
                "The assistant should use real PowerPoint UI interactions: slide creation, layout selection, rearranging, speaker notes, and slideshow mode.",
            ],
            success_criteria=[
                "The deck contains at least six slides with distinct titles matching the requested structure.",
                "Slides use readable layout and a coherent visual style rather than plain unformatted text only.",
                "At least one slide includes a chart, image, icon, or pasted visual object.",
                "At least three slides contain speaker notes.",
                "The assistant demonstrates slide rearranging after slide creation.",
                "Slide Show mode is entered, at least three slide advances occur, and presentation mode is exited cleanly.",
                "The .pptx saves with the requested filename in the PowerPoint output directory.",
                "The final deck can be inspected for slide count, notes, and embedded media.",
            ],
            evidence=[
                "Saved quarterly_business_briefing.pptx in the evaluation workspace",
                "Screenshot of normal editing view with slide thumbnails",
                "Screenshot of speaker notes visible for at least one slide",
                "Screenshot during slide show mode",
                "Operator note on slide rearranging, notes entry, presentation advance, and exit reliability",
            ],
            tags=["powerpoint", "deck_creation", "speaker_notes", "slide_rearrange", "presentation_mode", "visual_design"],
        ),
        _scenario(
            scenario_id="multi_window_switching",
            title="Multi Window Switching",
            category="general_driving",
            prompt=(
                "/usecomputer Open or focus two benign applications such as Notepad and File Explorer, switch "
                "between them several times, and report which window is active after each switch."
            ),
            setup=[
                "Use benign applications only.",
                "Avoid logged-in or sensitive applications during the test.",
            ],
            success_criteria=[
                "The assistant can focus the intended window repeatedly.",
                "Window switching does not leave the desktop in a confused state.",
                "The reported active window matches what the operator sees.",
            ],
            evidence=[
                "Screenshot of both windows visible or sequential screenshots after switching",
                "Operator note on focus reliability and latency",
            ],
            tags=["focus", "window_switching", "desktop_driving"],
        ),
        _scenario(
            scenario_id="scroll_context_and_rename",
            title="Scroll Context Menu And Rename",
            category="general_driving",
            prompt=(
                f"/usecomputer In File Explorer under {paths['workspace_dir']}, scroll if needed, use a context "
                "menu or direct selection to rename bravo.txt to bravo_renamed.txt, and confirm the new name."
            ),
            setup=[
                f"Workspace root: {paths['workspace_dir']}",
                "Restore the original filename before rerunning if needed.",
            ],
            success_criteria=[
                "The assistant can scroll or reposition the view if necessary.",
                "A context or rename action is triggered correctly.",
                "The file ends with the requested new name.",
                "No extra files are renamed accidentally.",
            ],
            evidence=[
                "Screenshot showing the renamed file",
                "Operator note on right-click reliability, menu targeting, and text entry stability",
            ],
            tags=["scroll", "context_menu", "rename", "explorer", "clicking"],
        ),
    ]

    rubric = {
        "pass_threshold": "All required scenarios pass. Optional app-specific scenarios pass when the app is installed.",
        "required_scenarios": [
            "preflight_readiness",
            "explorer_navigation",
            "dragdrop_folders",
            "multi_window_switching",
            "scroll_context_and_rename",
        ],
        "metrics": [
            "task_success",
            "completion_time_seconds",
            "manual_intervention_count",
            "wrong_click_count",
            "retry_count",
            "focus_loss_events",
            "artifact_completeness",
            "formatting_fidelity",
            "formula_correctness",
            "analysis_correctness",
        ],
        "severity_guidance": {
            "critical": "Data loss, destructive action, or uncontrolled navigation",
            "major": "Task fails or needs repeated operator rescue",
            "minor": "Task succeeds but with visible hesitation or one-off misclicks",
        },
    }

    report_template = {
        "root_dir": str(root_dir),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": "not_run",
        "summary": {
            "required_passed": 0,
            "required_failed": 0,
            "optional_passed": 0,
            "optional_failed": 0,
        },
        "results": [
            {
                "id": scenario["id"],
                "title": scenario["title"],
                "status": "not_run",
                "completion_time_seconds": None,
                "manual_intervention_count": 0,
                "wrong_click_count": 0,
                "retry_count": 0,
                "focus_loss_events": 0,
                "artifacts": [],
                "operator_notes": "",
            }
            for scenario in scenarios
        ],
    }

    return {
        "root_dir": str(root_dir),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "windows_usecomputer_manual_evaluation",
        "preconditions": [
            "Use only benign local applications and the generated workspace.",
            "Keep the Windows desktop unlocked and visible.",
            "Close or hide sensitive windows before evaluation.",
            "Prefer Microsoft Word and Excel only if installed; otherwise mark those scenarios skipped.",
        ],
        "workspace": paths,
        "scenarios": scenarios,
        "rubric": rubric,
        "report_template": report_template,
    }


def write_windows_usecomputer_eval_bundle(root_dir: Path) -> dict[str, Any]:
    bundle = build_windows_usecomputer_eval_plan(root_dir)
    state_dir = root_dir / "state"
    logs_dir = root_dir / "logs"
    state_dir.mkdir(parents=True, exist_ok=True)
    logs_dir.mkdir(parents=True, exist_ok=True)

    (state_dir / "windows_usecomputer_eval_plan.json").write_text(
        json.dumps(bundle, indent=2) + "\n",
        encoding="utf-8",
    )
    (state_dir / "windows_usecomputer_eval_report_template.json").write_text(
        json.dumps(bundle["report_template"], indent=2) + "\n",
        encoding="utf-8",
    )

    readme = [
        "# Windows /usecomputer Evaluation Bundle",
        "",
        "This bundle is a repeatable manual evaluation pack for Windows desktop control quality.",
        "",
        "## What it covers",
        "- General readiness, screenshot, mouse, and safe clicking",
        "- File Explorer navigation and file selection",
        "- Drag-and-drop between folders",
        "- Optional Microsoft Word editing and save flow",
        "- Optional Microsoft Excel data entry and formula flow",
        "- Advanced Microsoft Word report formatting with tables, lists, and footer page numbering",
        "- Advanced Microsoft Excel analysis with formulas, conditional formatting, sorting/filtering, and charts",
        "- Integrated Excel-to-Word report creation with PDF export",
        "- Advanced Microsoft PowerPoint deck creation, speaker notes, slide rearranging, and presentation simulation",
        "- Multi-window switching and focus reliability",
        "- Scrolling, context menus, and rename behavior",
        "",
        "## Files",
        "- `state/windows_usecomputer_eval_plan.json`: standardized tasks and scoring rubric",
        "- `state/windows_usecomputer_eval_report_template.json`: empty results template for a run",
        "- `workspace/`: safe folders and files for Explorer and drag/drop tests",
        "- `logs/artifacts/`: suggested place to save screenshots and evidence",
        "",
        "## Suggested usage",
        "1. Run the exact prompt for one scenario at a time through `/usecomputer`.",
        "2. Save screenshots and output files under the generated workspace or logs folders.",
        "3. Fill the report template after each scenario with pass/fail, retries, and notes.",
        "4. Compare multiple runs over time to identify regressions and improvements.",
        "",
    ]
    (root_dir / "README.md").write_text("\n".join(readme), encoding="utf-8")
    return bundle


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a Windows /usecomputer evaluation bundle")
    parser.add_argument("command", choices=["build"])
    parser.add_argument("--root", required=True, help="Output directory for the evaluation bundle")
    args = parser.parse_args()

    bundle = write_windows_usecomputer_eval_bundle(Path(args.root))
    print(json.dumps(bundle))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
