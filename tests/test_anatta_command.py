from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from orchestrator.command_registry import load_runtime_commands
from orchestrator.commands.anatta import anatta_command
from tools.anatta_diagnostics import build_report


def test_anatta_diagnostics_report_is_read_only_and_omits_memory_text(tmp_path):
    (tmp_path / "anatta_config.json").write_text(json.dumps({"mode": "shadow"}), encoding="utf-8")
    (tmp_path / "post_turn_observers.json").write_text(
        json.dumps(
            {
                "observers": [
                    {
                        "factory": "orchestrator.anatta.post_turn_observer:build_post_turn_observer",
                        "enabled": True,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    report = build_report(tmp_path, full=True)

    assert "mode: shadow" in report
    assert "observers: 1/1 enabled" in report
    assert "memory contents: not printed" in report
    assert "config writes: disabled" in report
    assert "orchestrator.anatta.post_turn_observer:build_post_turn_observer" in report


@pytest.mark.asyncio
async def test_anatta_command_sends_read_only_status(tmp_path):
    runtime = SimpleNamespace(
        workspace_dir=tmp_path,
        sent=[],
    )

    async def send_long_message(chat_id, text, request_id, purpose):
        runtime.sent.append(
            {
                "chat_id": chat_id,
                "text": text,
                "request_id": request_id,
                "purpose": purpose,
            }
        )

    runtime.send_long_message = send_long_message
    update = SimpleNamespace(effective_chat=SimpleNamespace(id=123), message=None)
    context = SimpleNamespace(args=[])

    await anatta_command(runtime, update, context)

    assert runtime.sent[0]["chat_id"] == 123
    assert runtime.sent[0]["request_id"] == "anatta-command"
    assert runtime.sent[0]["purpose"] == "command"
    assert "command mode: read-only" in runtime.sent[0]["text"]


def test_anatta_runtime_command_is_registered():
    commands = {command.name: command for command in load_runtime_commands()}

    assert commands["anatta"].description == "Read-only Anatta diagnostics"
