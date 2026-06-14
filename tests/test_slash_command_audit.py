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

from orchestrator.admin_local_testing import try_execute_slash_command_text
from orchestrator.slash_command_audit import (
    looks_like_slash_command,
    parse_inline_callback_command,
    parse_slash_command_text,
)


def test_looks_like_and_parse_slash_command_text():
    assert looks_like_slash_command("/status full") is True
    assert looks_like_slash_command("hello") is False
    assert looks_like_slash_command("/") is False
    assert parse_slash_command_text("/status full") == ("status", ["full"])
    assert parse_slash_command_text("/help@botname") == ("help", [])


def test_parse_inline_callback_command():
    assert parse_inline_callback_command("tgl:reboot:min") == ("tgl:reboot", ["min"])
    assert parse_inline_callback_command("model:gpt-4") == ("model", ["gpt-4"])
    assert parse_inline_callback_command("") == ("callback", [])


@pytest.mark.asyncio
async def test_try_execute_slash_command_text_dispatches_and_audits(tmp_path):
    runtime = _Runtime(tmp_path)
    result = await try_execute_slash_command_text(runtime, "/status", source_channel="api_chat", chat_id=7)
    assert result is not None
    assert result["ok"] is True
    rows = _read_jsonl(default_audit_path(tmp_path))
    assert rows[0]["source_channel"] == "api_chat"
    assert rows[0]["command_name"] == "status"


@pytest.mark.asyncio
async def test_try_execute_slash_command_text_returns_none_for_non_slash(tmp_path):
    runtime = _Runtime(tmp_path)
    assert await try_execute_slash_command_text(runtime, "hello") is None


@pytest.mark.asyncio
async def test_try_execute_slash_command_text_returns_none_for_unknown(tmp_path):
    runtime = _Runtime(tmp_path)
    assert await try_execute_slash_command_text(runtime, "/definitely_missing_command") is None


@pytest.mark.asyncio
async def test_wrap_callback_audits_telegram_callback(tmp_path, monkeypatch):
    from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime

    class _MiniRuntime:
        name = "nana"
        workspace_dir = tmp_path
        global_config = SimpleNamespace(authorized_id=42)

        def _is_authorized_user(self, user_id):
            return user_id == 42

    runtime = _MiniRuntime()
    calls = []

    async def handler(update, context):
        calls.append(update.callback_query.data)

    wrapped = FlexibleAgentRuntime._wrap_callback(runtime, "callback_toggle", handler)
    query = SimpleNamespace(
        data="tgl:verbose:on",
        from_user=SimpleNamespace(id=42),
        message=SimpleNamespace(chat=SimpleNamespace(id=99)),
        answer=lambda: None,
    )
    update = SimpleNamespace(callback_query=query)
    await wrapped(update, SimpleNamespace())

    assert calls == ["tgl:verbose:on"]
    rows = _read_jsonl(default_audit_path(tmp_path))
    assert len(rows) == 1
    assert rows[0]["source_channel"] == "telegram_callback"
    assert rows[0]["command_name"] == "tgl:verbose"
    assert rows[0]["args_redacted"] == ["on"]

class _PolicyRuntime(_Runtime):
    def __init__(self, tmp_path):
        super().__init__(tmp_path)
        self._disabled_commands = {"status"}

    def _is_command_allowed(self, cmd: str) -> bool:
        return (cmd or "").lstrip("/").lower() not in self._disabled_commands


@pytest.mark.asyncio
async def test_try_execute_slash_command_text_blocks_disabled_command(tmp_path):
    runtime = _PolicyRuntime(tmp_path)
    calls = []

    async def tracked_status(update, context):
        calls.append("executed")

    runtime.cmd_status = tracked_status
    result = await try_execute_slash_command_text(runtime, "/status", source_channel="api_chat", chat_id=7)
    assert result is not None
    assert result["ok"] is False
    assert result["command"] == "status"
    assert calls == []
    rows = _read_jsonl(default_audit_path(tmp_path))
    assert len(rows) == 1
    assert rows[0]["status"] == "blocked"
    assert rows[0]["blocked_reason"] == "command_disabled"
    assert rows[0]["source_channel"] == "api_chat"


@pytest.mark.asyncio
async def test_try_execute_slash_command_text_blocks_disabled_whatsapp_forwarded(tmp_path):
    runtime = _PolicyRuntime(tmp_path)
    calls = []

    async def tracked_status(update, context):
        calls.append("executed")

    runtime.cmd_status = tracked_status
    result = await try_execute_slash_command_text(
        runtime,
        "/status",
        source_channel="whatsapp_forwarded",
        chat_id="wa-chat-1",
    )
    assert result is not None
    assert result["ok"] is False
    assert calls == []
    rows = _read_jsonl(default_audit_path(tmp_path))
    assert rows[0]["source_channel"] == "whatsapp_forwarded"
    assert rows[0]["status"] == "blocked"


@pytest.mark.asyncio
async def test_try_execute_slash_command_text_normalizes_bot_suffix(tmp_path):
    runtime = _Runtime(tmp_path)

    async def cmd_help(update, context):
        await update.message.reply_text("help ok")

    runtime.cmd_help = cmd_help
    result = await try_execute_slash_command_text(runtime, "/help@botname", source_channel="api_chat")
    assert result is not None
    assert result["ok"] is True
    assert result["command"] == "help"
    rows = _read_jsonl(default_audit_path(tmp_path))
    assert rows[0]["command_name"] == "help"

