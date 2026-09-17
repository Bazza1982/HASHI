from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from adapters.base import BackendResponse
from orchestrator import request_diagnostics, runtime_debug_reporting, runtime_pipeline
from orchestrator.background_jobs import BackgroundJobManager
from tools.tool_audit import record_tool_action


class _Logger:
    def __init__(self) -> None:
        self.warnings: list[str] = []

    def warning(self, message: object, *args: object) -> None:
        rendered = str(message)
        if args:
            rendered = rendered % args
        self.warnings.append(rendered)


def _runtime(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        name="zelda",
        workspace_dir=tmp_path,
        logger=_Logger(),
        global_config=SimpleNamespace(instance_id="HASHI-test"),
    )


def test_backend_diagnostic_fields_keep_provider_request_response_and_wire_refs():
    response = BackendResponse(
        text="done",
        duration_ms=12,
        stream_metadata={
            "meter": {
                "provider_calls": [
                    {
                        "provider_request_id": "provider-request-1",
                        "provider_response_id": "provider-response-1",
                        "provider_wire_evidence_refs": ["hashi-log:wire:1"],
                    }
                ]
            },
            "her_v2": {"evidence_refs": ["hashi-log:her:1"]},
        },
        tool_call_count=0,
        side_effects_possible=False,
    )

    fields = runtime_pipeline.backend_diagnostic_fields(response)

    assert fields["provider_request_id"] == "provider-request-1"
    assert fields["provider_response_id"] == "provider-response-1"
    assert fields["provider_request_ids"] == ["provider-request-1"]
    assert fields["provider_response_ids"] == ["provider-response-1"]
    assert fields["wire_evidence_refs"] == ["hashi-log:wire:1"]
    assert fields["evidence_refs"] == ["hashi-log:her:1"]
    assert fields["side_effects_possible"] is False


def test_terminal_projection_records_final_state_and_only_evidence_based_retry(tmp_path: Path):
    runtime = _runtime(tmp_path)

    path = runtime_debug_reporting.persist_terminal_diagnostic(
        runtime,
        "req-1",
        {
            "success": False,
            "error": "provider unavailable",
            "error_code": "PROVIDER_CAPACITY_UNAVAILABLE",
            "error_retryable": True,
            "side_effects_possible": False,
            "tool_call_count": 0,
            "provider_request_id": "provider-request-1",
            "provider_response_id": "provider-response-1",
            "wire_evidence_refs": ["hashi-log:wire:1"],
        },
    )

    assert path is not None
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["terminal"]["state"] == "failed"
    assert payload["terminal"]["completed"] is False
    assert payload["provider"]["request_id"] == "provider-request-1"
    assert payload["provider"]["response_id"] == "provider-response-1"
    assert payload["safe_retry_evidence"]["status"] == "present"

    blocked = tmp_path / "not-a-directory"
    blocked.write_text("occupied", encoding="utf-8")
    runtime.workspace_dir = blocked
    assert (
        runtime_debug_reporting.persist_terminal_diagnostic(
            runtime,
            "req-2",
            {"success": True},
        )
        is None
    )
    assert runtime.logger.warnings


@pytest.mark.asyncio
async def test_request_diagnostic_query_joins_terminal_tools_and_background_history(
    tmp_path: Path,
):
    runtime = _runtime(tmp_path)
    runtime_debug_reporting.persist_terminal_diagnostic(
        runtime,
        "req-join",
        {
            "success": False,
            "error": "stopped after tool activity",
            "error_retryable": True,
            "side_effects_possible": True,
            "tool_call_count": 2,
        },
    )
    record_tool_action(
        workspace_dir=tmp_path,
        tool_name="file_write",
        tool_call_id="call-write",
        arguments={"path": "notes/result.txt", "content": "result"},
        output="ok",
        is_error=False,
        duration_ms=3,
        audit_context={"request_id": "req-join", "agent_name": "zelda"},
    )

    manager = BackgroundJobManager(tmp_path / "background_jobs")
    await manager.start()
    job = await manager.start_job(
        agent="zelda",
        cwd=tmp_path,
        argv=[sys.executable, "-c", "print('ok')"],
        origin={"request_id": "req-join"},
        notify_on_complete=False,
        trigger_agent_on_complete=False,
    )
    await manager._monitor_tasks[job.job_id]

    report = request_diagnostics.build_request_diagnostics(
        workspace_dir=tmp_path,
        request_id="req-join",
        background_jobs=manager.list(limit=20),
        background_history={job.job_id: manager.history(job.job_id)},
    )

    assert report["terminal"]["state"] == "failed"
    assert report["safe_retry_evidence"]["status"] == "absent"
    assert report["tool_actions"][0]["request_id"] == "req-join"
    assert report["file_writes"][0]["target"] == "notes/result.txt"
    assert report["background_jobs"][0]["state"] == "succeeded"
    assert [event["state"] for event in report["background_jobs"][0]["history"]] == [
        "created",
        "starting",
        "running",
        "succeeded",
    ]


def test_debug_report_exposes_response_id_and_retry_evidence(tmp_path: Path):
    runtime = _runtime(tmp_path)
    runtime.config = SimpleNamespace(active_backend="hashi-api")
    runtime.session_dir = tmp_path / "logs"
    settings = runtime_debug_reporting.DebugReportingSettings(
        enabled=True,
        target="reviewer@HASHI-test",
        journal="journal.md",
    )

    message = runtime_debug_reporting.build_report_message(
        runtime,
        "req-report",
        {
            "success": False,
            "provider_request_id": "provider-request-2",
            "provider_response_id": "provider-response-2",
            "safe_retry_evidence": {"status": "unknown"},
        },
        settings,
    )

    assert "Provider request ID: provider-request-2" in message
    assert "Provider response ID: provider-response-2" in message
    assert "Safe retry evidence: unknown" in message
