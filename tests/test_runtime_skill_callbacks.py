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
        self.skill = SimpleNamespace(
            id="demo",
            name="demo",
            description="Use when testing callbacks.",
            body="# Demo",
            source_type="installed",
            source="/tmp/source/demo",
            scope="project",
            managed=True,
            version="1.0",
            license=None,
            compatibility=None,
            allowed_tools=None,
            metadata={"version": "1.0"},
        )
        self.enabled = True
        self.uninstalled = False

    def get_skill(self, skill_id: str):
        return self.skill if skill_id == "demo" else None

    def list_skills(self):
        return [] if self.uninstalled else [self.skill]

    def skill_validation_errors(self):
        return ["bad/SKILL.md: invalid YAML"]

    def skill_callback_key(self, skill_id: str):
        return "demo-key"

    def get_skill_by_callback_key(self, key: str):
        return self.skill if key == "demo-key" and not self.uninstalled else None

    def is_skill_enabled(self, workspace_dir, skill_id: str):
        return self.enabled

    def set_skill_enabled(
        self, workspace_dir, skill_id: str, enabled: bool, actor="user"
    ):
        self.enabled = enabled
        return True, f"Skill '{skill_id}' {'enabled' if enabled else 'disabled'}."

    def skill_dependencies(self, skill_id: str, *, enabled_only: bool = False):
        return []

    def skill_resource_counts(self, skill):
        return {"scripts": 0, "references": 0, "assets": 0, "other": 1}

    def can_uninstall_skill(self, skill):
        return skill.source_type in {"project", "installed", "linked"}

    def uninstall_skill(self, skill_id: str):
        self.uninstalled = True
        return True, f"Skill '{skill_id}' uninstalled.", "/tmp/recovery"

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
        _skill_keyboard=lambda: "catalog-keyboard",
        send_long_message=lambda **kwargs: _send(sent, kwargs),
        sent_messages=sent,
    )


async def _send(sent, kwargs):
    sent.append(kwargs)


@pytest.mark.asyncio
async def test_handle_skill_show_callback():
    runtime = _runtime()
    query = _Query("skill:s:demo-key")

    handled = await runtime_skill_callbacks.handle_skill_callback(
        runtime, query, query.data
    )

    assert handled is True
    assert "<b>DEMO</b>" in query.edits[-1]["text"]
    assert "<b>Current</b> · <b>ENABLED</b>" in query.edits[-1]["text"]
    assert query.edits[-1]["parse_mode"] == "HTML"
    assert query.edits[-1]["reply_markup"] == "keyboard"
    assert query.answers[-1]["text"] is None


@pytest.mark.asyncio
async def test_handle_skill_back_callback_renders_flat_standard_catalog():
    runtime = _runtime()
    runtime._skill_keyboard = lambda: "catalog-keyboard"
    query = _Query("skill:back:menu")

    handled = await runtime_skill_callbacks.handle_skill_callback(
        runtime, query, query.data
    )

    assert handled is True
    assert "1" in query.edits[-1]["text"]
    assert query.edits[-1]["reply_markup"] == "catalog-keyboard"
    assert query.answers[-1]["text"] is None


@pytest.mark.asyncio
async def test_handle_skill_disable_and_enable_callbacks():
    runtime = _runtime()
    disable_query = _Query("skill:d:demo-key")

    handled = await runtime_skill_callbacks.handle_skill_callback(
        runtime,
        disable_query,
        disable_query.data,
    )

    assert handled is True
    assert runtime.skill_manager.enabled is False
    assert "DISABLED" in disable_query.edits[-1]["text"]

    enable_query = _Query("skill:e:demo-key")
    await runtime_skill_callbacks.handle_skill_callback(
        runtime, enable_query, enable_query.data
    )
    assert runtime.skill_manager.enabled is True
    assert "ENABLED" in enable_query.edits[-1]["text"]


@pytest.mark.asyncio
async def test_handle_skill_validation_and_invalid_package_callbacks():
    runtime = _runtime()
    validate_query = _Query("skill:v:demo-key")
    invalid_query = _Query("skill:z:all")

    await runtime_skill_callbacks.handle_skill_callback(
        runtime, validate_query, validate_query.data
    )
    await runtime_skill_callbacks.handle_skill_callback(
        runtime, invalid_query, invalid_query.data
    )

    assert "VALID" in validate_query.edits[-1]["text"]
    assert "invalid YAML" in invalid_query.edits[-1]["text"]


@pytest.mark.asyncio
async def test_handle_skill_uninstall_requires_confirmation():
    runtime = _runtime()
    request_query = _Query("skill:x:demo-key")

    await runtime_skill_callbacks.handle_skill_callback(
        runtime, request_query, request_query.data
    )

    assert runtime.skill_manager.uninstalled is False
    assert "UNINSTALL SKILL" in request_query.edits[-1]["text"]

    confirm_query = _Query("skill:xc:demo-key")
    await runtime_skill_callbacks.handle_skill_callback(
        runtime, confirm_query, confirm_query.data
    )

    assert runtime.skill_manager.uninstalled is True
    assert "0" in confirm_query.edits[-1]["text"]


@pytest.mark.asyncio
async def test_handle_project_skill_delete_requires_confirmation():
    runtime = _runtime()
    runtime.skill_manager.skill.source_type = "project"
    runtime.skill_manager.skill.managed = False
    request_query = _Query("skill:x:demo-key")

    await runtime_skill_callbacks.handle_skill_callback(
        runtime,
        request_query,
        request_query.data,
    )

    assert runtime.skill_manager.uninstalled is False
    assert "DELETE SKILL" in request_query.edits[-1]["text"]
    labels = [
        button.text
        for row in request_query.edits[-1]["reply_markup"].inline_keyboard
        for button in row
    ]
    assert "Delete Skill" in labels


@pytest.mark.asyncio
async def test_handle_legacy_skill_toggle_callback_is_disabled():
    runtime = _runtime()
    query = _Query("skill:toggle:demo:on")

    handled = await runtime_skill_callbacks.handle_skill_callback(
        runtime, query, query.data
    )

    assert handled is True
    assert query.answers[-1]["show_alert"] is True
    assert "legacy Skill action is disabled" in query.answers[-1]["text"]
    assert query.edits == []


@pytest.mark.asyncio
async def test_handle_legacy_skill_run_callback_is_disabled():
    runtime = _runtime()
    query = _Query("skill:run:demo")

    handled = await runtime_skill_callbacks.handle_skill_callback(
        runtime, query, query.data
    )

    assert handled is True
    assert query.answers[-1]["show_alert"] is True
    assert "legacy Skill action is disabled" in query.answers[-1]["text"]
    assert runtime.sent_messages == []


@pytest.mark.asyncio
async def test_handle_removed_cron_skill_callback_redirects_to_jobs():
    runtime = _runtime()
    query = _Query("skill:jobs:cron")

    handled = await runtime_skill_callbacks.handle_skill_callback(
        runtime, query, query.data
    )

    assert handled is True
    assert query.answers[-1]["show_alert"] is True
    assert "/jobs" in query.answers[-1]["text"]
    assert query.edits == []


@pytest.mark.asyncio
async def test_handle_unknown_skill_callback_answers_alert():
    runtime = _runtime()
    query = _Query("skill:show:missing")

    handled = await runtime_skill_callbacks.handle_skill_callback(
        runtime, query, query.data
    )

    assert handled is True
    assert query.answers[-1] == {"text": "Unknown skill", "show_alert": True}
