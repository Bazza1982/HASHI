from __future__ import annotations

from types import SimpleNamespace

import pytest

from orchestrator import runtime_help


def _update():
    return SimpleNamespace(effective_user=SimpleNamespace(id=123))


def _context(*args):
    return SimpleNamespace(args=list(args))


def _command(name: str, description: str):
    return SimpleNamespace(command=name, description=description)


@pytest.mark.asyncio
async def test_cmd_help_lists_enabled_and_disabled_commands():
    replies = []
    runtime = SimpleNamespace(
        name="lin_yueru",
        config=SimpleNamespace(type="flex"),
        get_bot_commands=lambda: [
            _command("help", "Show help menu"),
            _command("status", "View agent status"),
            _command("secret", "Private command"),
        ],
        _is_authorized_user=lambda user_id: True,
        _is_command_allowed=lambda name: name != "secret",
    )

    async def _reply_text(update, text, **kwargs):
        replies.append((text, kwargs))

    runtime._reply_text = _reply_text

    await runtime_help.cmd_help(runtime, _update(), _context())

    text = replies[-1][0]
    assert "Agent lin_yueru (flex) Commands" in text
    assert "/help - Show help menu" in text
    assert "/status - View agent status" in text
    assert "Disabled for this agent:" in text
    assert "/secret" in text
