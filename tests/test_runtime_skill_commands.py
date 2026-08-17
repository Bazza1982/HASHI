from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from orchestrator.runtime_skill_callbacks import (
    build_skill_action_keyboard,
    build_skill_catalog_keyboard,
)
from orchestrator.runtime_skill_commands import handle_standard_skill_command
from orchestrator.skill_manager import SkillManager


def _write_skill(
    root: Path, skill_id: str, description: str = "Use when testing."
) -> None:
    directory = root / "skills" / skill_id
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text(
        f"---\nname: {skill_id}\ndescription: {description}\n---\n\n# Instructions\n",
        encoding="utf-8",
    )


def _runtime(tmp_path: Path, manager: SkillManager):
    workspace = tmp_path / "workspaces" / "momo"
    runtime = SimpleNamespace(
        name="momo",
        workspace_dir=workspace,
        skill_manager=manager,
        enqueue_request=AsyncMock(return_value="req-1"),
    )
    runtime._skill_keyboard = lambda: build_skill_catalog_keyboard(manager, workspace)
    runtime._skill_action_keyboard = lambda skill: build_skill_action_keyboard(
        manager,
        workspace,
        skill,
    )
    return runtime


def _reply_collector():
    replies = []

    async def reply(text: str, **kwargs):
        replies.append({"text": text, **kwargs})

    return replies, reply


@pytest.mark.asyncio
async def test_skill_command_catalog_and_search_show_management_state(tmp_path: Path):
    _write_skill(tmp_path, "alpha-skill", "Use when handling alpha work.")
    manager = SkillManager(tmp_path, tmp_path / "tasks.json")
    runtime = _runtime(tmp_path, manager)
    update = SimpleNamespace(effective_chat=SimpleNamespace(id=123))
    replies, reply = _reply_collector()

    await handle_standard_skill_command(runtime, update, [], reply)
    await handle_standard_skill_command(runtime, update, ["find", "alpha"], reply)

    assert "1/1" in replies[0]["text"]
    assert "Invalid</b> · <code>0" in replies[0]["text"]
    assert "alpha-skill" in replies[1]["text"]
    labels = [
        button.text
        for row in replies[0]["reply_markup"].inline_keyboard
        for button in row
    ]
    assert "➕ Install" in labels
    assert "↻ Refresh" in labels
    assert "⚠️ Invalid 0" in labels


@pytest.mark.asyncio
async def test_disable_requires_force_for_enabled_job_and_blocks_execution(
    tmp_path: Path,
):
    _write_skill(tmp_path, "scheduled-skill")
    tasks_path = tmp_path / "tasks.json"
    tasks_path.write_text(
        json.dumps(
            {
                "version": 1,
                "heartbeats": [],
                "nudges": [],
                "crons": [
                    {
                        "id": "scheduled-nightly",
                        "agent": "momo",
                        "enabled": True,
                        "action": "skill:scheduled-skill",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    manager = SkillManager(tmp_path, tasks_path)
    runtime = _runtime(tmp_path, manager)
    update = SimpleNamespace(effective_chat=SimpleNamespace(id=123))
    replies, reply = _reply_collector()

    await handle_standard_skill_command(
        runtime,
        update,
        ["disable", "scheduled-skill"],
        reply,
    )

    assert "--force" in replies[-1]["text"]
    assert manager.is_skill_enabled(runtime.workspace_dir, "scheduled-skill") is True

    await handle_standard_skill_command(
        runtime,
        update,
        ["disable", "scheduled-skill", "--force"],
        reply,
    )
    await handle_standard_skill_command(
        runtime,
        update,
        ["scheduled-skill", "do", "the", "work"],
        reply,
    )

    assert manager.is_skill_enabled(runtime.workspace_dir, "scheduled-skill") is False
    assert "is disabled" in replies[-1]["text"]
    runtime.enqueue_request.assert_not_awaited()


def test_skill_keyboards_use_short_callbacks_and_show_delete_for_project_packages(
    tmp_path: Path,
):
    skill_id = "a" * 64
    _write_skill(tmp_path, skill_id)
    manager = SkillManager(tmp_path, tmp_path / "tasks.json")
    workspace = tmp_path / "workspaces" / "momo"
    skill = manager.get_skill(skill_id)
    assert skill is not None

    catalog = build_skill_catalog_keyboard(manager, workspace)
    detail = build_skill_action_keyboard(manager, workspace, skill)
    callbacks = [
        button.callback_data
        for markup in (catalog, detail)
        for row in markup.inline_keyboard
        for button in row
    ]
    labels = [button.text for row in detail.inline_keyboard for button in row]

    assert callbacks
    assert all(len(callback.encode("utf-8")) <= 64 for callback in callbacks)
    assert "⏸ Disable" in labels
    assert "🗑️ Delete" in labels


@pytest.mark.asyncio
async def test_skill_command_attributes_usage_to_the_queued_skill(tmp_path: Path):
    _write_skill(tmp_path, "measured-skill")
    manager = SkillManager(tmp_path, tmp_path / "tasks.json")
    runtime = _runtime(tmp_path, manager)
    update = SimpleNamespace(effective_chat=SimpleNamespace(id=123))
    _replies, reply = _reply_collector()

    await handle_standard_skill_command(
        runtime,
        update,
        ["measured-skill", "do", "the", "work"],
        reply,
    )

    runtime.enqueue_request.assert_awaited_once()
    assert runtime.enqueue_request.await_args.kwargs["skill_id"] == "measured-skill"
