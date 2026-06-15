from __future__ import annotations

import json

import pytest

from tools.registry import ToolRegistry
from tools.tool_audit import build_tool_audit_record


def test_build_tool_audit_record_redacts_and_truncates_sensitive_values():
    record = build_tool_audit_record(
        tool_name="bash",
        tool_call_id="call-1",
        arguments={
            "command": "echo token=abc123",
            "content": "x" * 900,
        },
        output="ok",
        is_error=False,
        duration_ms=3,
        audit_context={"agent_name": "zelda", "workspace_dir": "/tmp/work", "safety_mode": "read_write"},
        ts=1.0,
    )

    assert record["agent"] == "zelda"
    assert record["args_redacted"]["command"] == "echo token=[redacted]"
    assert record["args_redacted"]["content"].endswith("...[truncated]")
    assert record["status"] == "success"


@pytest.mark.asyncio
async def test_tool_registry_writes_success_audit_record(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = ToolRegistry(
        allowed_tools=["file_write"],
        access_root=workspace,
        workspace_dir=workspace,
        secrets={},
        audit_context={"agent_name": "nana", "workspace_dir": str(workspace), "safety_mode": "read_write"},
    )

    result = await registry.execute(
        "file_write",
        {"path": "report.txt", "content": "hello"},
        tool_call_id="call-write",
    )

    assert result.is_error is False
    rows = (workspace / "tool_action_audit.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(rows) == 1
    record = json.loads(rows[0])
    assert record["kind"] == "tool_action"
    assert record["tool_name"] == "file_write"
    assert record["tool_call_id"] == "call-write"
    assert record["agent"] == "nana"
    assert record["status"] == "success"
    assert record["args_redacted"] == {"path": "report.txt", "content": "hello"}
    assert "OK: wrote" in record["output_snippet"]


@pytest.mark.asyncio
async def test_tool_registry_writes_denied_audit_record(tmp_path):
    registry = ToolRegistry(
        allowed_tools=["file_read"],
        access_root=tmp_path,
        workspace_dir=tmp_path,
        secrets={},
        audit_context={"agent_name": "nana", "workspace_dir": str(tmp_path), "safety_mode": "read_only"},
    )

    result = await registry.execute("bash", {"command": "echo should-not-run"}, tool_call_id="call-deny")

    assert result.is_error is True
    rows = (tmp_path / "tool_action_audit.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(rows) == 1
    record = json.loads(rows[0])
    assert record["tool_name"] == "bash"
    assert record["tool_call_id"] == "call-deny"
    assert record["status"] == "failed"
    assert "not in your allowed tools" in record["output_snippet"]
