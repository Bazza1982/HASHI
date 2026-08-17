from types import SimpleNamespace

import pytest

from orchestrator import runtime_skill_callbacks


class _Query:
    def __init__(self, data: str = "skill:show:demo"):
        self.data = data
        self.message = SimpleNamespace(chat_id=123)
        self.answers = []
        self.edits = []

    async def answer(self, text=None, **kwargs):
        self.answers.append({"text": text, **kwargs})

    async def edit_message_text(self, text, **kwargs):
        self.edits.append({"text": text, **kwargs})


class _SkillManager:
    def __init__(self):
        self.skill = SimpleNamespace(id="demo")

    def get_skill(self, skill_id: str):
        return self.skill if skill_id == "demo" else None

    def list_skills(self):
        return [self.skill]

    def describe_skill(self, skill, workspace_dir):
        return f"Skill {skill.id}"


def _runtime():
    sent = []
    return SimpleNamespace(
        skill_manager=_SkillManager(),
        name="momo",
        workspace_dir="/tmp/workspace",
        config=SimpleNamespace(active_backend="codex-cli"),
        get_current_model=lambda: "gpt-test",
        _skill_action_keyboard=lambda skill: "keyboard",
        send_long_message=lambda **kwargs: _send(sent, kwargs),
        sent_messages=sent,
    )


async def _send(sent, kwargs):
    sent.append(kwargs)


@pytest.mark.asyncio
async def test_handle_skill_show_callback():
    runtime = _runtime()
    query = _Query("skill:show:demo")

    handled = await runtime_skill_callbacks.handle_skill_callback(runtime, query, query.data)

    assert handled is True
    assert "<b>DEMO</b>" in query.edits[-1]["text"]
    assert "<b>Current</b> · <b>READY</b>" in query.edits[-1]["text"]
    assert query.edits[-1]["parse_mode"] == "HTML"
    assert query.edits[-1]["reply_markup"] == "keyboard"
    assert query.answers[-1]["text"] is None


@pytest.mark.asyncio
async def test_handle_skill_back_callback_renders_flat_standard_catalog():
    runtime = _runtime()
    runtime._skill_keyboard = lambda: "catalog-keyboard"
    query = _Query("skill:back:menu")

    handled = await runtime_skill_callbacks.handle_skill_callback(runtime, query, query.data)

    assert handled is True
    assert "1" in query.edits[-1]["text"]
    assert query.edits[-1]["reply_markup"] == "catalog-keyboard"
    assert query.answers[-1]["text"] is None


@pytest.mark.asyncio
async def test_handle_legacy_skill_toggle_callback_is_disabled():
    runtime = _runtime()
    query = _Query("skill:toggle:demo:on")

    handled = await runtime_skill_callbacks.handle_skill_callback(runtime, query, query.data)

    assert handled is True
    assert query.answers[-1]["show_alert"] is True
    assert "legacy Skill action is disabled" in query.answers[-1]["text"]
    assert query.edits == []


@pytest.mark.asyncio
async def test_handle_legacy_skill_run_callback_is_disabled():
    runtime = _runtime()
    query = _Query("skill:run:demo")

    handled = await runtime_skill_callbacks.handle_skill_callback(runtime, query, query.data)

    assert handled is True
    assert query.answers[-1]["show_alert"] is True
    assert "legacy Skill action is disabled" in query.answers[-1]["text"]
    assert runtime.sent_messages == []


@pytest.mark.asyncio
async def test_handle_removed_cron_skill_callback_redirects_to_jobs():
    runtime = _runtime()
    query = _Query("skill:jobs:cron")

    handled = await runtime_skill_callbacks.handle_skill_callback(runtime, query, query.data)

    assert handled is True
    assert query.answers[-1]["show_alert"] is True
    assert "/jobs" in query.answers[-1]["text"]
    assert query.edits == []


@pytest.mark.asyncio
async def test_handle_unknown_skill_callback_answers_alert():
    runtime = _runtime()
    query = _Query("skill:show:missing")

    handled = await runtime_skill_callbacks.handle_skill_callback(runtime, query, query.data)

    assert handled is True
    assert query.answers[-1] == {"text": "Unknown skill", "show_alert": True}
