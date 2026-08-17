from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime
from orchestrator.legacy.bridge_agent_runtime import BridgeAgentRuntime
from orchestrator.skill_manager import SkillDefinition


@pytest.mark.asyncio
async def test_legacy_underscore_action_id_routes_to_jobs_automation():
    invoke_automation = AsyncMock(return_value=(True, "automation done"))
    runtime = SimpleNamespace(invoke_scheduler_automation=invoke_automation)

    result = await FlexibleAgentRuntime.invoke_scheduler_skill(
        runtime,
        skill_id="memory_consolidation",
        args="run",
        task_id="nightly-memory",
    )

    assert result == (True, "automation done")
    invoke_automation.assert_awaited_once_with(
        automation_id="memory_consolidation",
        args="run",
        task_id="nightly-memory",
    )


@pytest.mark.asyncio
async def test_scheduled_prompt_skill_retains_agent_current_backend(tmp_path):
    skill = SkillDefinition(
        id="legacy-pinned-skill",
        name="Legacy pinned skill",
        description="",
        body="Use the owning Agent runtime.",
        skill_dir=Path(tmp_path),
    )
    manager = SimpleNamespace(
        get_skill=lambda _skill_id: skill,
        build_prompt_for_skill=lambda _skill, args: f"skill prompt: {args}",
    )
    runtime = SimpleNamespace(
        skill_manager=manager,
        config=SimpleNamespace(active_backend="her", access_scope="drive"),
        logger=SimpleNamespace(info=Mock()),
        error_logger=SimpleNamespace(error=Mock()),
        enqueue_request=AsyncMock(return_value="req-1"),
        get_current_model=lambda: "deepseek/deepseek-v4-flash",
        _primary_chat_id=lambda: 123,
    )

    ok, message = await FlexibleAgentRuntime.invoke_scheduler_skill(
        runtime,
        skill_id=skill.id,
        args="now",
        task_id="cron-1",
    )

    assert ok is True
    assert message == "Scheduled prompt skill queued: legacy-pinned-skill"
    assert runtime.config.active_backend == "her"
    runtime.enqueue_request.assert_awaited_once_with(
        chat_id=123,
        prompt="skill prompt: now",
        source="scheduler-skill",
        summary="Skill Task [cron-1]",
        silent=False,
    )


@pytest.mark.asyncio
async def test_legacy_scheduled_prompt_skill_retains_agent_current_backend(tmp_path):
    skill = SkillDefinition(
        id="legacy-pinned-skill",
        name="Legacy pinned skill",
        description="",
        body="Use the owning Agent runtime.",
        skill_dir=Path(tmp_path),
    )
    manager = SimpleNamespace(
        get_skill=lambda _skill_id: skill,
        build_prompt_for_skill=lambda _skill, args: f"skill prompt: {args}",
    )
    runtime = SimpleNamespace(
        skill_manager=manager,
        config=SimpleNamespace(
            engine="her",
            model="deepseek/deepseek-v4-flash",
            workspace_dir=tmp_path,
        ),
        global_config=SimpleNamespace(authorized_id=123),
        name="sunny",
        logger=SimpleNamespace(info=Mock()),
        error_logger=SimpleNamespace(error=Mock()),
        enqueue_request=AsyncMock(return_value="req-1"),
    )

    ok, message = await BridgeAgentRuntime.invoke_scheduler_skill(
        runtime,
        skill_id=skill.id,
        args="now",
        task_id="cron-1",
    )

    assert ok is True
    assert message == "Scheduled prompt skill queued: legacy-pinned-skill"
    assert runtime.config.engine == "her"
    runtime.enqueue_request.assert_awaited_once_with(
        chat_id=123,
        prompt="skill prompt: now",
        source="scheduler-skill",
        summary="Skill Task [cron-1]",
        silent=False,
    )
