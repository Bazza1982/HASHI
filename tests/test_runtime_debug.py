from __future__ import annotations

from types import SimpleNamespace

import pytest

from orchestrator import runtime_debug


def _update():
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=123),
        effective_chat=SimpleNamespace(id=456),
    )


def _context(*args):
    return SimpleNamespace(args=list(args))


def _runtime(skill_manager):
    replies = []
    queued = []
    runtime = SimpleNamespace(
        _is_authorized_user=lambda user_id: True,
        workspace_dir="/tmp/workspace",
        skill_manager=skill_manager,
    )

    async def _reply_text(update, text, **kwargs):
        replies.append((text, kwargs))

    async def enqueue_request(chat_id, prompt, source, summary):
        queued.append((chat_id, prompt, source, summary))

    runtime._reply_text = _reply_text
    runtime.enqueue_request = enqueue_request
    return runtime, replies, queued


@pytest.mark.asyncio
async def test_cmd_debug_reports_missing_skill_manager_for_toggle():
    runtime, replies, queued = _runtime(skill_manager=None)

    await runtime_debug.cmd_debug(runtime, _update(), _context("on"))

    assert replies[-1][0] == "Skill manager not available."
    assert queued == []


@pytest.mark.asyncio
async def test_cmd_debug_toggles_mode():
    class _SkillManager:
        def set_toggle_state(self, workspace_dir, name, enabled):
            assert workspace_dir == "/tmp/workspace"
            assert name == "debug"
            return True, "saved"

    runtime, replies, queued = _runtime(skill_manager=_SkillManager())

    await runtime_debug.cmd_debug(runtime, _update(), _context("off"))

    assert replies[-1][0] == "🐛 Debug mode: OFF\nsaved"
    assert queued == []


@pytest.mark.asyncio
async def test_cmd_debug_reports_missing_skill_system():
    runtime, replies, queued = _runtime(skill_manager=None)

    await runtime_debug.cmd_debug(runtime, _update(), _context("investigate", "this"))

    assert replies[-1][0] == "Skill system is not configured."
    assert queued == []


@pytest.mark.asyncio
async def test_cmd_debug_reports_unknown_skill():
    class _SkillManager:
        def get_skill(self, skill_id):
            assert skill_id == "debug"
            return None

    runtime, replies, queued = _runtime(skill_manager=_SkillManager())

    await runtime_debug.cmd_debug(runtime, _update(), _context("investigate"))

    assert replies[-1][0] == "Unknown skill: debug"
    assert queued == []


@pytest.mark.asyncio
async def test_cmd_debug_reports_usage_without_prompt():
    class _SkillManager:
        def get_skill(self, skill_id):
            return SimpleNamespace(id=skill_id)

    runtime, replies, queued = _runtime(skill_manager=_SkillManager())

    await runtime_debug.cmd_debug(runtime, _update(), _context())

    assert replies[-1][0] == "Usage: /debug <prompt> or /debug on|off"
    assert queued == []


@pytest.mark.asyncio
async def test_cmd_debug_enqueues_skill_prompt():
    class _SkillManager:
        def get_skill(self, skill_id):
            return SimpleNamespace(id=skill_id)

        def build_prompt_for_skill(self, skill, prompt_text):
            assert skill.id == "debug"
            assert prompt_text == "investigate this"
            return "PROMPT"

    runtime, replies, queued = _runtime(skill_manager=_SkillManager())

    await runtime_debug.cmd_debug(runtime, _update(), _context("investigate", "this"))

    assert replies[-1][0] == "Running skill debug..."
    assert queued == [(456, "PROMPT", "skill:debug", "Skill debug")]
