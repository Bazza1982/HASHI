from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from orchestrator.admin_local_testing import execute_local_command, supported_commands
from orchestrator.command_registry import runtime_bot_commands
from orchestrator.commands.delay import parse_delay_request
from orchestrator.scheduler import MAX_DELAY_MINUTES, TaskScheduler


class _FakeRuntime:
    def __init__(self, tmp_path):
        self.name = "zelda"
        self.workspace_dir = tmp_path / "workspace"
        self.workspace_dir.mkdir()
        self.global_config = SimpleNamespace(authorized_id=1)
        self.queue = asyncio.Queue()
        scheduler = TaskScheduler(
            tasks_path=tmp_path / "tasks.json",
            state_path=tmp_path / "scheduler_state.json",
            runtimes=[self],
            authorized_id=1,
        )
        self.orchestrator = SimpleNamespace(scheduler=scheduler)

    def _is_authorized_user(self, user_id):
        return user_id == 1


def test_parse_delay_request_preserves_multiline_message_and_validates_minutes():
    assert parse_delay_request("5 say hi\nthen check the queue") == (
        5,
        "say hi\nthen check the queue",
    )
    with pytest.raises(ValueError, match="positive whole number"):
        parse_delay_request("0 hello")
    with pytest.raises(ValueError, match="7 days"):
        parse_delay_request(f"{MAX_DELAY_MINUTES + 1} hello")


def test_delay_is_registered_for_commands_and_telegram_menu(tmp_path):
    runtime = _FakeRuntime(tmp_path)

    assert "delay" in supported_commands(runtime)
    assert any(command.command == "delay" for command in runtime_bot_commands())


@pytest.mark.asyncio
async def test_delay_command_schedules_lists_and_cancels_persistent_message(tmp_path):
    runtime = _FakeRuntime(tmp_path)

    scheduled = await execute_local_command(
        runtime,
        "/delay 5 say hi\nthen check the queue",
        chat_id=42,
    )

    assert scheduled["ok"] is True
    text = scheduled["messages"][0]["text"]
    assert "Delayed message scheduled" in text
    records = await runtime.orchestrator.scheduler.list_delayed_messages("zelda")
    assert len(records) == 1
    assert records[0]["prompt"] == "say hi\nthen check the queue"
    delay_id = records[0]["id"]

    listed = await execute_local_command(runtime, "/delay list", chat_id=42)
    assert delay_id in listed["messages"][0]["text"]
    assert "say hi then check the queue" in listed["messages"][0]["text"]

    cancelled = await execute_local_command(
        runtime,
        f"/delay cancel {delay_id}",
        chat_id=42,
    )
    assert (
        f"Cancelled delayed message <code>{delay_id}</code>"
        in cancelled["messages"][0]["text"]
    )
    assert await runtime.orchestrator.scheduler.list_delayed_messages("zelda") == []


@pytest.mark.asyncio
async def test_delay_command_rejects_invalid_request_without_persisting(tmp_path):
    runtime = _FakeRuntime(tmp_path)

    result = await execute_local_command(runtime, "/delay later hello", chat_id=42)

    assert "positive whole number" in result["messages"][0]["text"]
    assert await runtime.orchestrator.scheduler.list_delayed_messages("zelda") == []
