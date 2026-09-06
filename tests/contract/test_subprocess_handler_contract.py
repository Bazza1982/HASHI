from __future__ import annotations

import json
import subprocess
from pathlib import Path

from nagare.handlers.subprocess_handler import SubprocessStepHandler
from nagare.logging.events import RunEventLogger


class _CompletedProcess:
    returncode = 0

    def __init__(self, stdout: str) -> None:
        self.stdout = stdout

    def communicate(self, timeout=None):
        del timeout
        return self.stdout, ""


def _handler(tmp_path: Path, run_id: str = "run-handler") -> SubprocessStepHandler:
    return SubprocessStepHandler(
        run_id=run_id,
        runs_root=tmp_path / "runs",
        repo_root=tmp_path,
    )


def test_unknown_subprocess_backend_fails_closed_without_starting_process(
    tmp_path,
    monkeypatch,
) -> None:
    def unexpected_popen(*args, **kwargs):
        raise AssertionError(f"Popen must not run: {args!r} {kwargs!r}")

    monkeypatch.setattr(subprocess, "Popen", unexpected_popen)
    result = _handler(tmp_path)._run_cli(
        agent_id="worker",
        task_id="task-1",
        system_prompt="system",
        user_prompt="task",
        worker_dir=tmp_path,
        backend="openrouter-api",
        model="example/model",
    )

    assert result["status"] == "failed"
    assert result["error_type"] == "unsupported_backend"


def test_unstructured_worker_output_is_not_reported_as_completed(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda *args, **kwargs: _CompletedProcess("I finished, but omitted the JSON contract."),
    )
    result = _handler(tmp_path)._run_cli(
        agent_id="worker",
        task_id="task-2",
        system_prompt="system",
        user_prompt="task",
        worker_dir=tmp_path,
        backend="claude-cli",
        model="test-model",
    )

    assert result["status"] == "failed"
    assert result["error_type"] == "invalid_worker_output"


def test_non_mapping_artifact_output_is_rejected_before_path_resolution(
    tmp_path,
    monkeypatch,
) -> None:
    stdout = '```json\n{"status":"completed","artifacts_produced":["result.txt"]}\n```'
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: _CompletedProcess(stdout))

    result = _handler(tmp_path)._run_cli(
        agent_id="worker",
        task_id="task-artifacts",
        system_prompt="system",
        user_prompt="task",
        worker_dir=tmp_path,
        backend="claude-cli",
        model="test-model",
    )

    assert result["status"] == "failed"
    assert result["error_type"] == "invalid_worker_output"
    assert "mapping" in result["error_message"]


def test_codex_without_model_uses_cli_config_instead_of_retired_hardcoded_default(
    tmp_path,
    monkeypatch,
) -> None:
    captured: list[str] = []
    stdout = '```json\n{"status":"completed","artifacts_produced":{}}\n```'

    def fake_popen(command, **kwargs):
        del kwargs
        captured.extend(command)
        return _CompletedProcess(stdout)

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    result = _handler(tmp_path)._run_cli(
        agent_id="worker",
        task_id="task-codex-default",
        system_prompt="system",
        user_prompt="task",
        worker_dir=tmp_path,
        backend="codex-cli",
        model="",
    )

    assert result["status"] == "completed"
    assert captured[:2] == ["codex", "exec"]
    assert "--model" not in captured
    assert "o4-mini" not in captured


def test_subprocess_artifact_paths_cannot_escape_worker_workspace(tmp_path, monkeypatch) -> None:
    stdout = (
        '```json\n{"status":"completed","artifacts_produced":'
        '{"result":"../outside.txt"}}\n```'
    )
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: _CompletedProcess(stdout))

    result = _handler(tmp_path)._run_cli(
        agent_id="worker",
        task_id="task-artifact-escape",
        system_prompt="system",
        user_prompt="task",
        worker_dir=tmp_path / "worker",
        backend="claude-cli",
        model="test-model",
    )

    assert result["status"] == "failed"
    assert result["error_type"] == "invalid_worker_output"
    assert "inside its workspace" in result["error_message"]


def test_explicit_stop_terminates_a_waiting_subprocess(tmp_path, monkeypatch) -> None:
    run_id = "run-stop-handler"
    stop_signal = tmp_path / "runs" / run_id / "_stop"
    stop_signal.parent.mkdir(parents=True)
    stop_signal.write_text("operator stop\n", encoding="utf-8")

    class WaitingProcess:
        returncode = -15

        def __init__(self) -> None:
            self.terminated = False

        def communicate(self, timeout=None):
            if not self.terminated:
                raise subprocess.TimeoutExpired(cmd="claude", timeout=timeout)
            return "partial output", ""

        def terminate(self):
            self.terminated = True

        def kill(self):
            raise AssertionError("graceful terminate should be sufficient")

    process = WaitingProcess()
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: process)

    result = _handler(tmp_path, run_id=run_id)._run_cli(
        agent_id="worker",
        task_id="task-3",
        system_prompt="system",
        user_prompt="task",
        worker_dir=tmp_path,
        backend="claude-cli",
        model="test-model",
    )

    assert process.terminated is True
    assert result["status"] == "failed"
    assert result["error_type"] == "cancelled"


def test_subprocess_events_separate_step_and_request_correlation(tmp_path, monkeypatch) -> None:
    run_id = "run-subprocess-events"
    event_logger = RunEventLogger(
        run_id=run_id,
        trace_id="trace-subprocess-events",
        workflow_id="subprocess-events",
        workflow_path="workflow.yaml",
        runs_root=tmp_path / "runs",
    )
    handler = SubprocessStepHandler(
        run_id=run_id,
        runs_root=tmp_path / "runs",
        repo_root=tmp_path,
        event_logger=event_logger,
    )
    agent_md = tmp_path / "AGENT.md"
    agent_md.write_text("Complete the assigned task.\n", encoding="utf-8")
    stdout = '```json\n{"status":"completed","artifacts_produced":{}}\n```'
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: _CompletedProcess(stdout))

    result = handler.execute(
        "worker",
        {
            "task_id": "task-subprocess-events",
            "run_id": run_id,
            "workflow_id": "subprocess-events",
            "payload": {"step_id": "workflow_step", "prompt": "Complete."},
        },
        str(agent_md),
        backend="claude-cli",
    )

    assert result["status"] == "completed"
    records = [
        json.loads(line)
        for line in (tmp_path / "runs" / run_id / "events.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert [record["event"] for record in records] == [
        "handler.invoke.started",
        "handler.invoke.completed",
    ]
    assert {record["step_id"] for record in records} == {"workflow_step"}
    assert {record["request_id"] for record in records} == {"task-subprocess-events"}
