from __future__ import annotations

import json
import shutil
from pathlib import Path

from nagare.engine.runner import FlowRunner
from nagare.yaml.codec import load_workflow_document, load_workflow_file, validate_workflow_graph


ROOT = Path(__file__).resolve().parents[2]
FIXTURES_DIR = ROOT / "tests" / "fixtures"
MANIFEST_PATH = FIXTURES_DIR / "manifest.json"
RUNS_ROOT = ROOT / "flow" / "runs"


def load_manifest() -> dict:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def test_fixture_manifest_references_existing_files() -> None:
    manifest = load_manifest()

    assert manifest["version"] == 1
    assert manifest["fixtures"], "Phase 0 requires a non-empty fixture corpus."

    for fixture in manifest["fixtures"]:
        fixture_path = ROOT / fixture["path"]
        assert fixture_path.exists(), f"Missing fixture file: {fixture['path']}"
        assert fixture_path.read_text(encoding="utf-8").strip(), (
            f"Fixture is empty: {fixture['path']}"
        )


def test_round_trip_must_preserve_unknown_fields_and_editor_metadata() -> None:
    source = (FIXTURES_DIR / "unknown_fields_workflow.yaml").read_text(encoding="utf-8")
    document = load_workflow_document(source, workflow_path=FIXTURES_DIR / "unknown_fields_workflow.yaml")
    exported = document.export()

    assert "x-team-note:" in exported
    assert "x-worker-extension:" in exported
    assert "x-step-note:" in exported
    assert "x-nagare-viz:" in exported
    assert "Phase 0 fixture: explicit round-trip preservation coverage." in exported
    assert exported == source
    assert document.compatibility_class == "B"
    assert {warning.code for warning in document.warnings} >= {
        "comments-present",
        "unknown-top-level-fields",
    }


def test_editor_metadata_update_preserves_unknown_fields_and_comments() -> None:
    source = (FIXTURES_DIR / "unknown_fields_workflow.yaml").read_text(encoding="utf-8")
    document = load_workflow_document(source)

    exported = document.export(
        editor_metadata={
            "version": 1,
            "nodes": {
                "draft": {"position": {"x": 360, "y": 240}},
                "review": {"position": {"x": 600, "y": 240}},
            },
            "viewport": {"x": 0, "y": 0, "zoom": 0.9},
        }
    )

    assert "Phase 0 fixture: explicit round-trip preservation coverage." in exported
    assert "x-team-note:" in exported
    assert "x-worker-extension:" in exported
    assert "x-step-note:" in exported

    reloaded = load_workflow_document(exported)
    assert reloaded.data["x-nagare-viz"]["nodes"]["draft"]["position"] == {"x": 360, "y": 240}
    assert reloaded.data["x-nagare-viz"]["nodes"]["review"]["position"] == {"x": 600, "y": 240}
    assert reloaded.data["x-team-note"]["ticket"] == "PHASE0-RT-001"


def test_legacy_fixture_is_downgraded_to_raw_yaml_mode() -> None:
    document = load_workflow_file(FIXTURES_DIR / "legacy_english_news_to_chinese_markdown.yaml")

    assert document.compatibility_class == "C"
    assert "legacy-dialect" in {warning.code for warning in document.warnings}
    assert document.export() == document.source


def test_dag_validator_detects_cycles_missing_references_and_duplicates() -> None:
    result = validate_workflow_graph(
        {
            "agents": {"workers": [{"id": "writer_01"}]},
            "steps": [
                {"id": "prepare", "agent": "writer_01", "depends": ["missing-step"]},
                {"id": "draft", "agent": "writer_01", "depends": ["review"]},
                {"id": "review", "agent": "ghost_01", "depends": ["draft"]},
                {"id": "draft", "agent": "writer_01", "depends": ["review"]},
            ],
        }
    )

    assert result.is_valid is False
    assert result.duplicate_step_ids == ("draft",)
    assert result.missing_dependencies == ("prepare->missing-step",)
    assert result.missing_agents == ("review->ghost_01",)
    assert result.cycles == (("draft", "review", "draft"),)


class FixtureStepHandler:
    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path

    def execute(
        self,
        agent_id: str,
        task_message: dict,
        agent_md_path: str,
        timeout_seconds: int = 600,
        backend: str = "claude-cli",
        model: str = "",
    ) -> dict:
        step_id = task_message["payload"]["step_id"]
        if step_id == "step_write":
            output_path = self.tmp_path / "output.txt"
            output_path.write_text("phase 4 export check", encoding="utf-8")
            return {
                "status": "completed",
                "artifacts_produced": {"quote": str(output_path)},
                "summary": "wrote quote",
            }

        review_path = self.tmp_path / "review.txt"
        review_path.write_text("phase 4 review", encoding="utf-8")
        return {
            "status": "completed",
            "artifacts_produced": {"review": str(review_path)},
            "summary": "reviewed quote",
        }


def test_exported_yaml_still_executes_in_engine(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(ROOT)
    run_id = "run-contract-round-trip-export"
    shutil.rmtree(RUNS_ROOT / run_id, ignore_errors=True)

    source = (FIXTURES_DIR / "smoke_test.yaml").read_text(encoding="utf-8")
    document = load_workflow_document(source, workflow_path=FIXTURES_DIR / "smoke_test.yaml")
    exported = document.export(
        editor_metadata={
            "version": 1,
            "nodes": {
                "step_write": {"position": {"x": 120, "y": 80}},
                "step_check": {"position": {"x": 420, "y": 80}},
            },
        }
    )
    exported_path = tmp_path / "smoke_test_exported.yaml"
    exported_path.write_text(exported, encoding="utf-8")

    runner = FlowRunner(
        str(exported_path),
        run_id=run_id,
        runs_root=RUNS_ROOT,
        repo_root=ROOT,
        step_handler=FixtureStepHandler(tmp_path),
    )
    runner.workflow["inter_step_wait_seconds"] = 0

    result = runner.start()

    assert result["success"] is True
    assert set(result["completed_steps"]) == {"step_write", "step_check"}
