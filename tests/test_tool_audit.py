from __future__ import annotations

import json

import pytest

from orchestrator.enterprise import ArtifactRegistry, IdentityService, TaskRegistry
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
    assert record["org_id"] is None
    assert record["project_id"] is None
    assert record["task_id"] is None
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


@pytest.mark.asyncio
async def test_tool_registry_auto_registers_file_write_artifact_with_enterprise_context(tmp_path):
    db_path = tmp_path / "enterprise.sqlite"
    identity = IdentityService.from_path(db_path)
    identity.create_organization(org_id="ORG-001", name="Acme")
    identity.create_project(
        org_id="ORG-001",
        name="Research",
        workspace_root=str(tmp_path),
        project_id="prj-research",
    )
    task = TaskRegistry.from_path(db_path).create_task(
        org_id="ORG-001",
        project_id="prj-research",
        prompt_summary="Write an artifact",
        task_id="task-1",
    )
    registry = ToolRegistry(
        allowed_tools=["file_write"],
        access_root=tmp_path,
        workspace_dir=tmp_path,
        secrets={},
        audit_context={
            "agent_name": "nana",
            "workspace_dir": str(tmp_path),
            "safety_mode": "read_write",
            "enterprise_db_path": str(db_path),
            "org_id": "ORG-001",
            "project_id": "prj-research",
            "task_id": task.id,
        },
    )

    result = await registry.execute(
        "file_write",
        {"path": "deliverables/report.md", "content": "hello enterprise"},
        tool_call_id="call-artifact",
    )

    assert result.is_error is False
    artifacts = ArtifactRegistry.from_path(db_path).list_artifacts(org_id="ORG-001", task_id=task.id)
    assert len(artifacts) == 1
    artifact = artifacts[0]
    assert artifact.type == "file"
    assert artifact.path.endswith("deliverables/report.md")
    assert artifact.hash.startswith("sha256:")
    assert artifact.metadata["source"] == "tool_registry.file_write"
    assert artifact.metadata["tool_call_id"] == "call-artifact"

    record = json.loads((tmp_path / "tool_action_audit.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert record["org_id"] == "ORG-001"
    assert record["project_id"] == "prj-research"
    assert record["task_id"] == "task-1"
    assert record["artifact_id"] == artifact.id


@pytest.mark.asyncio
async def test_tool_registry_enterprise_path_gate_blocks_workspace_escape(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    db_path = tmp_path / "enterprise.sqlite"
    identity = IdentityService.from_path(db_path)
    identity.create_organization(org_id="ORG-001", name="Acme")
    identity.create_project(
        org_id="ORG-001",
        name="Research",
        workspace_root=str(workspace),
        project_id="prj-research",
    )
    task = TaskRegistry.from_path(db_path).create_task(
        org_id="ORG-001",
        project_id="prj-research",
        prompt_summary="Attempt unsafe write",
        task_id="task-unsafe",
    )
    registry = ToolRegistry(
        allowed_tools=["file_write"],
        access_root=workspace,
        workspace_dir=workspace,
        secrets={},
        audit_context={
            "agent_name": "nana",
            "workspace_dir": str(workspace),
            "enterprise_db_path": str(db_path),
            "org_id": "ORG-001",
            "project_id": "prj-research",
            "task_id": task.id,
            "enterprise_workspace_root": str(workspace),
        },
    )

    result = await registry.execute(
        "file_write",
        {"path": "../outside.txt", "content": "leak"},
        tool_call_id="call-escape",
    )

    assert result.is_error is True
    assert "enterprise execution denied: workspace_escape" in result.output
    assert not (tmp_path / "outside.txt").exists()
    assert ArtifactRegistry.from_path(db_path).list_artifacts(org_id="ORG-001", task_id=task.id) == []

    record = json.loads((workspace / "tool_action_audit.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert record["status"] == "failed"
    assert record["tool_name"] == "file_write"
    assert "workspace_escape" in record["output_snippet"]
