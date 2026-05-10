from __future__ import annotations

from types import SimpleNamespace

import pytest

from orchestrator import runtime_effort


def _update():
    return SimpleNamespace(effective_user=SimpleNamespace(id=123))


def _context(*args):
    return SimpleNamespace(args=list(args))


def _runtime(*, available=None, current="medium"):
    replies = []
    set_calls = []
    runtime = SimpleNamespace(
        backend_manager=SimpleNamespace(current_backend=object()),
        _is_authorized_user=lambda user_id: True,
        _get_available_efforts=lambda: list(available or []),
        _get_current_effort=lambda: current,
        _effort_keyboard=lambda effort: ("kbd", effort),
    )

    def _set_active_effort(requested):
        set_calls.append(requested)

    async def _reply_text(update, text, **kwargs):
        replies.append((text, kwargs))

    runtime._set_active_effort = _set_active_effort
    runtime._reply_text = _reply_text
    return runtime, replies, set_calls


@pytest.mark.asyncio
async def test_cmd_effort_switches_to_requested_level():
    runtime, replies, set_calls = _runtime(available=["low", "medium", "high"])

    await runtime_effort.cmd_effort(runtime, _update(), _context("high"))

    assert set_calls == ["high"]
    assert replies[-1][0] == "Effort switched to: high"


@pytest.mark.asyncio
async def test_cmd_effort_rejects_unknown_level():
    runtime, replies, set_calls = _runtime(available=["low", "medium"])

    await runtime_effort.cmd_effort(runtime, _update(), _context("ultra"))

    assert set_calls == []
    assert replies[-1][0] == "Unknown effort level: ultra\nAvailable: low, medium"


@pytest.mark.asyncio
async def test_cmd_effort_shows_keyboard():
    runtime, replies, _set_calls = _runtime(available=["low", "medium"], current="medium")

    await runtime_effort.cmd_effort(runtime, _update(), _context())

    assert replies[-1][0] == "Current effort: medium\nSelect:"
    assert replies[-1][1]["reply_markup"] == ("kbd", "medium")
