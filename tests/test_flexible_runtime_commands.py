from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
import sys

import pytest

sys.modules.setdefault("edge_tts", SimpleNamespace())

from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime


class _FakeMessage:
    def __init__(self, text: str):
        self.text = text
        self.replies: list[str] = []

    async def reply_text(self, text: str, **kwargs):
        self.replies.append(text)
        return SimpleNamespace(ok=True)


class _FakeUpdate:
    def __init__(self, text: str):
        self.effective_user = SimpleNamespace(id=123)
        self.effective_chat = SimpleNamespace(id=456)
        self.message = _FakeMessage(text)


class _FakeLogger:
    def exception(self, *args, **kwargs):
        return None


@pytest.mark.asyncio
async def test_handle_workspace_command_prefers_runtime_anatta(tmp_path: Path):
    sent: list[dict[str, str]] = []

    async def send_long_message(chat_id, text, request_id, purpose):
        sent.append(
            {
                "chat_id": chat_id,
                "text": text,
                "request_id": request_id,
                "purpose": purpose,
            }
        )

    runtime = SimpleNamespace(
        workspace_dir=tmp_path,
        send_long_message=send_long_message,
        _is_authorized_user=lambda user_id: True,
        _record_active_chat=lambda update: None,
        _is_command_allowed=lambda command_name: True,
        error_logger=_FakeLogger(),
    )

    async def _reply_text(update, text):
        await update.message.reply_text(text)

    runtime._reply_text = _reply_text
    update = _FakeUpdate("/anatta")
    context = SimpleNamespace(args=[])

    await FlexibleAgentRuntime.handle_workspace_command(runtime, update, context)

    assert len(sent) == 1
    assert sent[0]["chat_id"] == 456
    assert sent[0]["request_id"] == "anatta-command"
    assert sent[0]["purpose"] == "command"
    assert "Anatta diagnostics:" in sent[0]["text"]
    assert "workspace-command-anatta" not in sent[0]["request_id"]


def test_get_bot_commands_includes_runtime_anatta_once(tmp_path: Path):
    (tmp_path / "workspace_commands.json").write_text(
        json.dumps(
            {
                "commands": [
                    {
                        "name": "anatta",
                        "description": "Legacy workspace anatta",
                        "factory": "orchestrator.anatta.command:build_workspace_command",
                        "enabled": True,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    runtime = SimpleNamespace(
        workspace_dir=tmp_path,
        _is_command_allowed=lambda command_name: True,
    )

    commands = FlexibleAgentRuntime.get_bot_commands(runtime)
    anatta_commands = [command for command in commands if command.command == "anatta"]

    assert len(anatta_commands) == 1
    assert anatta_commands[0].description == "Read-only Anatta diagnostics"
