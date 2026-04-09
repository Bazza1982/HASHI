from __future__ import annotations

import json
import threading
from pathlib import Path
from urllib.request import urlopen

from nagare.api.app import NagareApiServer
from nagare.api.runs import RunSnapshotService
from nagare.engine.artifacts import ArtifactStore
from nagare.engine.state import TaskState
from nagare.logging.events import RunEventLogger


def test_run_snapshot_service_returns_immutable_views_and_logs_requests(tmp_path: Path) -> None:
    run_id = "run-api-contract"
    runs_root = tmp_path / "runs"

    state = TaskState(run_id, runs_root=runs_root)
    state.set_workflow_metadata(
        workflow_id="smoke-test",
        workflow_version="1.0.0",
        workflow_path="/tmp/workflow.yaml",
    )
    state.set_workflow_status("running")
    state.set_step_status("step_write", "running", attempt=2)
    state.set_step_status("step_done", "completed", artifacts={"draft": "draft.txt"})

    artifact_source = tmp_path / "draft.txt"
    artifact_source.write_text("draft", encoding="utf-8")
    ArtifactStore(run_id, runs_root=runs_root).register("draft", str(artifact_source), step_id="step_done")

    event_logger = RunEventLogger(
        run_id=run_id,
        trace_id="trace-contract",
        workflow_id="smoke-test",
        workflow_path="/tmp/workflow.yaml",
        runs_root=runs_root,
    )
    event_logger.emit("run.started", message="Run started", data={"source": "contract"})

    service = RunSnapshotService(runs_root=runs_root)

    snapshot_response = service.get_run_snapshot(run_id, request_id="req-snapshot")
    events_response = service.get_run_events(run_id, request_id="req-events")
    artifacts_response = service.get_run_artifacts(run_id, request_id="req-artifacts")

    assert snapshot_response["snapshot_version"] == 1
    assert snapshot_response["run"]["run_id"] == run_id
    assert snapshot_response["run"]["status"] == "RUNNING"
    assert snapshot_response["run"]["step_status"]["step_write"]["status"] == "RUNNING"

    assert events_response["run_id"] == run_id
    assert events_response["count"] >= 1
    assert events_response["events"][0]["event"] == "run.started"

    assert artifacts_response["run_id"] == run_id
    assert artifacts_response["count"] == 1
    assert artifacts_response["artifacts"][0]["key"] == "draft"

    recorded_events = [
        json.loads(line)
        for line in (runs_root / run_id / "events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    api_events = [event for event in recorded_events if event["event"] == "api.request.completed"]
    assert len(api_events) == 3
    assert {event["request_id"] for event in api_events} == {
        "req-snapshot",
        "req-events",
        "req-artifacts",
    }


def test_api_server_exposes_read_only_run_endpoints(tmp_path: Path) -> None:
    run_id = "run-api-http"
    runs_root = tmp_path / "runs"

    state = TaskState(run_id, runs_root=runs_root)
    state.set_workflow_metadata(workflow_id="smoke-test", workflow_version="1.0.0", workflow_path="/tmp/wf.yaml")
    state.set_workflow_status("completed")
    state.set_step_status("step_done", "completed")

    server = NagareApiServer(("127.0.0.1", 0), runs_root=runs_root)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base_url = f"http://127.0.0.1:{server.server_port}"
        with urlopen(f"{base_url}/runs/{run_id}") as response:
            payload = json.loads(response.read().decode("utf-8"))
        assert payload["run"]["status"] == "COMPLETED"

        with urlopen(f"{base_url}/runs/{run_id}/events?limit=5") as response:
            event_payload = json.loads(response.read().decode("utf-8"))
        assert event_payload["run_id"] == run_id

        with urlopen(f"{base_url}/runs/{run_id}/artifacts") as response:
            artifact_payload = json.loads(response.read().decode("utf-8"))
        assert artifact_payload["run_id"] == run_id
        assert artifact_payload["count"] == 0
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()
