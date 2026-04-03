from __future__ import annotations

import json
import shutil
from pathlib import Path

from flow.engine.flow_runner import FlowRunner


ROOT = Path(__file__).resolve().parents[2]
RUNS_ROOT = ROOT / "flow" / "runs"
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
        timeout_seconds: int = 600,
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
    shutil.rmtree(RUNS_ROOT / run_id, ignore_errors=True)

    notifier = RecordingNotifier()
    evaluator = RecordingEvaluator()
    handler = FixtureStepHandler(tmp_path)

    runner = FlowRunner(
        str(SMOKE_FIXTURE),
        run_id=run_id,
        runs_root=RUNS_ROOT,
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
        for line in (RUNS_ROOT / run_id / "events.jsonl").read_text(encoding="utf-8").splitlines()
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
