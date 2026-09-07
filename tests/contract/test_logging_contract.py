from __future__ import annotations

import json

from flow.engine.runtime_logging import RunEventLogger
from flow.engine.task_state import TaskState


def test_run_event_logger_writes_contract_envelope_and_legacy_event(tmp_path) -> None:
    run_id = "run-contract-logging"
    run_dir = tmp_path / run_id

    logger = RunEventLogger(
        run_id=run_id,
        trace_id="trace-123",
        workflow_id="smoke-test",
        workflow_path="tests/fixtures/smoke_test.yaml",
        runs_root=tmp_path,
    )
    logger.emit(
        "step.completed",
        message="Step execution completed",
        step_id="step_write",
        duration_ms=1250,
        data={"agent_id": "writer_01"},
    )

    event_record = json.loads((run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert event_record["event"] == "step.completed"
    assert event_record["level"] == "INFO"
    assert event_record["run_id"] == run_id
    assert event_record["trace_id"] == "trace-123"
    assert event_record["workflow_id"] == "smoke-test"
    assert event_record["workflow_path"] == "tests/fixtures/smoke_test.yaml"
    assert event_record["step_id"] == "step_write"
    assert event_record["duration_ms"] == 1250
    assert event_record["data"] == {"agent_id": "writer_01"}

    legacy_record = json.loads(
        (run_dir / "evaluation_events.jsonl").read_text(encoding="utf-8").splitlines()[0]
    )
    assert legacy_record["event_type"] == "step_completed"
    assert legacy_record["run_id"] == run_id
    assert legacy_record["data"]["step_id"] == "step_write"
    assert legacy_record["data"]["duration_seconds"] == 1.25


def test_waiting_human_event_maps_to_legacy_intervention(tmp_path) -> None:
    logger = RunEventLogger(
        run_id="run-human-wait",
        trace_id="trace-human-wait",
        workflow_id="human-wait",
        workflow_path="workflow.yaml",
        runs_root=tmp_path,
    )

    logger.emit(
        "step.waiting_human",
        message="Step is waiting for human clarification",
        step_id="review",
        data={"question_count": 2},
    )

    record = json.loads(
        (tmp_path / "run-human-wait" / "evaluation_events.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()[0]
    )
    assert record["event_type"] == "human_intervention"
    assert record["data"] == {"question_count": 2, "step_id": "review"}


def test_task_state_runtime_snapshot_matches_public_contract(tmp_path) -> None:
    run_id = "run-contract-snapshot"

    state = TaskState(run_id, runs_root=tmp_path)
    state.set_workflow_metadata(
        workflow_id="smoke-test",
        workflow_version="1.0.0",
        workflow_path="tests/fixtures/smoke_test.yaml",
    )
    state.set_workflow_status("running")
    state.set_step_status("step_write", "running")
    state.set_step_status("step_done", "completed", artifacts={"draft": "out.txt"})
    state.set_step_status("step_fail", "failed", error="boom")

    snapshot = state.get_runtime_snapshot()

    assert snapshot["run_id"] == run_id
    assert snapshot["workflow_id"] == "smoke-test"
    assert snapshot["workflow_version"] == "1.0.0"
    assert snapshot["status"] == "RUNNING"
    assert snapshot["current_steps"] == ["step_write"]
    assert snapshot["completed_steps"] == ["step_done"]
    assert snapshot["failed_steps"] == ["step_fail"]
    assert snapshot["waiting_human_steps"] == []
    assert snapshot["step_status"]["step_write"]["status"] == "RUNNING"
    assert snapshot["step_status"]["step_write"]["attempt"] == 1
    assert snapshot["step_status"]["step_done"]["attempt"] == 0
    assert snapshot["step_status"]["step_done"]["artifacts"] == {"draft": "out.txt"}
    assert snapshot["step_status"]["step_fail"]["error"] == "boom"
