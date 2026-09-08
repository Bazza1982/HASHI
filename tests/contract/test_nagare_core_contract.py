from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from flow.engine.task_state import TaskState as FlowTaskState
from flow.engine.worker_dispatcher import WorkerDispatcher as FlowWorkerDispatcher
from nagare.engine.artifacts import ArtifactStore
from nagare.engine.callable_setup_manager import CallableSetupManager
from nagare.engine.preflight import PreFlightCollector, load_prefill_from_file
from nagare.engine.runner import FlowRunner
from nagare.engine.state import TaskState
from nagare.handlers.callable_handler import CallableStepHandler
from nagare.handlers.deterministic_handler import DeterministicStepHandler
from nagare.handlers.subprocess_handler import (
    WorkerDispatcher as NagareWorkerDispatcher,
)
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
        return {"scores": {"overall": 10}}


class NoArtifactHandler:
    def execute(
        self,
        agent_id: str,
        task_message: dict,
        agent_md_path: str,
        backend: str = "claude-cli",
        model: str = "",
    ) -> dict:
        del agent_id, task_message, agent_md_path, backend, model
        return {"status": "completed", "artifacts_produced": {}}


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


def _write_candidate_fixture(path: Path, *, version: str) -> None:
    path.write_text(
        f"""
workflow:
  id: candidate-contract
  name: Candidate contract
  version: {version}
agents:
  orchestrator:
    id: orchestrator
  workers:
    - id: noop_worker
      role: No-op test worker
      backend: callable
steps:
  - id: noop
    name: No-op
    agent: noop_worker
    prompt: Complete without artifacts.
""".strip()
        + "\n",
        encoding="utf-8",
    )


def test_nagare_runner_executes_fixture_with_injected_protocols(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(ROOT)
    run_id = "run-contract-nagare-core"
    runs_root = tmp_path / "runs"

    notifier = RecordingNotifier()
    evaluator = RecordingEvaluator()
    handler = FixtureStepHandler(tmp_path)

    runner = FlowRunner(
        str(SMOKE_FIXTURE),
        run_id=run_id,
        runs_root=runs_root,
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

    events_path = runs_root / run_id / "events.jsonl"
    event_names = [json.loads(line)["event"] for line in events_path.read_text(encoding="utf-8").splitlines()]
    assert "run.started" in event_names
    assert "step.completed" in event_names

    state = TaskState(run_id, runs_root=runs_root)
    snapshot = state.get_runtime_snapshot()
    assert snapshot["status"] == "COMPLETED"
    assert snapshot["completed_steps"] == ["step_check", "step_write"]


def test_flow_compatibility_imports_resolve_to_extracted_core() -> None:
    assert FlowTaskState is TaskState
    assert FlowWorkerDispatcher is NagareWorkerDispatcher


@pytest.mark.parametrize(
    ("version", "steps", "expected"),
    [
        ("../../escape", "[]", "MAJOR.MINOR.PATCH"),
        ("1.0.0", "[]", "at least one executable step"),
    ],
)
def test_workflow_contract_rejects_unsafe_version_and_empty_noop(
    tmp_path,
    version,
    steps,
    expected,
) -> None:
    workflow_path = tmp_path / "invalid-contract.yaml"
    workflow_path.write_text(
        f"""
workflow:
  id: invalid-contract
  name: Invalid contract
  version: "{version}"
agents:
  orchestrator:
    id: orchestrator
  workers:
    - id: worker
      role: Worker
      backend: callable
steps: {steps}
""".strip()
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=expected):
        FlowRunner(
            str(workflow_path),
            run_id="run-invalid-contract",
            runs_root=tmp_path / "runs",
            repo_root=tmp_path,
        )


def test_candidate_success_promotes_exact_trialled_workflow(tmp_path) -> None:
    workflow_path = tmp_path / "sample.yaml"
    candidate_path = tmp_path / "sample_candidate.yaml"
    _write_candidate_fixture(workflow_path, version="1.0.0")
    _write_candidate_fixture(candidate_path, version="1.1.0")

    runner = FlowRunner(
        str(workflow_path),
        run_id="run-candidate-success",
        runs_root=tmp_path / "runs",
        repo_root=tmp_path,
    )
    trialled_bytes = candidate_path.read_bytes()

    assert runner._using_candidate is True
    assert runner.workflow["workflow"]["version"] == "1.1.0"

    runner._handle_candidate_result(True)

    assert workflow_path.read_bytes() == trialled_bytes
    assert not candidate_path.exists()
    assert (
        tmp_path
        / "flow"
        / "evaluation_kb"
        / "workflow_versions"
        / "candidate-contract"
        / "v1.1.0_promoted.yaml"
    ).read_bytes() == trialled_bytes


def test_candidate_failure_preserves_canonical_workflow_and_removes_trial(tmp_path) -> None:
    workflow_path = tmp_path / "sample.yaml"
    candidate_path = tmp_path / "sample_candidate.yaml"
    _write_candidate_fixture(workflow_path, version="1.0.0")
    canonical_bytes = workflow_path.read_bytes()
    _write_candidate_fixture(candidate_path, version="1.1.0")

    runner = FlowRunner(
        str(workflow_path),
        run_id="run-candidate-failure",
        runs_root=tmp_path / "runs",
        repo_root=tmp_path,
    )
    runner._handle_candidate_result(False)

    assert workflow_path.read_bytes() == canonical_bytes
    assert not candidate_path.exists()
    assert not (tmp_path / "flow" / "evaluation_kb" / "workflow_versions").exists()


def test_wait_for_human_records_state_and_evaluator_event(tmp_path) -> None:
    workflow_path = tmp_path / "sample.yaml"
    _write_candidate_fixture(workflow_path, version="1.0.0")
    question_path = tmp_path / "questions.json"
    question_path.write_text(
        json.dumps({"clarification_questions": ["Approve the draft?"]}),
        encoding="utf-8",
    )
    runs_root = tmp_path / "runs"
    runner = FlowRunner(
        str(workflow_path),
        run_id="run-human-contract",
        runs_root=runs_root,
        repo_root=tmp_path,
    )

    runner._handle_wait_for_human(
        {"id": "review", "agent": "reviewer", "wait_for_human": True},
        {
            "success": True,
            "result": {"artifacts_produced": {"questions": str(question_path)}},
        },
    )

    state = runner.state.get_full_status()
    assert len(state["human_interventions"]) == 1
    assert state["human_interventions"][0]["step_id"] == "review"
    assert runner._pause_signal.is_file()
    legacy_rows = [
        json.loads(line)
        for line in (runs_root / "run-human-contract" / "evaluation_events.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert legacy_rows[-1]["event_type"] == "human_intervention"
    assert legacy_rows[-1]["data"]["question_count"] == 1


def test_structured_preflight_snapshot_reaches_dynamic_follow_up_steps(tmp_path) -> None:
    workflow_path = tmp_path / "sample.yaml"
    _write_candidate_fixture(workflow_path, version="1.0.0")
    runner = FlowRunner(
        str(workflow_path),
        run_id="run-preflight-snapshot",
        runs_root=tmp_path / "runs",
        repo_root=tmp_path,
    )
    runner.set_pre_flight_data(
        {
            "task_description": "prepare a review",
            "audience": "audit committee",
        }
    )

    params = runner._resolve_params(
        {
            "input": {
                "params": {
                    "all_answers": "{pre_flight}",
                    "audience_label": "Audience: {pre_flight.audience}",
                }
            }
        }
    )

    assert params["all_answers"] == {
        "task_description": "prepare a review",
        "audience": "audit committee",
    }
    assert params["audience_label"] == "Audience: audit committee"


def test_preflight_defaults_and_explicit_prefill_form_one_validated_context() -> None:
    workflow = {
        "workflow": {"name": "Pre-flight contract"},
        "pre_flight": {
            "defaults": {"review_depth": "full", "audience": "general"},
            "collect_from_human": [
                {"key": "topic", "question": "Topic?", "required": True, "type": "text"},
                {
                    "key": "audience",
                    "question": "Audience?",
                    "type": "choice",
                    "choices": ["general", "expert"],
                },
            ],
        },
    }

    answers = PreFlightCollector(
        workflow,
        prefill={"topic": "controls", "audience": "expert", "host_value": 7},
        silent=True,
    ).run()

    assert answers == {
        "review_depth": "full",
        "audience": "expert",
        "topic": "controls",
        "host_value": 7,
    }

    with pytest.raises(ValueError, match="Required pre-flight value 'topic'"):
        PreFlightCollector(workflow, silent=True).run()
    with pytest.raises(ValueError, match="must be one of"):
        PreFlightCollector(
            workflow,
            prefill={"topic": "controls", "audience": "invalid"},
            silent=True,
        ).run()


def test_prefill_file_must_exist_and_contain_a_json_object(tmp_path) -> None:
    with pytest.raises(FileNotFoundError, match="does not exist"):
        load_prefill_from_file(str(tmp_path / "missing.json"))

    invalid = tmp_path / "invalid.json"
    invalid.write_text("[]\n", encoding="utf-8")
    with pytest.raises(ValueError, match="JSON object"):
        load_prefill_from_file(str(invalid))

    valid = tmp_path / "valid.json"
    valid.write_text('{"topic": "controls"}\n', encoding="utf-8")
    assert load_prefill_from_file(str(valid)) == {"topic": "controls"}


def test_direct_runner_refuses_missing_required_preflight_before_execution(tmp_path) -> None:
    workflow_path = tmp_path / "required-preflight.yaml"
    workflow_path.write_text(
        """
workflow:
  id: required-preflight
  name: Required pre-flight
  version: 1.0.0
pre_flight:
  collect_from_human:
    - key: topic
      question: Topic?
      required: true
      type: text
agents:
  orchestrator:
    id: orchestrator
  workers:
    - id: worker
      role: Worker
      agent_md: worker.md
      backend: claude-cli
steps:
  - id: work
    name: Work
    agent: worker
    prompt: Work.
""".strip()
        + "\n",
        encoding="utf-8",
    )

    class UnexpectedHandler:
        def execute(self, *args, **kwargs):
            raise AssertionError(f"handler must not run: {args!r} {kwargs!r}")

    runner = FlowRunner(
        str(workflow_path),
        run_id="run-required-preflight",
        runs_root=tmp_path / "runs",
        repo_root=tmp_path,
        step_handler=UnexpectedHandler(),
    )

    result = runner.start()

    assert result["success"] is False
    assert result["error_type"] == "preflight_invalid"
    assert runner.state.get_full_status()["workflow_status"] == "failed"
    events = [
        json.loads(line)["event"]
        for line in runner.event_logger.events_path.read_text(encoding="utf-8").splitlines()
    ]
    assert "run.started" not in events


def test_deterministic_handler_rejects_output_path_traversal(tmp_path) -> None:
    handler = DeterministicStepHandler(runs_root=tmp_path / "runs")

    with pytest.raises(ValueError, match="inside its workspace"):
        handler.execute(
            "worker",
            {
                "run_id": "run-deterministic-path",
                "payload": {
                    "step_id": "work",
                    "output_spec": [
                        {"key": "result", "path": "../../outside.txt", "type": "text"}
                    ],
                },
            },
            "",
        )

    assert not (tmp_path / "outside.txt").exists()


def test_task_message_substitutes_prompt_output_and_exposes_worker_workspace(tmp_path) -> None:
    workflow_path = tmp_path / "sample.yaml"
    _write_candidate_fixture(workflow_path, version="1.0.0")
    runner = FlowRunner(
        str(workflow_path),
        run_id="run-task-message",
        runs_root=tmp_path / "runs",
        repo_root=tmp_path,
    )
    runner.set_pre_flight_data({"topic": "auditability", "output": "result.txt"})

    message = runner._build_task_message(
        {
            "id": "write",
            "agent": "writer",
            "prompt": "Write about {pre_flight.topic}.",
            "output": {
                "artifacts": [
                    {"key": "result", "path": "{pre_flight.output}", "type": "text"}
                ]
            },
        }
    )

    assert message["payload"]["prompt"] == "Write about auditability."
    assert message["payload"]["output_spec"][0]["path"] == "result.txt"
    assert Path(message["worker_workspace"]) == (
        tmp_path / "runs" / "run-task-message" / "workers" / "writer"
    )


def test_artifact_store_copies_directories_and_serializes_parallel_index_updates(tmp_path) -> None:
    store = ArtifactStore("run-artifacts", runs_root=tmp_path / "runs")
    sources = []
    for index in range(8):
        source = tmp_path / f"source-{index}.txt"
        source.write_text(f"value-{index}", encoding="utf-8")
        sources.append(source)

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(
            pool.map(
                lambda item: store.register(
                    f"artifact_{item[0]}", str(item[1]), step_id=f"step_{item[0]}"
                ),
                enumerate(sources),
            )
        )

    directory = tmp_path / "directory-source"
    directory.mkdir()
    (directory / "nested.txt").write_text("nested", encoding="utf-8")
    store.register("directory", str(directory), required=True)

    assert set(store.list_all()) == {"directory", *(f"artifact_{i}" for i in range(8))}
    copied_directory = store.get("directory")
    assert copied_directory is not None
    assert (copied_directory / "nested.txt").read_text(encoding="utf-8") == "nested"


def test_artifact_store_rejects_path_keys_and_lossy_multi_file_collisions(tmp_path) -> None:
    store = ArtifactStore("run-artifact-safety", runs_root=tmp_path / "runs")
    source_a = tmp_path / "a" / "same.txt"
    source_b = tmp_path / "b" / "same.txt"
    source_a.parent.mkdir()
    source_b.parent.mkdir()
    source_a.write_text("a", encoding="utf-8")
    source_b.write_text("b", encoding="utf-8")

    with pytest.raises(ValueError, match="artifact key"):
        store.register("../escape", str(source_a))
    with pytest.raises(ValueError, match="重复文件名"):
        store.register("bundle", [str(source_a), str(source_b)], required=True)


def test_callable_result_without_explicit_status_fails_closed(tmp_path) -> None:
    handler = CallableStepHandler(run_id="run-callable-contract", runs_root=tmp_path / "runs")
    handler.register("worker", lambda message: {"artifacts_produced": {}})

    result = handler.execute(
        "worker",
        {"payload": {"step_id": "callable-step"}},
        "",
        backend="callable",
    )

    assert result["status"] == "failed"
    assert result["error_type"] == "invalid_worker_output"


def test_callable_handler_uses_the_canonical_event_logger_contract(tmp_path) -> None:
    events = RunEventLogger(
        run_id="run-callable-events",
        trace_id="trace-callable-events",
        workflow_id="callable-events",
        workflow_path="workflow.yaml",
        runs_root=tmp_path / "runs",
    )
    handler = CallableStepHandler(
        run_id="run-callable-events",
        runs_root=tmp_path / "runs",
        event_logger=events,
    )
    handler.register("worker", lambda message: {"status": "completed", "artifacts_produced": {}})

    result = handler.execute(
        "worker",
        {"task_id": "task-callable-events", "payload": {"step_id": "callable_step"}},
        "",
        backend="callable",
    )

    assert result["status"] == "completed"
    records = [
        json.loads(line)
        for line in (tmp_path / "runs/run-callable-events/events.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert [record["event"] for record in records] == [
        "handler.invoke.started",
        "handler.invoke.completed",
    ]
    assert {record["step_id"] for record in records} == {"callable_step"}


def test_nagare_debug_recovery_has_no_fixed_attempt_or_timeout_ceiling(tmp_path) -> None:
    workflow_path = tmp_path / "unbounded-recovery.yaml"
    workflow_path.write_text(
        """
workflow:
  id: unbounded-recovery
  name: Unbounded recovery
  version: 1.0.0
pre_flight:
  collect_from_human: []
agents:
  orchestrator:
    id: orchestrator
  workers:
    - id: worker
      role: Primary worker
      agent_md: worker.md
      backend: claude-cli
      model: test
    - id: debug
      role: Debug worker
      agent_md: debug.md
      backend: claude-cli
      model: test
steps:
  - id: work
    name: Work
    agent: worker
    depends: []
    prompt: Finish the work.
    output:
      artifacts: []
error_handling:
  debug_agent: debug
""".strip(),
        encoding="utf-8",
    )

    class RecoveringHandler:
        def __init__(self) -> None:
            self.worker_calls = 0
            self.debug_calls = 0
            self.debug_messages: list[dict] = []
            self.task_ids: list[str] = []

        # Deliberately has no timeout argument: FlowRunner must not inject one.
        def execute(self, agent_id, task_message, agent_md_path, backend="claude-cli", model=""):
            del agent_md_path, backend, model
            self.task_ids.append(task_message["task_id"])
            if agent_id == "debug":
                self.debug_messages.append(task_message)
                self.debug_calls += 1
                return {
                    "status": "recovered",
                    "diagnosis": "The prior output did not satisfy the task contract.",
                    "evidence": [task_message["payload"]["params"]["failed_step"]["error"]],
                    "fix_applied": f"Applied materially revised recovery {self.debug_calls}.",
                    "changes_made": [f"revision-{self.debug_calls}"],
                }
            self.worker_calls += 1
            if self.worker_calls <= 4:
                return {
                    "status": "failed",
                    "error_type": "task_error",
                    "error_message": "correctable failure",
                }
            return {"status": "completed", "artifacts_produced": {}}

    handler = RecoveringHandler()
    runner = FlowRunner(
        str(workflow_path),
        run_id="run-unbounded-recovery",
        runs_root=tmp_path / "runs",
        repo_root=tmp_path,
        step_handler=handler,
    )
    runner.workflow["inter_step_wait_seconds"] = 0

    result = runner.start()

    assert result["success"] is True
    assert handler.worker_calls == 5
    assert handler.debug_calls == 4
    assert len(set(handler.task_ids)) == len(handler.task_ids)
    snapshot = runner.state.get_runtime_snapshot()
    assert snapshot["step_status"]["work"]["attempt"] == 5
    event_records = [
        json.loads(line)
        for line in (tmp_path / "runs" / runner.run_id / "events.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    retry_events = [event for event in event_records if event["event"] == "step.retrying"]
    assert [event["data"]["attempt"] for event in retry_events] == [2, 3, 4, 5]
    assert [
        len(message["payload"]["params"]["previous_recovery_attempts"])
        for message in handler.debug_messages
    ] == [0, 1, 2, 3]
    assert all("correctable failure" in message["payload"]["prompt"] for message in handler.debug_messages)


def test_skipped_step_keeps_zero_attempts_and_emits_a_structured_event(tmp_path) -> None:
    workflow_path = tmp_path / "skip-event.yaml"
    workflow_path.write_text(
        """
workflow:
  id: skip-event
  name: Skip event
  version: 1.0.0
pre_flight:
  defaults:
    skip_now: "yes"
agents:
  orchestrator:
    id: orchestrator
  workers:
    - id: worker
      role: Worker that must not be invoked
      backend: callable
steps:
  - id: optional_work
    name: Optional work
    agent: worker
    prompt: Do not execute this step.
    skip_if: "pre_flight.skip_now == 'yes'"
""".strip()
        + "\n",
        encoding="utf-8",
    )

    class FailingIfCalled:
        def execute(self, *args, **kwargs):
            raise AssertionError("a skipped step must not invoke its handler")

    runner = FlowRunner(
        str(workflow_path),
        run_id="run-skip-event",
        runs_root=tmp_path / "runs",
        repo_root=tmp_path,
        step_handler=FailingIfCalled(),
    )
    runner.workflow["inter_step_wait_seconds"] = 0

    result = runner.start()

    assert result["success"] is True
    snapshot = runner.state.get_runtime_snapshot()
    assert snapshot["step_status"]["optional_work"] == {
        "status": "SKIPPED",
        "attempt": 0,
        "started_at": None,
        "ended_at": None,
        "artifacts": {},
        "error": None,
    }
    events = [
        json.loads(line)
        for line in (tmp_path / "runs" / runner.run_id / "events.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert [event["event"] for event in events].count("step.skipped") == 1
    assert "step.started" not in {event["event"] for event in events}


def test_debug_recovery_contract_rejects_an_exact_repeated_action() -> None:
    recovery = {
        "status": "recovered",
        "diagnosis": "Output format mismatch",
        "evidence": ["parser rejected the result"],
        "fix_applied": "Rewrote the result as valid JSON",
        "changes_made": ["updated result.json"],
    }
    history = [
        {
            "diagnosis": recovery["diagnosis"],
            "evidence": recovery["evidence"],
            "fix_applied": recovery["fix_applied"],
            "changes_made": recovery["changes_made"],
        }
    ]

    assert FlowRunner._validate_recovery_result(recovery, []) is None
    assert "repeated" in FlowRunner._validate_recovery_result(recovery, history)


def test_completed_step_fails_closed_when_declared_artifact_is_missing(tmp_path) -> None:
    workflow_path = tmp_path / "missing-artifact.yaml"
    workflow_path.write_text(
        """
workflow:
  id: missing-artifact
  name: Missing artifact
  version: 1.0.0
agents:
  orchestrator:
    id: orchestrator
  workers:
    - id: worker
      role: Primary worker
      agent_md: worker.md
      backend: claude-cli
      model: test
steps:
  - id: work
    name: Work
    agent: worker
    prompt: Create output.
    output:
      artifacts:
        - key: result
          path: result.txt
          type: text
""".strip()
        + "\n",
        encoding="utf-8",
    )

    class MissingArtifactHandler:
        def execute(self, agent_id, task_message, agent_md_path, backend="claude-cli", model=""):
            del agent_id, task_message, agent_md_path, backend, model
            return {"status": "completed", "artifacts_produced": {}}

    runner = FlowRunner(
        str(workflow_path),
        run_id="run-missing-artifact",
        runs_root=tmp_path / "runs",
        repo_root=tmp_path,
        step_handler=MissingArtifactHandler(),
    )
    runner.workflow["inter_step_wait_seconds"] = 0

    result = runner.start()

    assert result["success"] is False
    assert "omitted declared artifact" in result["error"]
    assert runner.state.get_full_status()["steps"]["work"]["status"] == "failed"


def test_declared_artifact_types_are_verified_from_real_files(tmp_path) -> None:
    invalid_json = tmp_path / "invalid.json"
    invalid_json.write_text("not json\n", encoding="utf-8")
    fake_directory = tmp_path / "bundle"
    fake_directory.write_text("not a directory\n", encoding="utf-8")

    json_error = FlowRunner._validate_declared_artifacts(
        {
            "output": {
                "artifacts": [
                    {"key": "report", "path": "report.json", "type": "json"}
                ]
            }
        },
        {"artifacts_produced": {"report": str(invalid_json)}},
    )
    directory_error = FlowRunner._validate_declared_artifacts(
        {
            "output": {
                "artifacts": [
                    {"key": "bundle", "path": "bundle", "type": "directory"}
                ]
            }
        },
        {"artifacts_produced": {"bundle": str(fake_directory)}},
    )

    assert json_error is not None and "invalid JSON" in json_error
    assert directory_error is not None and "expected directory" in directory_error


def test_automatic_quality_gate_blocks_false_json_result(tmp_path) -> None:
    workflow_path = tmp_path / "quality-gate.yaml"
    workflow_path.write_text(
        """
workflow:
  id: quality-gate
  name: Quality gate
  version: 1.0.0
agents:
  orchestrator:
    id: orchestrator
  workers:
    - id: validator
      role: Validation worker
      agent_md: validator.md
      backend: claude-cli
      model: test
steps:
  - id: validate
    name: Validate
    agent: validator
    prompt: Validate output.
    output:
      artifacts:
        - key: validation_report
          path: validation_report.json
          type: json
    quality_gate:
      type: auto
      criteria:
        - validation_report.valid == true
""".strip()
        + "\n",
        encoding="utf-8",
    )
    report_path = tmp_path / "validation_report.json"
    report_path.write_text('{"valid": false}\n', encoding="utf-8")

    class InvalidQualityHandler:
        def execute(self, agent_id, task_message, agent_md_path, backend="claude-cli", model=""):
            del agent_id, task_message, agent_md_path, backend, model
            return {
                "status": "completed",
                "artifacts_produced": {"validation_report": str(report_path)},
            }

    runner = FlowRunner(
        str(workflow_path),
        run_id="run-quality-gate",
        runs_root=tmp_path / "runs",
        repo_root=tmp_path,
        step_handler=InvalidQualityHandler(),
    )
    runner.workflow["inter_step_wait_seconds"] = 0

    result = runner.start()

    assert result["success"] is False
    assert "Quality gate failed" in result["error"]
    assert "validation_report.valid == true" in result["error"]


def test_parallel_task_failure_uses_the_same_unbounded_debug_recovery(tmp_path) -> None:
    workflow_path = tmp_path / "parallel-recovery.yaml"
    workflow_path.write_text(
        """
workflow:
  id: parallel-recovery
  name: Parallel recovery
  version: 1.0.0
agents:
  orchestrator:
    id: orchestrator
  workers:
    - id: worker_a
      role: Parallel worker A
      agent_md: worker-a.md
      backend: claude-cli
      model: test
    - id: worker_b
      role: Parallel worker B
      agent_md: worker-b.md
      backend: claude-cli
      model: test
    - id: debug
      role: Debug worker
      agent_md: debug.md
      backend: claude-cli
      model: test
steps:
  - id: work_a
    name: Work A
    agent: worker_a
    strategy: parallel
    prompt: Work A.
  - id: work_b
    name: Work B
    agent: worker_b
    strategy: parallel
    prompt: Work B.
error_handling:
  debug_agent: debug
""".strip()
        + "\n",
        encoding="utf-8",
    )

    class ParallelRecoveryHandler:
        def __init__(self) -> None:
            self.worker_a_calls = 0
            self.debug_calls = 0

        def execute(self, agent_id, task_message, agent_md_path, backend="claude-cli", model=""):
            del task_message, agent_md_path, backend, model
            if agent_id == "debug":
                self.debug_calls += 1
                return {
                    "status": "recovered",
                    "diagnosis": "Parallel worker output needs a revised action.",
                    "evidence": ["parallel worker returned task_error"],
                    "fix_applied": f"Applied parallel recovery {self.debug_calls}.",
                    "changes_made": [f"parallel-revision-{self.debug_calls}"],
                }
            if agent_id == "worker_a":
                self.worker_a_calls += 1
                if self.worker_a_calls <= 4:
                    return {
                        "status": "failed",
                        "error_type": "task_error",
                        "error_message": "correctable failure",
                    }
            return {"status": "completed", "artifacts_produced": {}}

    handler = ParallelRecoveryHandler()
    runner = FlowRunner(
        str(workflow_path),
        run_id="run-parallel-recovery",
        runs_root=tmp_path / "runs",
        repo_root=tmp_path,
        step_handler=handler,
    )
    runner.workflow["inter_step_wait_seconds"] = 0

    result = runner.start()

    assert result["success"] is True
    assert set(result["completed_steps"]) == {"work_a", "work_b"}
    assert handler.worker_a_calls == 5
    assert handler.debug_calls == 4


def test_disabled_evaluation_does_not_invoke_the_host_evaluator(tmp_path) -> None:
    workflow_path = tmp_path / "no-evaluation.yaml"
    _write_candidate_fixture(workflow_path, version="1.0.0")
    evaluator = RecordingEvaluator()
    runner = FlowRunner(
        str(workflow_path),
        run_id="run-no-evaluation",
        runs_root=tmp_path / "runs",
        repo_root=tmp_path,
        evaluator=evaluator,
        step_handler=NoArtifactHandler(),
    )
    runner.workflow["evaluation"] = {"enabled": False}

    result = runner.start()

    assert result["success"] is True
    assert evaluator.run_ids == []


def test_callable_setup_wait_is_released_by_explicit_stop(tmp_path) -> None:
    runs_root = tmp_path / "runs"
    run_id = "run-callable-stop"
    manager = CallableSetupManager(run_id=run_id, runs_root=runs_root)
    event = manager.request_setup(
        "callable-worker",
        {"payload": {"step_id": "callable-step"}},
        attempt=1,
    )
    stop_signal = runs_root / run_id / "_stop"
    stop_signal.parent.mkdir(parents=True, exist_ok=True)
    stop_signal.write_text("operator stop\n", encoding="utf-8")

    assert manager.wait_for_setup("callable-worker", event) is False


def test_callable_delivery_rejects_agent_path_traversal(tmp_path) -> None:
    callables_root = tmp_path / "callables"
    manager = CallableSetupManager(
        run_id="run-callable-path",
        runs_root=tmp_path / "runs",
        callables_root=callables_root,
    )

    result = manager.deliver_code("../escape", "def run(task_message):\n    return {'status': 'completed'}\n")

    assert result["ok"] is False
    assert "agent_id" in result["error"]
    assert not (tmp_path / "escape.py").exists()


def test_stop_result_remains_aborted_and_is_not_relabelled_failed(tmp_path) -> None:
    workflow_path = tmp_path / "stop.yaml"
    workflow_path.write_text(
        """
workflow:
  id: stop-contract
  name: Stop contract
  version: 1.0.0
agents:
  orchestrator:
    id: orchestrator
  workers:
    - id: worker
      role: Primary worker
      agent_md: worker.md
      backend: claude-cli
steps:
  - id: work
    name: Work
    agent: worker
    prompt: Stop safely.
""".strip()
        + "\n",
        encoding="utf-8",
    )
    runs_root = tmp_path / "runs"
    run_id = "run-stop-state"

    class StopHandler:
        def execute(self, agent_id, task_message, agent_md_path, backend="claude-cli", model=""):
            del agent_id, task_message, agent_md_path, backend, model
            stop = runs_root / run_id / "_stop"
            stop.write_text("authorized test stop\n", encoding="utf-8")
            return {
                "status": "failed",
                "error_type": "cancelled",
                "error_message": "stopped",
            }

    runner = FlowRunner(
        str(workflow_path),
        run_id=run_id,
        runs_root=runs_root,
        repo_root=tmp_path,
        step_handler=StopHandler(),
    )
    runner.workflow["inter_step_wait_seconds"] = 0

    result = runner.start()

    assert result["error_type"] == "cancelled"
    assert runner.state.get_full_status()["workflow_status"] == "aborted"
    events = [
        json.loads(line)["event"]
        for line in (runs_root / run_id / "events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert "run.cancelled" in events
    assert "run.failed" not in events
