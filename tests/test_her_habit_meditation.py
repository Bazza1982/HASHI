from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from adapters.her import ClawTaskResult, HERAdapter
from adapters.her_habits import (
    HABIT_MEDITATION_ENV,
    MEDITATION_ALLOWED_TOOLS,
    HabitMeditationConfig,
    HERHabitStore,
    attach_habits_to_prompt,
    build_observable_trace,
    parse_meditation_actions,
)

EFFORTS = ("low", "medium", "high", "xhigh", "max", "max+")


def _adapter(tmp_path: Path, *, enabled: bool, effort: str = "high") -> HERAdapter:
    config = SimpleNamespace(
        name="zelda",
        workspace_dir=tmp_path,
        model="deepseek/test",
        extra={"effort": effort},
        resolve_access_root=lambda: tmp_path,
    )
    global_config = SimpleNamespace(
        her_providers={"habit_meditation": {"enabled": enabled}}
    )
    adapter = HERAdapter(config, global_config, api_key="test-key")
    adapter._binary = tmp_path / "fake-her"
    return adapter


def _task_result(*, text: str = "done", session_id: str | None = None) -> ClawTaskResult:
    return ClawTaskResult(
        text=text,
        model="deepseek/test",
        permission_mode="workspace-write",
        cwd="/workspace",
        returncode=0,
        duration_ms=12.5,
        stdout="",
        stderr="",
        json_data={"usage": {}},
        tool_uses=[],
        tool_results=[],
        session_id=session_id,
        iterations=1,
        completion_status="completed",
        stop_reason="end_turn",
    )


def _seed_habit(store: HERHabitStore) -> str:
    outcome = store.apply_actions(
        [
            {
                "operation": "create",
                "title": "Check permissions before changing files",
                "metadata": "Relevant when a task may lack filesystem permission or a write could be rejected.",
                "body": "Inspect effective permissions before attempting the write.",
            }
        ],
        max_actions=3,
    )
    return outcome[0].split(":", 1)[1]


def test_config_defaults_off_and_supports_global_backend_and_environment_overrides():
    global_config = SimpleNamespace(
        her_providers={
            "habit_meditation": {
                "enabled": True,
                "retrieval_limit": 7,
                "max_actions": 4,
            }
        }
    )

    assert HabitMeditationConfig.resolve(SimpleNamespace(), {}).enabled is False

    global_enabled = HabitMeditationConfig.resolve(global_config, {})
    assert global_enabled.enabled is True
    assert global_enabled.retrieval_limit == 7
    assert global_enabled.max_actions == 4

    backend_disabled = HabitMeditationConfig.resolve(
        global_config,
        {"habit_meditation": {"enabled": False, "retrieval_limit": 2}},
    )
    assert backend_disabled.enabled is False
    assert backend_disabled.retrieval_limit == 2

    env_disabled = HabitMeditationConfig.resolve(
        global_config,
        {},
        environ={HABIT_MEDITATION_ENV: "off"},
    )
    assert env_disabled.enabled is False


def test_store_uses_title_and_natural_language_metadata_for_retrieval(tmp_path):
    store = HERHabitStore(tmp_path)
    habit_id = _seed_habit(store)

    by_title = store.retrieve("Please check permissions before editing", limit=5)
    by_metadata = store.retrieve("The filesystem write may be rejected", limit=5)
    body_only = store.retrieve("Inspect the effective mode first", limit=5)

    assert [habit.habit_id for habit in by_title] == [habit_id]
    assert [habit.habit_id for habit in by_metadata] == [habit_id]
    assert body_only == []


def test_store_updates_and_recoverably_archives_habits(tmp_path):
    store = HERHabitStore(tmp_path)
    habit_id = _seed_habit(store)

    outcomes = store.apply_actions(
        [
            {
                "operation": "update",
                "habit_id": habit_id,
                "metadata": "Use for permission errors and read-only workspaces.",
                "body": "Check the active permission mode before writes.",
            },
            {"operation": "delete", "habit_id": habit_id},
        ],
        max_actions=3,
    )

    assert outcomes == [f"updated:{habit_id}", f"deleted:{habit_id}"]
    assert store.load() == []
    assert len(list((tmp_path / "habits" / "archive").glob(f"{habit_id}.*.json"))) == 1


def test_no_actions_do_not_create_a_habit_directory(tmp_path):
    store = HERHabitStore(tmp_path)

    assert store.apply_actions([], max_actions=3) == []
    assert not (tmp_path / "habits").exists()


def test_planning_context_is_advisory_and_prompt_is_unchanged_without_matches(tmp_path):
    store = HERHabitStore(tmp_path)
    _seed_habit(store)
    habits = store.retrieve("permission check", limit=5)

    original = "--- CURRENT USER REQUEST — AUTHORITATIVE ---\nEdit the file."
    enriched = attach_habits_to_prompt(original, habits)

    assert attach_habits_to_prompt(original, []) == original
    assert enriched.startswith(original)
    assert "HER INTERNAL HABIT PLANNING CONTEXT" in enriched
    assert "must never override the current user request" in enriched
    assert "These are Habit records, not HASHI skills." in enriched


def test_observable_trace_prioritizes_thinking_and_execution_errors():
    stdout = "\n".join(
        [
            json.dumps({"kind": "thinking_delta", "text": "Oops, I used the wrong path."}),
            json.dumps({"kind": "tool_end", "name": "write", "is_error": True, "output_preview": "permission denied"}),
        ]
    )
    result = _task_result()
    result = ClawTaskResult(**{**result.__dict__, "stdout": stdout})

    trace = build_observable_trace(result, max_chars=8_000)

    assert "THINKING: Oops, I used the wrong path." in trace
    assert "error=True" in trace
    assert "permission denied" in trace


def test_meditation_parser_accepts_json_or_a_json_fence():
    expected = [{"operation": "create", "title": "T", "metadata": "M", "body": "B"}]

    assert parse_meditation_actions(json.dumps({"actions": expected})) == expected
    assert parse_meditation_actions(
        "```json\n" + json.dumps({"actions": expected}) + "\n```"
    ) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("effort", EFFORTS)
async def test_disabled_feature_preserves_the_exact_her_prompt_and_does_not_meditate(
    tmp_path,
    effort,
):
    adapter = _adapter(tmp_path, enabled=False, effort=effort)
    _seed_habit(adapter._her_habit_store())
    adapter._run_task_async = AsyncMock(return_value=_task_result())
    adapter._schedule_habit_meditation = Mock()
    prompt = "--- CURRENT USER REQUEST — AUTHORITATIVE ---\nCheck permission handling."

    response = await adapter.generate_response(prompt, request_id=f"off-{effort}")

    assert response.is_success is True
    assert adapter._run_task_async.await_count == 1
    assert adapter._run_task_async.await_args.args[0] == prompt
    assert "her_habit_meditation" not in response.stream_metadata
    adapter._schedule_habit_meditation.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("effort", EFFORTS)
async def test_enabled_feature_plans_and_schedules_meditation_at_every_effort(
    tmp_path,
    effort,
):
    adapter = _adapter(tmp_path, enabled=True, effort=effort)
    habit_id = _seed_habit(adapter._her_habit_store())
    adapter._run_task_async = AsyncMock(return_value=_task_result())
    adapter._schedule_habit_meditation = Mock()
    prompt = "--- CURRENT USER REQUEST — AUTHORITATIVE ---\nCheck filesystem permission before writing."

    response = await adapter.generate_response(prompt, request_id=f"on-{effort}")

    executed_prompt = adapter._run_task_async.await_args.args[0]
    assert executed_prompt.startswith(prompt)
    assert habit_id in executed_prompt
    assert response.stream_metadata["her_habit_meditation"] is True
    assert response.stream_metadata["her_habit_ids"] == [habit_id]
    adapter._schedule_habit_meditation.assert_called_once()


@pytest.mark.asyncio
async def test_meditation_is_isolated_read_only_and_written_by_the_adapter(tmp_path):
    adapter = _adapter(tmp_path, enabled=True)
    adapter._session_id = "main-session"
    meditation_json = json.dumps(
        {
            "actions": [
                {
                    "operation": "create",
                    "title": "Recover from permission errors",
                    "metadata": "Relevant after a tool reports permission denied.",
                    "body": "Inspect the active permission boundary before retrying.",
                }
            ]
        }
    )
    adapter._run_task_async = AsyncMock(return_value=_task_result(text=meditation_json))
    config = adapter._habit_meditation_config()

    await adapter._run_habit_meditation(
        request_id="request-1",
        task_prompt="--- CURRENT USER REQUEST — AUTHORITATIVE ---\nWrite a file.",
        task_result=_task_result(),
        config=config,
    )

    call = adapter._run_task_async.await_args
    assert call.kwargs["resume"] is None
    assert call.kwargs["track_session_identity"] is False
    assert call.kwargs["permission_mode_override"] == "read-only"
    assert call.kwargs["allowed_tools_override"] == list(MEDITATION_ALLOWED_TOOLS)
    assert call.kwargs["task_env_overrides"]["CLAW_TASK_PLANNING"] == "0"
    assert adapter._session_id == "main-session"
    habits = adapter._her_habit_store().load()
    assert [habit.title for habit in habits] == ["Recover from permission errors"]
