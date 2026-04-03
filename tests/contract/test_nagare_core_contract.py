from __future__ import annotations

import json
import shutil
from pathlib import Path

from flow.engine.task_state import TaskState as FlowTaskState
from flow.engine.worker_dispatcher import WorkerDispatcher as FlowWorkerDispatcher
from nagare.engine.runner import FlowRunner
from nagare.engine.state import TaskState
from nagare.handlers.subprocess_handler import WorkerDispatcher as NagareWorkerDispatcher


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
        return {"scores": {"overall": 10}}


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
        self.calls.append(
            {
                "agent_id": agent_id,
                "task_id": task_message["task_id"],
                "input_artifacts": dict(task_message["payload"].get("input_artifacts", {})),
            }
        )
        step_id = task_message["payload"]["step_id"]
        if step_id == "step_write":
            output_path = self.tmp_path / "output.txt"
            output_path.write_text("AI 协作让复杂问题更清晰", encoding="utf-8")
            return {
                "status": "completed",
                "artifacts_produced": {"quote": str(output_path)},
                "summary": "wrote quote",
            }

        assert task_message["payload"]["input_artifacts"]["quote"]
        review_path = self.tmp_path / "review.txt"
        review_path.write_text("AI 协作让复杂问题更清晰 - 评分：9/10 - 理由：简洁。", encoding="utf-8")
        return {
            "status": "completed",
            "artifacts_produced": {"review": str(review_path)},
            "summary": "reviewed quote",
        }


def test_nagare_runner_executes_fixture_with_injected_protocols(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(ROOT)
    run_id = "run-contract-nagare-core"
    shutil.rmtree(RUNS_ROOT / run_id, ignore_errors=True)

    notifier = RecordingNotifier()
    evaluator = RecordingEvaluator()
    handler = FixtureStepHandler(tmp_path)

    runner = FlowRunner(
        str(SMOKE_FIXTURE),
        run_id=run_id,
        runs_root=RUNS_ROOT,
        repo_root=ROOT,
        notifier=notifier,
        evaluator=evaluator,
        step_handler=handler,
    )
    runner.workflow["inter_step_wait_seconds"] = 0
    result = runner.start()

    assert result["success"] is True
    assert set(result["completed_steps"]) == {"step_write", "step_check"}
    assert evaluator.run_ids == [run_id]
    assert len(handler.calls) == 2
    assert handler.calls[1]["input_artifacts"]["quote"].endswith("output.txt")
    assert notifier.messages == []

    events_path = RUNS_ROOT / run_id / "events.jsonl"
    event_names = [json.loads(line)["event"] for line in events_path.read_text(encoding="utf-8").splitlines()]
    assert "run.started" in event_names
    assert "step.completed" in event_names

    state = TaskState(run_id, runs_root=RUNS_ROOT)
    snapshot = state.get_runtime_snapshot()
    assert snapshot["status"] == "COMPLETED"
    assert snapshot["completed_steps"] == ["step_check", "step_write"]


def test_flow_compatibility_imports_resolve_to_extracted_core() -> None:
    assert FlowTaskState is TaskState
    assert FlowWorkerDispatcher is NagareWorkerDispatcher
