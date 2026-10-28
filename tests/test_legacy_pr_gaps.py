from __future__ import annotations

import json
import os
from types import SimpleNamespace

import pytest

from orchestrator import request_diagnostics, runtime_debug_reporting, workzone
from orchestrator.agent_overview import _workzone_overview
from tools.tool_audit import record_tool_action


@pytest.mark.skipif(os.name != "nt", reason="requires native Windows path semantics")
def test_foreign_posix_workzone_is_neither_remapped_nor_a_tool_root(tmp_path):
    # /Users exists as a rooted path on ordinary Windows installations.
    with pytest.raises(ValueError, match="POSIX absolute"):
        workzone.resolve_workzone_input("/Users", tmp_path, tmp_path / "agent")

    foreign = "/Users"
    state = {"slots": [{"slot_id": "main", "path": foreign, "enabled": True}]}
    normalized = workzone.normalize_workzone_state(state)
    [slot] = normalized["slots"]
    assert slot["path"] == foreign
    assert slot["path_kind"] == "posix"
    assert slot["execution_platform"] == "windows"
    assert slot["host_compatible"] is False
    assert slot["available"] is False
    assert workzone.primary_workzone_path(normalized) is None
    workspace = tmp_path / "agent"
    workspace.mkdir()
    roots = workzone.access_roots_for_workzones(tmp_path, normalized, workspace_dir=workspace)
    assert roots == (workspace.resolve(),)
    overview = _workzone_overview(None, workspace, desired_workzone_state=state)
    assert overview["slots"][0]["path"] == foreign
    assert overview["slots"][0]["host_compatible"] is False

    (workspace / "workzone.json").write_text(json.dumps({"path": foreign}), encoding="utf-8")
    assert workzone.load_workzone(workspace) is None


def _persist_terminal(tmp_path, request_id, **kwargs):
    runtime = SimpleNamespace(
        name="test",
        workspace_dir=tmp_path,
        global_config=SimpleNamespace(instance_id="test"),
        logger=SimpleNamespace(warning=lambda *args: None),
    )
    runtime_debug_reporting.persist_terminal_diagnostic(runtime, request_id, kwargs)


def test_diagnostics_reconciles_job_that_finishes_after_request(tmp_path):
    request_id = "later-job"
    _persist_terminal(tmp_path, request_id, success=False, interrupted=True,
                      side_effects_possible=True, tool_call_count=1)
    record_tool_action(
        workspace_dir=tmp_path, tool_name="background_job_start",
        tool_call_id="job-call", arguments={}, output="started", is_error=False,
        duration_ms=1, audit_context={"request_id": request_id},
    )
    job = {"job_id": "job-1", "state": "running", "origin": {"request_id": request_id}}
    running = request_diagnostics.build_request_diagnostics(
        workspace_dir=tmp_path, request_id=request_id, background_jobs=[job]
    )
    assert running["reconciliation"]["request_completion"] == "not_completed"
    assert running["reconciliation"]["effect_status"] == "unknown"
    finished = request_diagnostics.build_request_diagnostics(
        workspace_dir=tmp_path, request_id=request_id,
        background_jobs=[{**job, "state": "succeeded", "returncode": 0}],
    )
    assert finished["reconciliation"]["effect_status"] == "observed"
    assert any(e["kind"] == "background_job_completed"
               for e in finished["reconciliation"]["known_effects"])
    assert finished["safe_retry_evidence"]["status"] != "present"


def test_diagnostics_distinguishes_verified_write_and_missing_evidence(tmp_path):
    _persist_terminal(tmp_path, "write", success=False, error_retryable=True,
                      side_effects_possible=True, tool_call_count=1)
    record_tool_action(
        workspace_dir=tmp_path, tool_name="file_write", tool_call_id="write-1",
        arguments={"path": "result.txt"}, output="saved", is_error=False,
        duration_ms=1, audit_context={"request_id": "write"},
        details={"effect_receipt": {"kind": "write", "readback": True}},
    )
    written = request_diagnostics.build_request_diagnostics(
        workspace_dir=tmp_path, request_id="write"
    )
    assert written["reconciliation"]["effect_status"] == "observed"
    assert written["reconciliation"]["confirmed_write_count"] == 1
    assert written["safe_retry_evidence"]["status"] != "present"

    _persist_terminal(tmp_path, "missing", success=False, error_retryable=True,
                      side_effects_possible=True, tool_call_count=1)
    missing = request_diagnostics.build_request_diagnostics(
        workspace_dir=tmp_path, request_id="missing"
    )
    assert missing["reconciliation"]["effect_status"] == "unknown"
    assert missing["reconciliation"]["evidence_limited"] is True
    assert missing["safe_retry_evidence"]["status"] != "present"


def test_diagnostics_preserves_confirmed_effects_recorded_at_terminal(tmp_path):
    _persist_terminal(
        tmp_path, "older-write", success=False, error_retryable=True,
        side_effects_possible=False, tool_call_count=0,
        effect_reconciliation={"confirmed_write_count": 1},
    )
    report = request_diagnostics.build_request_diagnostics(
        workspace_dir=tmp_path, request_id="older-write"
    )
    assert report["reconciliation"]["effect_status"] == "observed"
    assert report["reconciliation"]["confirmed_write_count"] >= 1
    assert report["safe_retry_evidence"]["status"] == "absent"


def test_workzone_path_kinds_keep_wsl_and_network_paths_distinct():
    assert workzone.workzone_path_kind(r"C:\Projects") == "windows_drive"
    assert workzone.workzone_path_kind(r"\\wsl.localhost\Ubuntu-22.04\home") == "wsl_unc"
    assert workzone.workzone_path_kind("//wsl$/Ubuntu-22.04/home") == "wsl_unc"
    assert workzone.workzone_path_kind(r"\\server\share") == "windows_unc"
    assert workzone.workzone_path_kind("/Users") == "posix"
    assert workzone.workzone_path_kind("relative/folder") == "relative"


def test_diagnostics_recognizes_strict_read_receipts_without_promising_retry(tmp_path):
    request_id = "verified-read"
    _persist_terminal(tmp_path, request_id, success=False, error_retryable=True,
                      side_effects_possible=True, tool_call_count=1)
    digest = "a" * 64
    record_tool_action(
        workspace_dir=tmp_path, tool_name="file_read", tool_call_id="read-1",
        arguments={}, output="content", is_error=False, duration_ms=1,
        audit_context={"request_id": request_id},
        details={
            "receipt_completed": True, "receipt_status": "success",
            "effect_receipt": {
                "kind": "read", "tool_name": "file_read",
                "evidence_ref": f"tool:read-1:sha256:{digest}",
                "revision": f"sha256:{digest}",
            },
        },
    )
    result = request_diagnostics.build_request_diagnostics(
        workspace_dir=tmp_path, request_id=request_id
    )
    assert result["reconciliation"]["confirmed_read_count"] == 1
    assert result["reconciliation"]["effect_status"] == "none_observed"
    assert result["safe_retry_evidence"]["status"] != "present"
