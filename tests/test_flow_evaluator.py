from __future__ import annotations

import json
import logging
import subprocess
import sys
from pathlib import Path

import pytest

from flow.agents.evaluator import evaluator as evaluator_module
from nagare.logging.events import RunEventLogger


def _write_events(root: Path, run_id: str, *, debug_count: int = 0) -> None:
    run_dir = root / run_id
    run_dir.mkdir(parents=True)
    events = [
        {
            "workflow_id": "sample-workflow",
            "event_type": "workflow_started",
            "ts": "2026-09-06T00:00:00+00:00",
            "data": {},
        },
        *[
            {
                "workflow_id": "sample-workflow",
                "event_type": "debug_started",
                "ts": f"2026-09-06T00:00:0{index + 1}+00:00",
                "data": {"step_id": "translate"},
            }
            for index in range(debug_count)
        ],
        {
            "workflow_id": "sample-workflow",
            "event_type": "step_completed",
            "ts": "2026-09-06T00:00:05+00:00",
            "data": {"step_id": "translate", "duration_seconds": 5},
        },
        {
            "workflow_id": "sample-workflow",
            "event_type": "workflow_completed",
            "ts": "2026-09-06T00:00:10+00:00",
            "data": {},
        },
    ]
    (run_dir / "evaluation_events.jsonl").write_text(
        "".join(json.dumps(event) + "\n" for event in events),
        encoding="utf-8",
    )


def _evaluator(tmp_path: Path, monkeypatch) -> evaluator_module.FlowEvaluator:
    monkeypatch.setattr(evaluator_module, "KB_PATH", tmp_path / "evaluation_kb")
    monkeypatch.setattr(evaluator_module, "RUNS_PATH", tmp_path / "runs")
    return evaluator_module.FlowEvaluator()


def test_evaluator_persists_truthful_report_and_recommendations(
    tmp_path: Path,
    monkeypatch,
) -> None:
    evaluator = _evaluator(tmp_path, monkeypatch)
    _write_events(evaluator_module.RUNS_PATH, "run-001", debug_count=2)

    report = evaluator.evaluate_run("run-001")

    assert report["success"] is True
    assert report["scores"] == {
        "stability": 4.0,
        "efficiency": None,
        "intervention": 10.0,
        "quality": None,
        "overall": 7.0,
        "coverage": 0.5,
        "measured_dimensions": ["stability", "intervention"],
    }
    assert len(report["recommendations"]) == 1
    assert report["recommendations"][0]["class"] == "B"

    persisted = json.loads(
        (
            evaluator_module.RUNS_PATH
            / "run-001"
            / "evaluation_report.json"
        ).read_text(encoding="utf-8")
    )
    assert persisted == report
    score_rows = (
        evaluator_module.KB_PATH / "workflow_scores" / "scores.jsonl"
    ).read_text(encoding="utf-8").splitlines()
    assert len(score_rows) == 1


def test_evaluator_threshold_requests_review_without_claiming_auto_change(
    tmp_path: Path,
    monkeypatch,
    caplog,
) -> None:
    evaluator = _evaluator(tmp_path, monkeypatch)
    caplog.set_level(logging.INFO, logger="flow.evaluator")

    for index in range(5):
        run_id = f"run-{index}"
        _write_events(evaluator_module.RUNS_PATH, run_id)
        evaluator.evaluate_run(run_id)

    assert "已达到人工模式复核阈值" in caplog.text
    assert "未自动修改知识库或工作流" in caplog.text


def test_evaluator_consumes_real_legacy_event_translation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    evaluator = _evaluator(tmp_path, monkeypatch)
    logger = RunEventLogger(
        run_id="run-real-events",
        trace_id="trace-real-events",
        workflow_id="sample-workflow",
        workflow_path="workflow.yaml",
        runs_root=evaluator_module.RUNS_PATH,
    )
    logger.emit("run.started", message="started")
    logger.emit(
        "handler.invoke.started",
        message="Debug handler started",
        step_id="translate",
        data={"debug_agent": "debug"},
    )
    logger.emit(
        "step.failed",
        message="failed once",
        step_id="translate",
        error_message="retryable",
    )
    logger.emit(
        "step.completed",
        message="recovered",
        step_id="translate",
        duration_ms=2500,
    )
    logger.emit(
        "step.waiting_human",
        message="review requested",
        step_id="review",
        data={"question_count": 1},
    )
    logger.emit("run.completed", message="completed")

    report = evaluator.evaluate_run("run-real-events")

    assert report["metrics"]["debug_interventions"] == 1
    assert report["metrics"]["human_interventions"] == 1
    assert report["metrics"]["step_durations"] == {"translate": 2.5}
    assert report["metrics"]["failed_steps"] == 1
    assert report["scores"]["stability"] == 7.0
    assert report["scores"]["intervention"] == 8.0


def test_flow_cli_does_not_advertise_unimplemented_resume_command() -> None:
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, str(root / "flow" / "flow_cli.py"), "--help"],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert "resume" not in result.stdout


def test_evaluator_rejects_run_id_path_traversal(tmp_path: Path) -> None:
    evaluator = evaluator_module.FlowEvaluator(
        runs_path=tmp_path / "runs",
        kb_path=tmp_path / "kb",
    )

    with pytest.raises(ValueError, match="run_id"):
        evaluator.evaluate_run("../escape")


def test_flow_cli_rejects_run_id_path_traversal() -> None:
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, str(root / "flow" / "flow_cli.py"), "eval", "../escape"],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )

    assert result.returncode == 1
    assert "Run ID 无效" in result.stdout
