from __future__ import annotations

from types import SimpleNamespace

import pytest

from orchestrator import runtime_mode


class _Backend:
    def __init__(self):
        self.session_mode = None

    def set_session_mode(self, enabled: bool):
        self.session_mode = enabled


def _runtime():
    replies = []
    backend = _Backend()
    runtime = SimpleNamespace()
    runtime.backend_manager = SimpleNamespace(
        agent_mode="flex",
        current_backend=backend,
        _save_state=lambda: replies.append("saved"),
    )
    runtime._is_authorized_user = lambda user_id: True

    async def _reply_text(update, text, **kwargs):
        replies.append((text, kwargs))

    runtime._reply_text = _reply_text
    return runtime, backend, replies


def _update():
    return SimpleNamespace(effective_user=SimpleNamespace(id=123))


def _context(*args):
    return SimpleNamespace(args=list(args))


@pytest.mark.asyncio
async def test_cmd_mode_without_args_shows_keyboard():
    runtime, _, replies = _runtime()
    await runtime_mode.cmd_mode(runtime, _update(), _context())
    assert "Current mode" in replies[-1][0]
    assert replies[-1][1]["parse_mode"] == "HTML"


@pytest.mark.asyncio
async def test_cmd_mode_switches_to_fixed():
    runtime, backend, replies = _runtime()
    await runtime_mode.cmd_mode(runtime, _update(), _context("fixed"))
    assert runtime.backend_manager.agent_mode == "fixed"
    assert backend.session_mode is True
    assert "saved" in replies


@pytest.mark.asyncio
async def test_callback_mode_toggle_updates_message():
    runtime, backend, replies = _runtime()
    edits = []
    answers = []

    async def _edit_message_text(text, **kwargs):
        edits.append((text, kwargs))

    async def _answer(text=None):
        answers.append(text)

    query = SimpleNamespace(
        message=SimpleNamespace(chat_id=456),
        edit_message_text=_edit_message_text,
        answer=_answer,
    )
    await runtime_mode.callback_mode_toggle(runtime, query, "fixed")
    assert runtime.backend_manager.agent_mode == "fixed"
    assert backend.session_mode is True
    assert "saved" in replies
    assert "Mode: <b>fixed</b>" in edits[0][0]
    assert answers[-1] == "Switched to fixed"
