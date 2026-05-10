from __future__ import annotations

from types import SimpleNamespace

import pytest

from orchestrator import runtime_skill


def _update():
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=123),
        effective_chat=SimpleNamespace(id=456),
    )


def _context(*args):
    return SimpleNamespace(args=list(args))


def _callback_update(data):
    answers = []
    edits = []

    async def answer(text=None, show_alert=False):
        answers.append((text, show_alert))

    async def edit_message_text(text, **kwargs):
        edits.append((text, kwargs))

    return SimpleNamespace(
        callback_query=SimpleNamespace(
            from_user=SimpleNamespace(id=123),
            data=data,
            message=SimpleNamespace(chat_id=456),
            answer=answer,
            edit_message_text=edit_message_text,
        )
    ), answers, edits


def _runtime(skill_manager):
    replies = []
    queued = []
    runtime = SimpleNamespace(
        _is_authorized_user=lambda user_id: True,
        workspace_dir="/tmp/workspace",
        skill_manager=skill_manager,
        _skill_keyboard=lambda: "KEYBOARD",
        _skill_action_keyboard=lambda skill: f"ACTIONS:{skill.id}",
        _skills_by_type=lambda: {},
        config=SimpleNamespace(active_backend="openai", allowed_backends=[{"engine": "openai"}]),
        get_current_model=lambda: "gpt-test",
        _get_active_skill_sections=lambda: [],
    )

    async def _reply_text(update, text, **kwargs):
        replies.append((text, kwargs))

    async def enqueue_request(chat_id, prompt, source, summary):
        queued.append((chat_id, prompt, source, summary))

    async def send_long_message(chat_id, text, request_id, purpose):
        replies.append((text, {"chat_id": chat_id, "request_id": request_id, "purpose": purpose}))

    runtime._reply_text = _reply_text
    runtime.enqueue_request = enqueue_request
    runtime.send_long_message = send_long_message
    runtime._render_skill_jobs = lambda update, kind: None
    runtime._build_habit_browser_view = lambda: ("HABITS", "MARKUP")
    runtime._switch_backend_mode = None
    runtime._send_text = None
    return runtime, replies, queued


@pytest.mark.asyncio
async def test_cmd_skill_shows_keyboard_without_args():
    runtime, replies, queued = _runtime(skill_manager=object())

    await runtime_skill.cmd_skill(runtime, _update(), _context())

    assert replies[-1][0] == "Skills"
    assert replies[-1][1]["reply_markup"] == "KEYBOARD"
    assert queued == []


@pytest.mark.asyncio
async def test_cmd_skill_reports_unknown_skill():
    class _SkillManager:
        def get_skill(self, skill_id):
            assert skill_id == "missing"
            return None

    runtime, replies, queued = _runtime(skill_manager=_SkillManager())

    await runtime_skill.cmd_skill(runtime, _update(), _context("missing"))

    assert replies[-1][0] == "Unknown skill: missing"
    assert queued == []


@pytest.mark.asyncio
async def test_cmd_skill_shows_toggle_description_without_on_off():
    skill = SimpleNamespace(id="debug", type="toggle")

    class _SkillManager:
        def get_skill(self, skill_id):
            return skill

        def describe_skill(self, resolved_skill, workspace_dir):
            assert resolved_skill is skill
            assert workspace_dir == "/tmp/workspace"
            return "toggle description"

    runtime, replies, queued = _runtime(skill_manager=_SkillManager())

    await runtime_skill.cmd_skill(runtime, _update(), _context("debug"))

    assert replies[-1][0] == "toggle description"
    assert replies[-1][1]["reply_markup"] == "ACTIONS:debug"
    assert queued == []


@pytest.mark.asyncio
async def test_cmd_skill_enqueues_prompt_skill():
    skill = SimpleNamespace(id="research", type="prompt", backend=None)

    class _SkillManager:
        def get_skill(self, skill_id):
            assert skill_id == "research"
            return skill

        def build_prompt_for_skill(self, resolved_skill, rest):
            assert resolved_skill is skill
            assert rest == "investigate this"
            return "PROMPT"

    runtime, replies, queued = _runtime(skill_manager=_SkillManager())

    await runtime_skill.cmd_skill(runtime, _update(), _context("research", "investigate", "this"))

    assert replies[-1][0] == "Running skill research..."
    assert queued == [(456, "PROMPT", "skill:research", "Skill research")]


@pytest.mark.asyncio
async def test_callback_skill_ignores_noop():
    runtime, replies, queued = _runtime(skill_manager=object())
    update, answers, edits = _callback_update("skill:noop:none")

    await runtime_skill.callback_skill(runtime, update, SimpleNamespace())

    assert answers[-1] == (None, False)
    assert edits == []
    assert replies == []
    assert queued == []


@pytest.mark.asyncio
async def test_callback_skill_shows_described_skill():
    skill = SimpleNamespace(id="debug")

    class _SkillManager:
        def get_skill(self, skill_id):
            assert skill_id == "debug"
            return skill

        def describe_skill(self, resolved_skill, workspace_dir):
            assert resolved_skill is skill
            return "skill detail"

    runtime, replies, queued = _runtime(skill_manager=_SkillManager())
    update, answers, edits = _callback_update("skill:show:debug")

    await runtime_skill.callback_skill(runtime, update, SimpleNamespace())

    assert edits[-1][0] == "skill detail"
    assert edits[-1][1]["reply_markup"] == "ACTIONS:debug"
    assert answers[-1] == (None, False)
    assert replies == []
    assert queued == []
