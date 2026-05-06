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
        self.toggles = []
        self.runs = []

    def get_skill(self, skill_id: str):
        return self.skill if skill_id == "demo" else None

    def describe_skill(self, skill, workspace_dir):
        return f"Skill {skill.id}"

    def set_toggle_state(self, workspace_dir, skill_id: str, *, enabled: bool):
        self.toggles.append((skill_id, enabled))
        return True, f"{skill_id} {'on' if enabled else 'off'}"

    async def run_action_skill(self, skill, workspace_dir, *, extra_env):
        self.runs.append((skill.id, extra_env))
        return True, "skill output"


def _runtime():
    sent = []
    return SimpleNamespace(
        skill_manager=_SkillManager(),
        workspace_dir="/tmp/workspace",
        config=SimpleNamespace(active_backend="codex-cli"),
        get_current_model=lambda: "gpt-test",
        _skill_action_keyboard=lambda skill: "keyboard",
        _render_skill_jobs=None,
        _build_habit_browser_view=None,
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
    assert query.edits[-1]["text"] == "Skill demo"
    assert query.edits[-1]["reply_markup"] == "keyboard"
    assert query.answers[-1]["text"] is None


@pytest.mark.asyncio
async def test_handle_skill_toggle_callback():
    runtime = _runtime()
    query = _Query("skill:toggle:demo:on")

    handled = await runtime_skill_callbacks.handle_skill_callback(runtime, query, query.data)

    assert handled is True
    assert runtime.skill_manager.toggles == [("demo", True)]
    assert query.edits[-1]["text"] == "demo on"


@pytest.mark.asyncio
async def test_handle_skill_run_callback_sends_output():
    runtime = _runtime()
    query = _Query("skill:run:demo")

    handled = await runtime_skill_callbacks.handle_skill_callback(runtime, query, query.data)

    assert handled is True
    assert runtime.skill_manager.runs == [
        (
            "demo",
            {
                "BRIDGE_ACTIVE_BACKEND": "codex-cli",
                "BRIDGE_ACTIVE_MODEL": "gpt-test",
            },
        )
    ]
    assert query.answers[-1]["text"] == "Skill executed"
    assert runtime.sent_messages[-1]["text"] == "skill output"


@pytest.mark.asyncio
async def test_handle_unknown_skill_callback_answers_alert():
    runtime = _runtime()
    query = _Query("skill:show:missing")

    handled = await runtime_skill_callbacks.handle_skill_callback(runtime, query, query.data)

    assert handled is True
    assert query.answers[-1] == {"text": "Unknown skill", "show_alert": True}
