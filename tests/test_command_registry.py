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


class _FakeBackgroundJobRecord:
    job_id = "job-test"
    state = "succeeded"
    returncode = 0
    created_at = "2026-07-07T00:00:00+00:00"
    updated_at = "2026-07-07T00:00:01+00:00"
    error = None
    command = {"display": "python smoke.py"}


class _FakeBackgroundJobManager:
    def __init__(self):
        self.records = [_FakeBackgroundJobRecord()]

    def list(self, **kwargs):
        return list(self.records)

    def get(self, job_id):
        return self.records[0] if job_id == "job-test" else None

    def tail(self, job_id, **kwargs):
        if job_id != "job-test":
            raise KeyError(job_id)
        return "smoke-ok"

    async def cancel(self, job_id):
        if job_id != "job-test":
            raise KeyError(job_id)
        self.records[0].state = "cancelled"
        return self.records[0]


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


@pytest.mark.asyncio
async def test_admin_local_command_exposes_message_text_to_handlers(monkeypatch, tmp_path):
    private_dir = tmp_path / "private_commands"
    private_dir.mkdir()
    (private_dir / "sample.py").write_text(
        "from orchestrator.command_registry import RuntimeCommand\n"
        "async def callback(runtime, update, context):\n"
        "    await update.message.reply_text('text: ' + update.message.text)\n"
        "COMMANDS = [RuntimeCommand(name='read_text', description='Read text', callback=callback)]\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HASHI_PRIVATE_COMMAND_DIRS", str(private_dir))
    runtime = _FakeRuntime()

    result = await execute_local_command(runtime, "/read_text validate sl-demo", chat_id=123)

    assert result["ok"] is True
    assert result["messages"][0]["text"] == "text: /read_text validate sl-demo"


@pytest.mark.asyncio
async def test_bg_command_defaults_to_run_and_preserves_task_text():
    runtime = _FakeRuntime()

    result = await execute_local_command(runtime, "/bg full citalio service for paper 3", chat_id=123)

    assert result["ok"] is True
    assert result["command"] == "bg"
    assert result["messages"][0]["text"].startswith("Background-capable task queued.")
    assert runtime.queued[0]["chat_id"] == 123
    assert runtime.queued[0]["source"] == "background:prompt"
    assert runtime.queued[0]["summary"] == "Background task: full citalio service for paper 3"
    assert "--- USER TASK ---\nfull citalio service for paper 3" in runtime.queued[0]["prompt"]
    assert "BackgroundJobManager" in runtime.queued[0]["prompt"]


@pytest.mark.asyncio
async def test_bg_command_run_alias_matches_default_run():
    runtime = _FakeRuntime()

    result = await execute_local_command(runtime, "/bg run quoted task", chat_id=123)

    assert result["ok"] is True
    assert runtime.queued[0]["source"] == "background:prompt"
    assert "--- USER TASK ---\nquoted task" in runtime.queued[0]["prompt"]


@pytest.mark.asyncio
async def test_bg_command_reserved_status_is_not_treated_as_task():
    runtime = _FakeRuntime()
    runtime.background_job_manager = _FakeBackgroundJobManager()

    result = await execute_local_command(runtime, "/bg status", chat_id=123)

    assert result["ok"] is True
    assert runtime.queued == []
    assert "Background job status" in result["messages"][0]["text"]


@pytest.mark.asyncio
async def test_bg_command_tail_uses_background_job_manager():
    runtime = _FakeRuntime()
    runtime.background_job_manager = _FakeBackgroundJobManager()

    result = await execute_local_command(runtime, "/bg tail job-test", chat_id=123)

    assert result["ok"] is True
    assert runtime.queued == []
    assert "smoke-ok" in result["messages"][0]["text"]


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
