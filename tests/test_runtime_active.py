from __future__ import annotations

from types import SimpleNamespace

import pytest

from orchestrator import runtime_active


def _update():
    return SimpleNamespace(effective_user=SimpleNamespace(id=123))


def _context(*args):
    return SimpleNamespace(args=list(args))


class _SkillManager:
    ACTIVE_HEARTBEAT_DEFAULT_MINUTES = 20

    def __init__(self):
        self.calls = []

    def describe_active_heartbeat(self, agent_name):
        self.calls.append(("describe", agent_name))
        return "heartbeat status"

    def set_active_heartbeat(self, agent_name, *, enabled, minutes=None):
        self.calls.append(("set", agent_name, enabled, minutes))
        return True, f"active {enabled} {minutes}"


def _runtime():
    replies = []
    runtime = SimpleNamespace(
        name="lin_yueru",
        skill_manager=_SkillManager(),
        _is_authorized_user=lambda user_id: True,
        _active_keyboard=lambda: "kbd",
    )

    async def _reply_text(update, text, **kwargs):
        replies.append((text, kwargs))

    runtime._reply_text = _reply_text
    return runtime, replies


@pytest.mark.asyncio
async def test_cmd_active_shows_status_by_default():
    runtime, replies = _runtime()

    await runtime_active.cmd_active(runtime, _update(), _context())

    assert runtime.skill_manager.calls == [("describe", "lin_yueru")]
    assert replies[-1][0] == "heartbeat status"
    assert replies[-1][1]["reply_markup"] == "kbd"


@pytest.mark.asyncio
async def test_cmd_active_turns_on_with_minutes():
    runtime, replies = _runtime()

    await runtime_active.cmd_active(runtime, _update(), _context("on", "15"))

    assert runtime.skill_manager.calls == [("set", "lin_yueru", True, 15)]
    assert replies[-1][0] == "active True 15"


@pytest.mark.asyncio
async def test_cmd_active_turns_off():
    runtime, replies = _runtime()

    await runtime_active.cmd_active(runtime, _update(), _context("off"))

    assert runtime.skill_manager.calls == [("set", "lin_yueru", False, None)]
    assert replies[-1][0] == "active False None"
