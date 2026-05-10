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


def test_runtime_command_registry_includes_slim_core_runtime_commands():
    commands = {command.name: command for command in load_runtime_commands()}

    for name in ("agents", "backend", "cos", "credit", "debug", "end", "exp", "fork", "group", "handoff", "hchat", "help", "load", "logo", "long", "loop", "move", "park", "say", "skill", "start", "status", "stop", "sys", "ticket", "token", "transfer", "mode", "model", "effort", "retry", "safevoice", "usage", "usecomputer", "usercomputer", "voice", "wa_off", "wa_on", "wa_send", "whisper", "new", "fresh", "memory", "wipe", "reset", "clear", "remote", "reboot", "terminate", "anatta"):
        assert name in commands

    bot_names = [command.command for command in runtime_bot_commands()]
    for name in ("agents", "backend", "cos", "credit", "debug", "end", "exp", "fork", "group", "handoff", "hchat", "help", "load", "logo", "long", "loop", "move", "park", "say", "skill", "start", "status", "stop", "sys", "ticket", "transfer", "usecomputer", "usercomputer", "model", "effort", "retry", "safevoice", "voice", "wa_off", "wa_on", "wa_send", "whisper", "remote", "reboot", "terminate", "anatta"):
        assert bot_names.count(name) == 1


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
