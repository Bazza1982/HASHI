from __future__ import annotations

from types import SimpleNamespace

import pytest

from orchestrator.admin_local_testing import execute_local_command, supported_commands
from orchestrator.command_registry import load_runtime_callbacks, load_runtime_commands, runtime_bot_commands


class _FakeRuntime:
    def __init__(self):
        self.global_config = SimpleNamespace(authorized_id=1)
        self.queued = []

    async def enqueue_request(self, chat_id, prompt, source, summary, **kwargs):
        self.queued.append(
            {
                "chat_id": chat_id,
                "prompt": prompt,
                "source": source,
                "summary": summary,
                **kwargs,
            }
        )
        return "req-test"


class _RuntimeWithNativeCommands:
    def __init__(self):
        self.global_config = SimpleNamespace(authorized_id=1)

    async def cmd_mode(self, update, context):
        await update.message.reply_text("mode ok")

    async def cmd_notepad(self, update, context):
        await update.message.reply_text("notepad ok")

    async def cmd_brain(self, update, context):
        await update.message.reply_text("brain ok")


def test_runtime_command_registry_loads_external_private_commands(monkeypatch, tmp_path):
    private_dir = tmp_path / "private_commands"
    private_dir.mkdir()
    (private_dir / "sample.py").write_text(
        "from orchestrator.command_registry import RuntimeCommand\n"
        "async def callback(runtime, update, context):\n"
        "    await update.message.reply_text('private ok')\n"
        "COMMANDS = [RuntimeCommand(name='private_sample', description='Private sample', callback=callback)]\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HASHI_PRIVATE_COMMAND_DIRS", str(private_dir))

    commands = {command.name: command for command in load_runtime_commands()}

    assert "private_sample" in commands
    assert any(command.command == "private_sample" for command in runtime_bot_commands())


def test_runtime_command_registry_loads_external_private_callbacks(monkeypatch, tmp_path):
    private_dir = tmp_path / "private_commands"
    private_dir.mkdir()
    (private_dir / "sample.py").write_text(
        "from orchestrator.command_registry import RuntimeCallback\n"
        "async def callback(runtime, update, context):\n"
        "    await update.callback_query.answer()\n"
        "CALLBACKS = [RuntimeCallback(pattern=r'^private:', callback=callback)]\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HASHI_PRIVATE_COMMAND_DIRS", str(private_dir))

    callbacks = load_runtime_callbacks()

    assert any(callback.pattern == r"^private:" for callback in callbacks)


@pytest.mark.asyncio
async def test_admin_local_command_executes_registered_private_command(monkeypatch, tmp_path):
    private_dir = tmp_path / "private_commands"
    private_dir.mkdir()
    (private_dir / "sample.py").write_text(
        "from orchestrator.command_registry import RuntimeCommand\n"
        "async def callback(runtime, update, context):\n"
        "    await update.message.reply_text('private ok: ' + ' '.join(context.args))\n"
        "COMMANDS = [RuntimeCommand(name='private_sample', description='Private sample', callback=callback)]\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HASHI_PRIVATE_COMMAND_DIRS", str(private_dir))
    runtime = _FakeRuntime()

    result = await execute_local_command(runtime, "/private_sample hello", chat_id=123)

    assert result["ok"] is True
    assert result["command"] == "private_sample"
    assert "private_sample" in supported_commands(runtime)
    assert result["messages"][0]["text"] == "private ok: hello"


def test_admin_supported_commands_include_runtime_bound_native_commands():
    commands = supported_commands(_RuntimeWithNativeCommands())

    assert "mode" in commands
    assert "notepad" in commands
    assert "brain" in commands


@pytest.mark.asyncio
async def test_admin_local_command_blocks_human_restart_even_when_registered():
    runtime = _FakeRuntime()

    result = await execute_local_command(runtime, "/restart", chat_id=123)

    assert result["ok"] is False
    assert result["command"] == "restart"
    assert "human-only" in result["error"]
