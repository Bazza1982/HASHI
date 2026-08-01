from __future__ import annotations

from types import SimpleNamespace

import pytest

from orchestrator.commands.notify import notify_command
from orchestrator.telegram_notifications import notify_enabled


class _Message:
    def __init__(self):
        self.replies = []

    async def reply_text(self, text, **kwargs):
        self.replies.append((text, kwargs))


def _runtime(tmp_path, *, authorized=True):
    replies = []

    async def _reply_text(_update, text, **kwargs):
        replies.append((text, kwargs))

    return SimpleNamespace(
        workspace_dir=tmp_path,
        _notify_enabled=False,
        _is_authorized_user=lambda _user_id: authorized,
        _reply_text=_reply_text,
        replies=replies,
    )


def _update():
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=123),
        effective_chat=SimpleNamespace(id=456),
        message=_Message(),
    )


@pytest.mark.asyncio
async def test_notify_defaults_off_and_reports_status(tmp_path):
    runtime = _runtime(tmp_path)

    await notify_command(runtime, _update(), SimpleNamespace(args=[]))

    assert notify_enabled(runtime) is False
    text, kwargs = runtime.replies[0]
    assert "<b>TELEGRAM NOTIFICATIONS</b>" in text
    assert "<b>Current</b> · <b>OFF</b>" in text
    assert kwargs["parse_mode"] == "HTML"
    labels = [button.text for row in kwargs["reply_markup"].inline_keyboard for button in row]
    assert "✓ Off" in labels


@pytest.mark.asyncio
async def test_notify_on_persists_marker(tmp_path):
    runtime = _runtime(tmp_path)

    await notify_command(runtime, _update(), SimpleNamespace(args=["on"]))

    assert notify_enabled(runtime) is True
    assert (tmp_path / ".notify_on").exists()
    assert "<b>Current</b> · <b>ON</b>" in runtime.replies[0][0]
    labels = [
        button.text
        for row in runtime.replies[0][1]["reply_markup"].inline_keyboard
        for button in row
    ]
    assert "✓ On" in labels


@pytest.mark.asyncio
async def test_notify_off_removes_marker(tmp_path):
    marker = tmp_path / ".notify_on"
    marker.touch()
    runtime = _runtime(tmp_path)
    runtime._notify_enabled = True

    await notify_command(runtime, _update(), SimpleNamespace(args=["off"]))

    assert notify_enabled(runtime) is False
    assert not marker.exists()
    assert "<b>Current</b> · <b>OFF</b>" in runtime.replies[0][0]
