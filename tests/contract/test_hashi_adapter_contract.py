from __future__ import annotations

import json
from pathlib import Path

from flow.adapters.hashi import HASHIStepHandler
from flow.engine.flow_runner import FlowRunner
from nagare.logging.events import RunEventLogger

ROOT = Path(__file__).resolve().parents[2]
SMOKE_FIXTURE = ROOT / "tests" / "fixtures" / "smoke_test.yaml"


class RecordingNotifier:
    def __init__(self) -> None:
        self.messages: list[dict] = []

    def send(self, *, agent_id: str, text: str, run_id: str | None = None, workflow_id: str | None = None) -> None:
        self.messages.append(
            {"agent_id": agent_id, "text": text, "run_id": run_id, "workflow_id": workflow_id}
        )


class RecordingEvaluator:
    def __init__(self) -> None:
        self.run_ids: list[str] = []

    def evaluate_run(self, run_id: str) -> dict:
        self.run_ids.append(run_id)
        return {"scores": {"overall": 9.5}}


class FixtureStepHandler:
    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.calls: list[dict] = []

    def execute(
        self,
        agent_id: str,
        task_message: dict,
        agent_md_path: str,
        backend: str = "claude-cli",
        model: str = "",
    ) -> dict:
        step_id = task_message["payload"]["step_id"]
        self.calls.append({"agent_id": agent_id, "step_id": step_id, "task_id": task_message["task_id"]})
        if step_id == "step_write":
            output_path = self.tmp_path / "output.txt"
            output_path.write_text("adapter path quote", encoding="utf-8")
            return {
                "status": "completed",
                "artifacts_produced": {"quote": str(output_path)},
                "summary": "wrote quote",
            }

        review_path = self.tmp_path / "review.txt"
        review_path.write_text("adapter review", encoding="utf-8")
        return {
            "status": "completed",
            "artifacts_produced": {"review": str(review_path)},
            "summary": "reviewed quote",
        }


def test_hashi_flow_runner_uses_adapter_layer_with_correlation_logging(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(ROOT)
    run_id = "run-contract-hashi-adapter"
    runs_root = tmp_path / "runs"

    notifier = RecordingNotifier()
    evaluator = RecordingEvaluator()
    handler = FixtureStepHandler(tmp_path)

    runner = FlowRunner(
        str(SMOKE_FIXTURE),
        run_id=run_id,
        runs_root=runs_root,
        repo_root=ROOT,
        step_handler=handler,
        notifier=notifier,
        evaluator=evaluator,
    )
    runner.workflow["agents"]["orchestrator"]["human_interface"] = "akane"
    runner._human_interface = "akane"
    runner.workflow["inter_step_wait_seconds"] = 0

    result = runner.start()

    assert result["success"] is True
    assert len(handler.calls) == 2
    assert evaluator.run_ids == [run_id]
    assert len(notifier.messages) >= 2
    assert all(message["run_id"] == run_id for message in notifier.messages)
    assert all(message["workflow_id"] == "smoke-test" for message in notifier.messages)

    events = [
        json.loads(line)
        for line in (runs_root / run_id / "events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    adapter_events = [event for event in events if event["event"].startswith("adapter.")]
    assert adapter_events
    assert {event["event"] for event in adapter_events} >= {
        "adapter.step_handler.started",
        "adapter.step_handler.completed",
        "adapter.notifier.started",
        "adapter.notifier.completed",
        "adapter.evaluator.started",
        "adapter.evaluator.completed",
    }

    trace_ids = {event["trace_id"] for event in adapter_events}
    assert trace_ids == {runner.trace_id}
    assert all(event["run_id"] == run_id for event in adapter_events)

    step_requests = {
        event["request_id"]
        for event in adapter_events
        if event["event"].startswith("adapter.step_handler")
    }
    assert step_requests == {call["task_id"] for call in handler.calls}


def test_hashi_adapter_treats_recovered_debug_result_as_completed_event(tmp_path) -> None:
    class RecoveredDelegate:
        def execute(self, **kwargs):
            del kwargs
            return {"status": "recovered"}

    event_logger = RunEventLogger(
        run_id="run-adapter-recovered",
        trace_id="trace-adapter-recovered",
        workflow_id="adapter-contract",
        workflow_path=None,
        runs_root=tmp_path / "runs",
    )
    handler = HASHIStepHandler(RecoveredDelegate(), event_logger=event_logger)

    result = handler.execute(
        agent_id="debug",
        task_message={"task_id": "debug-task", "payload": {"step_id": "work"}},
        agent_md_path="debug.md",
    )

    assert result["status"] == "recovered"
    events = [
        json.loads(line)
        for line in event_logger.events_path.read_text(encoding="utf-8").splitlines()
    ]
    assert events[-1]["event"] == "adapter.step_handler.completed"


def test_hashi_default_evaluator_uses_runner_specific_roots(tmp_path) -> None:
    runs_root = tmp_path / "custom-runs"
    runner = FlowRunner(
        str(SMOKE_FIXTURE),
        run_id="run-adapter-evaluator-root",
        runs_root=runs_root,
        repo_root=tmp_path,
        step_handler=FixtureStepHandler(tmp_path),
    )
    runner.workflow["inter_step_wait_seconds"] = 0

    result = runner.start()

    assert result["success"] is True
    report_path = runs_root / runner.run_id / "evaluation_report.json"
    assert json.loads(report_path.read_text(encoding="utf-8"))["success"] is True
    assert (tmp_path / "flow" / "evaluation_kb" / "workflow_scores" / "scores.jsonl").is_file()
