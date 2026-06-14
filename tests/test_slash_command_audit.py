from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from orchestrator.admin_local_testing import execute_local_command
from orchestrator.slash_command_audit import (
    SlashCommandAuditSession,
    append_audit_record,
    build_audit_record,
    default_audit_path,
    redact_args,
)


def _read_jsonl(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_redact_args_masks_sensitive_commands():
    assert redact_args("notepad", ["show", "secret"]) == ["[redacted]"]
    assert redact_args("status", ["full"]) == ["full"]


def test_redact_args_masks_secret_patterns_and_truncates():
    long_arg = "token=abc123 " + ("x" * 300)
    redacted = redact_args("status", [long_arg])[0]
    assert "token=[redacted]" in redacted
    assert redacted.endswith("...[truncated]")


def test_build_audit_record_has_stable_schema(tmp_path):
    record = build_audit_record(
        agent="nana",
        command_name="status",
        args=["full"],
        source_channel="telegram",
        handler_kind="native",
        status="success",
        duration_ms=12,
        actor_id=1,
        chat_id=2,
    )
    for key in (
        "ts",
        "agent",
        "command_name",
        "args_redacted",
        "source_channel",
        "handler_kind",
        "status",
        "duration_ms",
        "actor_id",
        "chat_id",
        "error",
        "blocked_reason",
        "side_effects",
    ):
        assert key in record


def test_append_audit_record_is_json_parseable(tmp_path):
    path = tmp_path / "audit.jsonl"
    append_audit_record(path, build_audit_record(
        agent="nana",
        command_name="help",
        source_channel="workbench_api",
        handler_kind="native",
        status="success",
        duration_ms=1,
    ))
    rows = _read_jsonl(path)
    assert len(rows) == 1
    assert rows[0]["command_name"] == "help"


def test_concurrent_appends_do_not_corrupt_jsonl(tmp_path):
    path = tmp_path / "audit.jsonl"

    def _write(i: int):
        append_audit_record(path, build_audit_record(
            agent="nana",
            command_name=f"cmd{i}",
            source_channel="workbench_api",
            handler_kind="native",
            status="success",
            duration_ms=i,
        ))

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(_write, range(20)))

    rows = _read_jsonl(path)
    assert len(rows) == 20
    assert len({row["command_name"] for row in rows}) == 20


def test_session_records_denied_blocked_and_failed(tmp_path):
    path = tmp_path / "audit.jsonl"

    denied = SlashCommandAuditSession(
        audit_path=path,
        agent="nana",
        command_name="status",
        args=[],
        source_channel="telegram",
        handler_kind="native",
        actor_id=9,
        chat_id=10,
    )
    denied.deny("unauthorized")
    denied.finish()

    blocked = SlashCommandAuditSession(
        audit_path=path,
        agent="nana",
        command_name="backend",
        args=["grok-cli"],
        source_channel="telegram",
        handler_kind="native",
    )
    blocked.block("command_disabled")
    blocked.finish()

    failed = SlashCommandAuditSession(
        audit_path=path,
        agent="nana",
        command_name="queue",
        args=["clear"],
        source_channel="workbench_api",
        handler_kind="registry",
    )
    failed.fail(RuntimeError("boom"))
    failed.finish()

    rows = _read_jsonl(path)
    assert [row["status"] for row in rows] == ["denied", "blocked", "failed"]
    assert rows[0]["blocked_reason"] == "unauthorized"
    assert rows[1]["blocked_reason"] == "command_disabled"
    assert rows[2]["error"] == "boom"


class _Runtime:
    def __init__(self, tmp_path):
        self.name = "nana"
        self.workspace_dir = tmp_path
        self.global_config = SimpleNamespace(authorized_id=42)

    async def cmd_status(self, update, context):
        await update.message.reply_text("status ok")


@pytest.mark.asyncio
async def test_execute_local_command_writes_success_audit(tmp_path):
    runtime = _Runtime(tmp_path)
    result = await execute_local_command(runtime, "/status", chat_id=99)
    assert result["ok"] is True
    rows = _read_jsonl(default_audit_path(tmp_path))
    assert len(rows) == 1
    assert rows[0]["status"] == "success"
    assert rows[0]["source_channel"] == "workbench_api"
    assert rows[0]["command_name"] == "status"
    assert rows[0]["chat_id"] == 99


@pytest.mark.asyncio
async def test_execute_local_command_writes_blocked_restart_audit(tmp_path):
    runtime = _Runtime(tmp_path)
    result = await execute_local_command(runtime, "/restart", chat_id=99)
    assert result["ok"] is False
    rows = _read_jsonl(default_audit_path(tmp_path))
    assert rows[0]["status"] == "blocked"
    assert rows[0]["blocked_reason"] == "human_only_restart"


@pytest.mark.asyncio
async def test_execute_local_command_writes_failed_unknown_audit(tmp_path):
    runtime = _Runtime(tmp_path)
    result = await execute_local_command(runtime, "/definitely_missing_command", chat_id=99)
    assert result["ok"] is False
    rows = _read_jsonl(default_audit_path(tmp_path))
    assert rows[0]["status"] == "failed"
    assert "unknown command" in (rows[0]["error"] or "")
