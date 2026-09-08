from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from nagare import cli as nagare_cli
from nagare.engine.runner import FlowRunner
from nagare.yaml.codec import load_workflow_document, load_workflow_file

ROOT = Path(__file__).resolve().parents[2]
NAGARE_ROOT = ROOT / "nagare"

FORBIDDEN_IMPORT_ROOTS = {"flow", "hashi", "tools"}
RETIRED_WORKFLOW_KEYS = {
    "auto_apply",
    "improvement_threshold",
    "max_attempts",
    "max_retries",
    "max_total_attempts",
    "on_max_exceeded",
    "retry_strategy",
    "timeout_seconds",
    "wait_for_human_timeout_seconds",
}

RETIRED_WORKER_KEYS = {"workspace", "controllable_by"}


def _published_workflow_paths() -> list[Path]:
    return sorted(
        [
            *ROOT.glob("flow/workflows/examples/*.yaml"),
            *ROOT.glob("flow/workflows/library/*.yaml"),
            *ROOT.glob("flow/minato/*/shimanto/*/nagare/*/workflow.yaml"),
        ]
    )


def _walk_keys(value, *, path: str = ""):
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}" if path else str(key)
            yield child_path, key
            yield from _walk_keys(child, path=child_path)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _walk_keys(child, path=f"{path}[{index}]")


def test_nagare_package_has_no_forbidden_runtime_imports() -> None:
    for path in NAGARE_ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots = {alias.name.split(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                roots = {node.module.split(".")[0]}
            else:
                continue
            forbidden = roots & FORBIDDEN_IMPORT_ROOTS
            assert not forbidden, f"{path} imports forbidden runtime module(s): {sorted(forbidden)}"


def test_published_workflows_are_resolvable_and_use_current_runtime_contract() -> None:
    paths = _published_workflow_paths()
    assert paths

    problems: list[str] = []
    for workflow_path in paths:
        document = load_workflow_file(workflow_path)
        if not document.graph_validation.is_valid:
            problems.append(f"{workflow_path}: invalid DAG {document.graph_validation}")
        if document.unknown_top_level_keys:
            problems.append(
                f"{workflow_path}: unknown top-level keys {document.unknown_top_level_keys}"
            )

        workflow = document.data
        for worker in workflow.get("agents", {}).get("workers", []):
            agent_md = worker.get("agent_md")
            if worker.get("backend") != "callable" and (
                not isinstance(agent_md, str) or not (ROOT / agent_md).is_file()
            ):
                problems.append(
                    f"{workflow_path}: missing agent_md for {worker.get('id')}: {agent_md}"
                )
            if worker.get("backend") not in {"claude-cli", "codex-cli", "callable"}:
                problems.append(
                    f"{workflow_path}: unsupported backend for {worker.get('id')}: "
                    f"{worker.get('backend')}"
                )
            ignored = sorted(RETIRED_WORKER_KEYS.intersection(worker))
            if ignored:
                problems.append(
                    f"{workflow_path}: worker {worker.get('id')} uses ignored fields: {ignored}"
                )

        for key_path, key in _walk_keys(workflow):
            if key in RETIRED_WORKFLOW_KEYS:
                problems.append(f"{workflow_path}: retired key {key_path}")

        for step in workflow.get("steps", []):
            if step.get("wait_for_human") and step.get("strategy") == "parallel":
                problems.append(
                    f"{workflow_path}: wait_for_human step cannot run in parallel: {step.get('id')}"
                )

    assert problems == []


def test_published_workflows_load_under_the_enforced_runtime_contract(tmp_path) -> None:
    for index, workflow_path in enumerate(_published_workflow_paths()):
        runner = FlowRunner(
            str(workflow_path),
            run_id=f"run-published-contract-{index}",
            runs_root=tmp_path / "runs",
            repo_root=ROOT,
        )
        assert runner.workflow["workflow"]["id"]


def test_retired_per_worker_config_layer_is_not_published() -> None:
    assert list((ROOT / "flow" / "agents").glob("**/config.json")) == []
    assert not (ROOT / "flow" / "schema" / "agent.schema.yaml").exists()
    assert not (ROOT / "flow" / "schema" / "workflow.schema.yaml").exists()


def test_runtime_fixtures_match_their_published_workflows_exactly() -> None:
    pairs = {
        ROOT / "flow/workflows/examples/smoke_test.yaml": ROOT / "tests/fixtures/smoke_test.yaml",
        ROOT / "flow/workflows/examples/meta_workflow_creation.yaml": ROOT
        / "tests/fixtures/meta_workflow_creation.yaml",
        ROOT / "flow/workflows/library/book_translation.yaml": ROOT
        / "tests/fixtures/book_translation.yaml",
        ROOT
        / "flow/minato/ai-consulting/shimanto/workflow-work/nagare/academic-writing-paragraph/workflow.yaml": ROOT
        / "tests/fixtures/academic_writing_paragraph.yaml",
    }
    for published, fixture in pairs.items():
        assert fixture.read_bytes() == published.read_bytes(), (
            f"Fixture drifted from published workflow: {fixture} != {published}"
        )


def test_public_evaluation_knowledge_base_contains_only_evidence_empty_templates() -> None:
    expected = {
        ROOT / "flow/evaluation_kb/patterns/successful.yaml": ("patterns", "total_patterns"),
        ROOT / "flow/evaluation_kb/patterns/failure.yaml": ("patterns", "total_patterns"),
    }
    for path, (items_key, count_key) in expected.items():
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert payload[items_key] == []
        assert payload[count_key] == 0

    benchmarks = yaml.safe_load(
        (ROOT / "flow/evaluation_kb/model_performance/benchmarks.yaml").read_text(
            encoding="utf-8"
        )
    )
    assert benchmarks["models"] == {}
    assert benchmarks["task_model_matrix"] == {}


def test_meta_workflow_delivers_a_reviewed_bundle_without_self_modifying_claims() -> None:
    path = ROOT / "flow/workflows/examples/meta_workflow_creation.yaml"
    source = path.read_text(encoding="utf-8")
    workflow = load_workflow_file(path).data
    step_ids = [step["id"] for step in workflow["steps"]]

    assert workflow["workflow"]["version"] == "2.0.0"
    assert step_ids[-3:] == [
        "validate_workflow_bundle",
        "independent_release_review",
        "package_release",
    ]
    assert workflow["output"]["source_artifact"] == "release_bundle"
    assert all("model" not in worker for worker in workflow["agents"]["workers"])
    for stale_reference in (
        "patterns/common_failures.yaml",
        "patterns/model_performance.yaml",
        "improvements/applied.yaml",
        "improvements/pending.yaml",
        "workflow_scores/scores.jsonl",
        "flow/workflows/library/{workflow_id}_candidate.yaml",
    ):
        assert stale_reference not in source


def test_workflow_schema_does_not_advertise_retired_runtime_controls() -> None:
    schema = load_workflow_document(
        (ROOT / "flow/workflows/schema/workflow_schema.yaml").read_text(encoding="utf-8")
    ).data
    retired = [path for path, key in _walk_keys(schema) if key in RETIRED_WORKFLOW_KEYS]
    assert retired == []


def test_python_module_and_cli_help_resolve() -> None:
    import_result = subprocess.run(
        [sys.executable, "-c", "import nagare; print(nagare.__all__)"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert import_result.returncode == 0, import_result.stderr
    assert "FlowRunner" in import_result.stdout

    cli_result = subprocess.run(
        [sys.executable, "-m", "nagare.cli", "--help"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert cli_result.returncode == 0, cli_result.stderr
    assert "run" in cli_result.stdout
    assert "status" in cli_result.stdout
    assert "resume" in cli_result.stdout
    assert "api" in cli_result.stdout
    assert "eval" not in cli_result.stdout


def test_resume_cli_clears_only_a_valid_run_pause_signal(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setattr(nagare_cli, "RUNS_ROOT", tmp_path)
    run_dir = tmp_path / "run-valid"
    run_dir.mkdir()
    (run_dir / "state.json").write_text("{}\n", encoding="utf-8")
    pause = run_dir / "_pause"
    pause.write_text("operator pause\n", encoding="utf-8")

    nagare_cli.cmd_resume(SimpleNamespace(run_id="run-valid"))

    assert not pause.exists()
    assert "已解除暂停信号" in capsys.readouterr().out

    with pytest.raises(SystemExit):
        nagare_cli.cmd_resume(SimpleNamespace(run_id="../outside"))
