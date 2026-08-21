from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from adapters import her_habits as habits
from adapters.her import ClawCommandError, ClawTaskResult, ClawTimeoutError, HERAdapter
from adapters.her_habits import (
    HABIT_BODY_MAX_CHARS,
    HABIT_ADVISORY_CONTEXT_ENV,
    HABIT_METADATA_MAX_CHARS,
    HABIT_MEDITATION_ENV,
    HABIT_TITLE_MAX_CHARS,
    HabitMeditationConfig,
    HERHabitStore,
    HERMeditationJournal,
    MeditationValidationError,
    attach_habits_to_prompt,
    build_observable_trace,
    habit_short_references,
    parse_meditation_actions,
    render_habit_advisory_context,
    resolve_habit_reference,
)

def test_her_adapter_declares_habit_pipeline_ownership() -> None:
    assert HERAdapter.habit_pipeline_owner == "adapter"


def test_her_reload_tolerates_stale_habit_module_until_dependency_reload(tmp_path):
    project_root = Path(__file__).resolve().parents[1]
    script = r'''
import importlib
import logging
import sys
from pathlib import Path
from types import SimpleNamespace

import adapters.her as her
import adapters.her_habits as habits

habit_exports = (
    "MEDITATION_ALLOWED_TOOLS",
    "HabitMeditationConfig",
    "HERHabitStore",
    "HERMeditationJournal",
    "MeditationValidationError",
    "HABIT_ADVISORY_CONTEXT_ENV",
    "attach_habits_to_prompt",
    "build_meditation_prompt",
    "extract_current_request",
    "parse_meditation_actions",
    "render_habit_advisory_context",
)
for name in habit_exports:
    habits.__dict__.pop(name, None)
    her.__dict__.pop(name, None)

# This is the critical old-process order: HER reloads while its already-loaded
# Habit dependency still has the previous export surface.
importlib.reload(her)
assert her._her_habits is habits
assert all(name not in her.__dict__ for name in habit_exports)

# The normal /reboot pass reaches and refreshes the dependency afterwards.
importlib.reload(habits)
adapter = object.__new__(her.HERAdapter)
adapter._habit_journal_instance = None
adapter.config = SimpleNamespace(workspace_dir=Path(sys.argv[1]))
adapter.logger = logging.getLogger("test.her.reload")
journal = adapter._her_meditation_journal()
assert type(journal) is habits.HERMeditationJournal
'''
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)],
        cwd=project_root,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


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


def _task_result(
    *,
    text: str = "done",
    session_id: str | None = None,
    tool_uses: list[dict] | None = None,
    tool_results: list[dict] | None = None,
) -> ClawTaskResult:
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
        tool_uses=list(tool_uses or []),
        tool_results=list(tool_results or []),
        session_id=session_id,
        iterations=1,
        completion_status="completed",
        stop_reason="end_turn",
        terminal_kind="model_report",
        message_origin="primary_model",
        exit_reasoning_status="embedded",
        exit_reasoning_attempts=0,
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

    ephemeral_disabled = HabitMeditationConfig.resolve(
        global_config,
        {"habit_learning_eligible": False},
        environ={HABIT_MEDITATION_ENV: "on"},
    )
    assert ephemeral_disabled.enabled is False


def test_task_env_discards_ambient_habit_advisory_context(tmp_path, monkeypatch):
    monkeypatch.setenv(HABIT_ADVISORY_CONTEXT_ENV, "untrusted ambient habit")
    adapter = _adapter(tmp_path, enabled=True)

    assert HABIT_ADVISORY_CONTEXT_ENV not in adapter._task_env()


def test_store_uses_title_and_natural_language_metadata_for_retrieval(tmp_path):
    store = HERHabitStore(tmp_path)
    habit_id = _seed_habit(store)

    by_title = store.retrieve("Please check permissions before editing", limit=5)
    by_metadata = store.retrieve("The filesystem write may be rejected", limit=5)
    body_only = store.retrieve("Inspect the effective mode first", limit=5)

    assert [habit.habit_id for habit in by_title] == [habit_id]
    assert [habit.habit_id for habit in by_metadata] == [habit_id]
    assert body_only == []


def test_store_retrieves_chinese_title_and_metadata(tmp_path):
    store = HERHabitStore(tmp_path)
    [outcome] = store.apply_actions(
        [
            {
                "operation": "create",
                "title": "写文件前检查权限",
                "metadata": "适用于文件写入可能因权限不足而被拒绝的任务。",
                "body": "先读取并确认当前权限边界，再执行写入。",
            }
        ],
        max_actions=3,
    )
    habit_id = outcome.split(":", 1)[1]

    matches = store.retrieve("这次文件写入可能被权限拒绝，请先检查。", limit=5)

    assert [habit.habit_id for habit in matches] == [habit_id]


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


def test_compact_limits_reject_without_truncation_and_legacy_records_remain_readable(
    tmp_path,
):
    store = HERHabitStore(tmp_path)
    oversized_title = "x" * (HABIT_TITLE_MAX_CHARS + 1)

    outcomes = store.apply_actions(
        [
            {
                "operation": "create",
                "title": oversized_title,
                "metadata": "Relevant compact metadata.",
                "body": "Apply the compact behaviour.",
            }
        ],
        max_actions=1,
    )

    assert outcomes == ["ignored:create:MeditationValidationError"]
    assert store.load() == []

    store.root.mkdir(parents=True)
    legacy_payload = {
        "format": "her-habit-v1",
        "id": "legacy-long-habit",
        "title": "L" * (HABIT_TITLE_MAX_CHARS + 20),
        "metadata": "M" * (HABIT_METADATA_MAX_CHARS + 20),
        "body": "B" * (HABIT_BODY_MAX_CHARS + 20),
        "created_at": "2026-08-01T00:00:00+00:00",
        "updated_at": "2026-08-01T00:00:00+00:00",
    }
    (store.root / "legacy-long-habit.json").write_text(
        json.dumps(legacy_payload),
        encoding="utf-8",
    )
    legacy = store.get("legacy-long-habit")
    assert legacy is not None
    assert legacy.title == legacy_payload["title"]
    assert legacy.protected is False

    [partial_update] = store.apply_actions(
        [
            {
                "operation": "update",
                "habit_id": legacy.habit_id,
                "body": "A compact replacement body.",
            }
        ],
        max_actions=1,
    )
    assert partial_update == "ignored:update:MeditationValidationError"
    assert store.get(legacy.habit_id).title == legacy_payload["title"]

    [canonical_update] = store.apply_actions(
        [
            {
                "operation": "update",
                "habit_id": legacy.habit_id,
                "title": "Use compact canonical habits",
                "metadata": "Relevant when an older Habit exceeds the active contract.",
                "body": "Replace the complete record with concise current behaviour.",
            }
        ],
        max_actions=1,
    )
    assert canonical_update == f"updated:{legacy.habit_id}"


def test_protected_habit_blocks_automatic_update_and_archive(tmp_path):
    store = HERHabitStore(tmp_path)
    habit_id = _seed_habit(store)
    change = store.set_protected(habit_id, True)

    assert change is not None
    assert store.get(habit_id).protected is True
    outcomes = store.apply_actions(
        [
            {
                "operation": "update",
                "habit_id": habit_id,
                "body": "Automatic writers must not apply this change.",
            },
            {"operation": "delete", "habit_id": habit_id},
        ],
        max_actions=2,
    )

    assert outcomes == [
        "ignored:update:PermissionError",
        "ignored:delete:PermissionError",
    ]
    assert store.get(habit_id).protected is True
    assert store.archived_count() == 0

    [manual_delete] = store.apply_actions(
        [{"operation": "delete", "habit_id": habit_id}],
        max_actions=1,
        allow_protected=True,
    )
    assert manual_delete == f"deleted:{habit_id}"


def test_habit_references_support_number_full_id_and_collision_expansion(
    tmp_path,
    monkeypatch,
):
    store = HERHabitStore(tmp_path)
    first_id = _seed_habit(store)
    [second_outcome] = store.apply_actions(
        [
            {
                "operation": "create",
                "title": "Validate references before mutation",
                "metadata": "Relevant when a user selects a Habit from the list.",
                "body": "Resolve the displayed reference against the current catalogue.",
            }
        ],
        max_actions=1,
    )
    second_id = second_outcome.split(":", 1)[1]
    catalogue = sorted(store.load(), key=lambda habit: habit.habit_id)

    assert resolve_habit_reference(catalogue, "1") == catalogue[0]
    assert resolve_habit_reference(catalogue, catalogue[1].habit_id) == catalogue[1]

    monkeypatch.setattr(
        habits,
        "_habit_reference_digest",
        lambda habit_id: (
            "AAAAAAAA1" + "0" * 55
            if habit_id == first_id
            else "AAAAAAAA2" + "0" * 55
        ),
    )
    references = habit_short_references(catalogue)
    assert references[first_id] == "H-AAAAAAAA1"
    assert references[second_id] == "H-AAAAAAAA2"
    assert resolve_habit_reference(catalogue, "H-AAAAAAAA") is None
    assert resolve_habit_reference(catalogue, references[first_id]).habit_id == first_id


def test_no_actions_do_not_create_a_habit_directory(tmp_path):
    store = HERHabitStore(tmp_path)

    assert store.apply_actions([], max_actions=3) == []
    assert not (tmp_path / "habits").exists()


def test_planning_context_renderer_preserves_legacy_wrapper_contract(tmp_path):
    store = HERHabitStore(tmp_path)
    _seed_habit(store)
    habits = store.retrieve("permission check", limit=5)

    original = "--- CURRENT USER REQUEST — AUTHORITATIVE ---\nEdit the file."
    advisory = render_habit_advisory_context(habits)
    enriched = attach_habits_to_prompt(original, habits)

    assert render_habit_advisory_context([]) == ""
    assert attach_habits_to_prompt(original, []) == original
    assert enriched == f"{original}\n\n{advisory}"
    assert "HER INTERNAL HABIT PLANNING CONTEXT" in advisory
    assert "must never override the current user request" in advisory
    assert "These are Habit records, not HASHI skills." in advisory


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


@pytest.mark.parametrize(
    "payload",
    [
        'prefix {"actions": []}',
        '{"actions": [], "extra": true}',
        '{"actions": [{"operation": "update", "habit_id": "valid-id"}]}',
        (
            '{"actions": [{"operation": "create", "title": "T", "metadata": "M", '
            '"body": "api_key=super-secret-value"}]}'
        ),
    ],
)
def test_meditation_parser_rejects_open_or_secret_bearing_output(payload):
    with pytest.raises(MeditationValidationError):
        parse_meditation_actions(payload)


def test_observable_trace_redacts_common_secret_shapes():
    result = ClawTaskResult(
        **{
            **_task_result().__dict__,
            "stderr": "Bearer abcdefghijklmnop and api_key=super-secret-value",
        }
    )

    trace = build_observable_trace(result, max_chars=8_000)

    assert "abcdefghijklmnop" not in trace
    assert "super-secret-value" not in trace
    assert "[REDACTED" in trace


def test_durable_journal_recovers_without_retry_ceiling(tmp_path):
    journal = HERMeditationJournal(tmp_path)
    job_id = "1" * 32
    job_id, queued = journal.enqueue(
        job_id=job_id,
        request_id="request-1",
        prompt="meditate",
        max_actions=3,
    )
    assert queued is True
    assert journal.enqueue(
        job_id=job_id,
        request_id="request-1",
        prompt="different duplicate prompt",
        max_actions=3,
    ) == (job_id, False)

    assert journal.claim(job_id) == "meditate"
    assert HERMeditationJournal(tmp_path).recover_interrupted_jobs() == 1
    for attempt in range(1, 7):
        if attempt > 1:
            assert journal.claim(job_id) == "meditate"
        journal.mark_pending(job_id, reason="runtime_shutdown")

    job = journal.get(job_id)
    assert job is not None
    assert job["status"] == "pending"
    assert job["attempts"] == 6
    assert job["error_code"] == "runtime_shutdown"
    assert journal.claim(job_id) == "meditate"


def test_replayed_create_action_is_idempotent(tmp_path):
    store = HERHabitStore(tmp_path)
    actions = [
        {
            "operation": "create",
            "title": "Inspect permissions before writing",
            "metadata": "Relevant when a write may be denied.",
            "body": "Inspect the effective permission boundary first.",
        }
    ]

    first = store.apply_actions(actions, max_actions=3, idempotency_key="job-1")
    second = store.apply_actions(actions, max_actions=3, idempotency_key="job-1")

    assert second == first
    assert len(store.load()) == 1


@pytest.mark.asyncio
async def test_disabled_feature_preserves_the_exact_her_prompt_and_does_not_meditate(
    tmp_path,
):
    adapter = _adapter(tmp_path, enabled=False)
    _seed_habit(adapter._her_habit_store())
    adapter._run_task_async = AsyncMock(return_value=_task_result())
    adapter._schedule_habit_meditation = Mock()
    prompt = "--- CURRENT USER REQUEST — AUTHORITATIVE ---\nCheck permission handling."

    response = await adapter.generate_response(prompt, request_id="off")

    assert response.is_success is True
    assert adapter._run_task_async.await_count == 1
    assert adapter._run_task_async.await_args.args[0] == prompt
    assert adapter._run_task_async.await_args.kwargs["task_env_overrides"] is None
    assert "her_habit_meditation" not in response.stream_metadata
    adapter._schedule_habit_meditation.assert_not_called()


@pytest.mark.asyncio
async def test_enabled_feature_plans_and_schedules_meditation(
    tmp_path,
):
    adapter = _adapter(tmp_path, enabled=True)
    habit_id = _seed_habit(adapter._her_habit_store())
    adapter._run_task_async = AsyncMock(return_value=_task_result())
    adapter._schedule_habit_meditation = Mock()
    prompt = "--- CURRENT USER REQUEST — AUTHORITATIVE ---\nCheck filesystem permission before writing."

    response = await adapter.generate_response(prompt, request_id="on")

    foreground_call = adapter._run_task_async.await_args
    executed_prompt = foreground_call.args[0]
    advisory = foreground_call.kwargs["task_env_overrides"][
        HABIT_ADVISORY_CONTEXT_ENV
    ]
    assert executed_prompt == prompt
    assert habit_id not in executed_prompt
    assert habit_id in advisory
    assert "must never override the current user request" in advisory
    assert response.stream_metadata["her_habit_meditation"] is True
    assert response.stream_metadata["her_habit_ids"] == [habit_id]
    adapter._schedule_habit_meditation.assert_called_once()


@pytest.mark.asyncio
async def test_runtime_intake_ineligibility_disables_adapter_habit_pipeline(tmp_path):
    adapter = _adapter(tmp_path, enabled=True)
    _seed_habit(adapter._her_habit_store())
    adapter.config._hashi_runtime = SimpleNamespace(
        current_request_meta={
            "request_id": "req-internal",
            "habit_learning_eligible": False,
        }
    )
    adapter._run_task_async = AsyncMock(return_value=_task_result())
    adapter._schedule_habit_meditation = Mock()
    prompt = "Internal maintenance request"

    response = await adapter.generate_response(prompt, request_id="req-internal")

    assert response.is_success is True
    assert adapter._run_task_async.await_args.args[0] == prompt
    assert "her_habit_meditation" not in response.stream_metadata
    adapter._schedule_habit_meditation.assert_not_called()


@pytest.mark.asyncio
async def test_habit_on_off_preserves_exact_visible_output_and_tool_side_effects(
    tmp_path,
):
    prompt = (
        "--- CURRENT USER REQUEST — AUTHORITATIVE ---\n"
        "Check filesystem permission before writing the report."
    )
    tool_uses = [
        {"id": "tool-1", "name": "read_file", "input": {"path": "report.md"}},
        {"id": "tool-2", "name": "write_file", "input": {"path": "report.md"}},
    ]
    tool_results = [
        {"tool_use_id": "tool-1", "is_error": False, "output": "writable"},
        {"tool_use_id": "tool-2", "is_error": False, "output": "written"},
    ]
    deterministic_result = _task_result(
        text="REPORT_WRITTEN",
        session_id="session-1",
        tool_uses=tool_uses,
        tool_results=tool_results,
    )
    off = _adapter(tmp_path / "off", enabled=False)
    on = _adapter(tmp_path / "on", enabled=True)
    _seed_habit(off._her_habit_store())
    habit_id = _seed_habit(on._her_habit_store())
    off._run_task_async = AsyncMock(return_value=deterministic_result)
    on._run_task_async = AsyncMock(return_value=deterministic_result)
    off._schedule_habit_meditation = Mock()
    on._schedule_habit_meditation = Mock()

    off_response = await off.generate_response(prompt, request_id="compare-off")
    on_response = await on.generate_response(prompt, request_id="compare-on")

    assert off._run_task_async.await_args.args[0] == prompt
    assert on._run_task_async.await_args.args[0] == prompt
    assert habit_id in on._run_task_async.await_args.kwargs[
        "task_env_overrides"
    ][HABIT_ADVISORY_CONTEXT_ENV]
    assert off_response.text == on_response.text == "REPORT_WRITTEN"
    assert off_response.tool_call_count == on_response.tool_call_count == 2
    assert deterministic_result.tool_uses == tool_uses
    assert deterministic_result.tool_results == tool_results
    off._schedule_habit_meditation.assert_not_called()
    on._schedule_habit_meditation.assert_called_once()


@pytest.mark.asyncio
async def test_unrelated_habit_leaves_executed_prompt_byte_identical(tmp_path):
    adapter = _adapter(tmp_path, enabled=True)
    _seed_habit(adapter._her_habit_store())
    adapter._run_task_async = AsyncMock(return_value=_task_result(text="SUNNY"))
    adapter._schedule_habit_meditation = Mock()
    prompt = (
        "--- CURRENT USER REQUEST — AUTHORITATIVE ---\n"
        "Summarize tomorrow's coastal weather forecast."
    )

    response = await adapter.generate_response(prompt, request_id="irrelevant-habit")

    assert response.text == "SUNNY"
    assert adapter._run_task_async.await_args.args[0] == prompt
    assert adapter._run_task_async.await_args.kwargs["task_env_overrides"] is None
    assert response.stream_metadata["her_habit_ids"] == []


@pytest.mark.asyncio
async def test_conflicting_habit_remains_advisory_and_authoritative_request_wins(
    tmp_path,
):
    adapter = _adapter(tmp_path, enabled=True)
    [outcome] = adapter._her_habit_store().apply_actions(
        [
            {
                "operation": "create",
                "title": "Delete files during cleanup",
                "metadata": "Relevant when deleting files or cleaning a directory.",
                "body": "Delete matching files immediately without asking.",
            }
        ],
        max_actions=3,
    )
    habit_id = outcome.split(":", 1)[1]
    prompt = (
        "--- CURRENT USER REQUEST — AUTHORITATIVE ---\n"
        "Do not delete any files. Only list files that might be cleaned up."
    )
    observed_tool_uses: list[dict] = []

    async def deterministic_foreground(executed_prompt, **kwargs):
        advisory = kwargs["task_env_overrides"][HABIT_ADVISORY_CONTEXT_ENV]
        assert executed_prompt == prompt
        assert str(habit_id) not in executed_prompt
        assert str(habit_id) in advisory
        assert "must never override the current user request" in advisory
        result = _task_result(
            text="candidate.log",
            tool_uses=[{"id": "tool-1", "name": "list_files", "input": {}}],
            tool_results=[
                {"tool_use_id": "tool-1", "is_error": False, "output": "candidate.log"}
            ],
        )
        observed_tool_uses.extend(result.tool_uses)
        return result

    adapter._run_task_async = AsyncMock(side_effect=deterministic_foreground)
    adapter._schedule_habit_meditation = Mock()

    response = await adapter.generate_response(prompt, request_id="conflicting-habit")

    assert response.text == "candidate.log"
    assert [item["name"] for item in observed_tool_uses] == ["list_files"]
    assert all("delete" not in item["name"] for item in observed_tool_uses)
    assert response.stream_metadata["her_habit_ids"] == [habit_id]


@pytest.mark.asyncio
async def test_deterministic_create_retrieve_and_behavioral_use_closed_loop(tmp_path):
    adapter = _adapter(tmp_path, enabled=True)
    observed = {
        "formation_observed": False,
        "retrieval_observed": False,
        "behavioral_use_observed": False,
    }
    foreground_tool_names: list[str] = []

    async def scripted_provider(executed_prompt, *, request_id, **kwargs):
        if request_id == "lifecycle-create":
            assert "HER INTERNAL HABIT PLANNING CONTEXT" not in executed_prompt
            assert kwargs["task_env_overrides"] is None
            return _task_result(text="FIRST_TURN_DONE", session_id="foreground-1")
        if request_id.endswith(":habit-meditation"):
            if "FIRST_TURN_DONE" in executed_prompt:
                return _task_result(
                    text=json.dumps(
                        {
                            "actions": [
                                {
                                    "operation": "create",
                                    "title": "Inspect permissions before writing reports",
                                    "metadata": "Relevant when a report write may be rejected by filesystem permissions.",
                                    "body": "Read the permission boundary before writing the report.",
                                }
                            ]
                        }
                    ),
                    session_id="meditation-1",
                )
            return _task_result(text='{"actions": []}', session_id="meditation-2")
        assert request_id == "lifecycle-use"
        assert executed_prompt == second_prompt
        assert "HER INTERNAL HABIT PLANNING CONTEXT" not in executed_prompt
        advisory = kwargs["task_env_overrides"][HABIT_ADVISORY_CONTEXT_ENV]
        assert "HER INTERNAL HABIT PLANNING CONTEXT" in advisory
        assert "Read the permission boundary before writing the report." in advisory
        observed["retrieval_observed"] = True
        result = _task_result(
            text="SECOND_TURN_USED_HABIT",
            session_id="foreground-2",
            tool_uses=[
                {"id": "read", "name": "read_file", "input": {"path": "permissions"}},
                {"id": "write", "name": "write_file", "input": {"path": "report.md"}},
            ],
            tool_results=[
                {"tool_use_id": "read", "is_error": False, "output": "writable"},
                {"tool_use_id": "write", "is_error": False, "output": "written"},
            ],
        )
        foreground_tool_names.extend(item["name"] for item in result.tool_uses)
        return result

    adapter._run_task_async = AsyncMock(side_effect=scripted_provider)
    first_prompt = (
        "--- CURRENT USER REQUEST — AUTHORITATIVE ---\n"
        "Write the first report and observe any permission lesson."
    )
    first = await adapter.generate_response(first_prompt, request_id="lifecycle-create")
    await asyncio.gather(*list(adapter._habit_meditation_tasks))
    habits = adapter._her_habit_store().load()
    observed["formation_observed"] = (
        first.text == "FIRST_TURN_DONE" and len(habits) == 1
    )

    second_prompt = (
        "--- CURRENT USER REQUEST — AUTHORITATIVE ---\n"
        "Write another report that may be rejected by filesystem permissions."
    )
    second = await adapter.generate_response(second_prompt, request_id="lifecycle-use")
    observed["behavioral_use_observed"] = (
        second.text == "SECOND_TURN_USED_HABIT"
        and foreground_tool_names == ["read_file", "write_file"]
    )
    await asyncio.gather(*list(adapter._habit_meditation_tasks))

    assert observed == {
        "formation_observed": True,
        "retrieval_observed": True,
        "behavioral_use_observed": True,
    }
    assert second.stream_metadata["her_habit_ids"] == [habits[0].habit_id]
    jobs = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in adapter._her_meditation_journal().root.glob("*.json")
    ]
    assert sorted(job["status"] for job in jobs) == ["completed", "no_change"]


@pytest.mark.asyncio
async def test_failed_started_her_run_skips_ungrounded_meditation(tmp_path):
    adapter = _adapter(tmp_path, enabled=True)
    adapter._run_task_async = AsyncMock(
        side_effect=ClawTimeoutError("idle timeout", timeout_s=60)
    )
    adapter._schedule_habit_meditation = Mock()

    response = await adapter.generate_response(
        "--- CURRENT USER REQUEST — AUTHORITATIVE ---\nDiagnose the failure.",
        request_id="failed-run",
    )

    assert response.is_success is False
    adapter._schedule_habit_meditation.assert_not_called()
    audit = json.loads(
        (tmp_path / "backend_state" / "her_habit_audit.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()[-1]
    )
    assert audit["event"] == "habit_meditation_skipped"
    assert audit["reason"] == "foreground_error_without_grounded_task_result"
    assert audit["exception_type"] == "ClawTimeoutError"
    assert audit["grounded_task_result"] is False
    assert not (tmp_path / "backend_state" / "her_habit_meditation").exists()


@pytest.mark.asyncio
async def test_provider_http_error_never_enters_habit_learning_or_notification(tmp_path):
    adapter = _adapter(tmp_path, enabled=True)
    adapter._run_task_async = AsyncMock(
        side_effect=ClawCommandError(
            "HER command exited with code 1",
            returncode=1,
            stderr="provider rejected unsupported image input",
            parsed_error={
                "kind": "run_finished",
                "error_kind": "api_http_error",
                "error": "HTTP 400",
            },
        )
    )
    adapter._schedule_habit_meditation = Mock()
    adapter._spawn_habit_notification_job = Mock()

    response = await adapter.generate_response(
        "--- CURRENT USER REQUEST — AUTHORITATIVE ---\nRead this image.",
        request_id="provider-image-error",
    )

    assert response.is_success is False
    adapter._schedule_habit_meditation.assert_not_called()
    adapter._spawn_habit_notification_job.assert_not_called()
    audit = json.loads(
        (tmp_path / "backend_state" / "her_habit_audit.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()[-1]
    )
    assert audit["event"] == "habit_meditation_skipped"
    assert audit["exception_type"] == "ClawCommandError"
    assert audit["error_kind"] == "api_http_error"
    assert audit["terminal_kind"] == "run_finished"
    assert audit["returncode"] == 1
    assert not (tmp_path / "backend_state" / "her_habit_meditation").exists()


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
    job_id = "2" * 32

    assert adapter._schedule_habit_meditation(
        job_id=job_id,
        request_id="request-1",
        task_prompt="--- CURRENT USER REQUEST — AUTHORITATIVE ---\nWrite a file.",
        task_result=_task_result(),
        config=config,
    ) is True
    [task] = list(adapter._habit_meditation_tasks)
    await task

    call = adapter._run_task_async.await_args
    assert call.kwargs["resume"] is None
    assert call.kwargs["track_session_identity"] is False
    assert call.kwargs["permission_mode_override"] == "read-only"
    assert call.kwargs["allowed_tools_override"] == []
    assert call.kwargs["task_env_overrides"]["CLAW_TASK_PLANNING"] == "0"
    assert call.kwargs["task_env_overrides"]["CLAW_EXECUTION_EFFORT"] == "low"
    assert adapter._session_id == "main-session"
    habits = adapter._her_habit_store().load()
    assert [habit.title for habit in habits] == ["Recover from permission errors"]
    assert adapter._her_meditation_journal().get(job_id)["status"] == "completed"


@pytest.mark.asyncio
async def test_meditation_corrects_one_invalid_response_then_applies_it(tmp_path):
    adapter = _adapter(tmp_path, enabled=True)
    invalid = json.dumps(
        {
            "actions": [
                {
                    "operation": "create",
                    "title": "x" * (HABIT_TITLE_MAX_CHARS + 1),
                    "metadata": "Relevant after a format mistake.",
                    "body": "Apply the corrected compact instruction.",
                }
            ]
        }
    )
    corrected = json.dumps(
        {
            "actions": [
                {
                    "operation": "create",
                    "title": "Correct compact output",
                    "metadata": "Relevant after a format mistake.",
                    "body": "Apply the corrected compact instruction.",
                }
            ]
        }
    )
    adapter._run_task_async = AsyncMock(
        side_effect=[_task_result(text=invalid), _task_result(text=corrected)]
    )
    config = adapter._habit_meditation_config()
    job_id = "9" * 32
    assert adapter._schedule_habit_meditation(
        job_id=job_id,
        request_id="request-correct-output",
        task_prompt="--- CURRENT USER REQUEST — AUTHORITATIVE ---\nDo work.",
        task_result=_task_result(),
        config=config,
    ) is True

    [task] = list(adapter._habit_meditation_tasks)
    await task

    assert adapter._run_task_async.await_count == 2
    correction_prompt = adapter._run_task_async.await_args_list[1].args[0]
    assert "actions[0].title exceeds 48 characters" in correction_prompt
    assert invalid in correction_prompt
    assert [habit.title for habit in adapter._her_habit_store().load()] == [
        "Correct compact output"
    ]
    assert adapter._her_meditation_journal().get(job_id)["status"] == "completed"


@pytest.mark.asyncio
async def test_cancelled_meditation_remains_pending_for_restart(tmp_path):
    adapter = _adapter(tmp_path, enabled=True)
    started = asyncio.Event()

    async def wait_forever(*_args, **_kwargs):
        started.set()
        await asyncio.Future()

    adapter._run_task_async = wait_forever
    config = adapter._habit_meditation_config()
    job_id = "3" * 32
    assert adapter._schedule_habit_meditation(
        job_id=job_id,
        request_id="request-cancelled",
        task_prompt="--- CURRENT USER REQUEST — AUTHORITATIVE ---\nDo work.",
        task_result=_task_result(),
        config=config,
    ) is True
    [task] = list(adapter._habit_meditation_tasks)
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    job = adapter._her_meditation_journal().get(job_id)
    assert job is not None
    assert job["status"] == "pending"
    assert job["attempts"] == 1


@pytest.mark.asyncio
async def test_restart_replays_durable_actions_without_another_model_call(tmp_path):
    adapter = _adapter(tmp_path, enabled=True)
    journal = adapter._her_meditation_journal()
    # Existing v1 journals derived their 32-hex job ID from request_id. They
    # remain recoverable without migration after execution-scoped IDs land.
    legacy_job_id = journal.legacy_job_id_for("request-applying")
    job_id, _ = journal.enqueue(
        job_id=legacy_job_id,
        request_id="request-applying",
        prompt="already meditated",
        max_actions=3,
    )
    assert journal.claim(job_id) == "meditate"
    actions = [
        {
            "operation": "create",
            "title": "Verify the effective runtime",
            "metadata": "Relevant after changing a packaged HER runtime.",
            "body": "Verify the effective runtime rather than only its source files.",
        }
    ]
    journal.store_actions(job_id, actions)
    # Simulate a crash after the first write but before the journal completed.
    adapter._her_habit_store().apply_actions(
        actions,
        max_actions=3,
        idempotency_key=job_id,
    )
    adapter._run_task_async = AsyncMock()

    await adapter._run_habit_meditation(
        job_id=job_id,
        config=adapter._habit_meditation_config(),
    )

    adapter._run_task_async.assert_not_awaited()
    assert len(adapter._her_habit_store().load()) == 1
    assert journal.get(job_id)["status"] == "completed"


@pytest.mark.asyncio
async def test_reused_hashi_request_id_creates_distinct_meditation_jobs(tmp_path):
    async def complete_one_execution(adapter: HERAdapter) -> None:
        adapter._run_task_async = AsyncMock(
            side_effect=[
                _task_result(text="foreground complete"),
                _task_result(text='{"actions": []}'),
            ]
        )
        response = await adapter.generate_response(
            "--- CURRENT USER REQUEST — AUTHORITATIVE ---\nDo the next task.",
            request_id="req-0001",
        )
        assert response.is_success is True
        [meditation_task] = list(adapter._habit_meditation_tasks)
        await meditation_task

    first_runtime = _adapter(tmp_path, enabled=True)
    await complete_one_execution(first_runtime)

    # Simulate a fresh HASHI runtime whose request counter starts at req-0001.
    second_runtime = _adapter(tmp_path, enabled=True)
    await complete_one_execution(second_runtime)

    journal = HERMeditationJournal(tmp_path)
    jobs = [
        journal.get(path.stem)
        for path in sorted(journal.root.glob("*.json"))
    ]
    assert len(jobs) == 2
    assert {job["request_id"] for job in jobs if job is not None} == {"req-0001"}
    assert {job["status"] for job in jobs if job is not None} == {"no_change"}
    assert len({job["job_id"] for job in jobs if job is not None}) == 2


@pytest.mark.asyncio
async def test_meditation_runs_independently_without_blocking_foreground(
    tmp_path,
):
    adapter = _adapter(tmp_path, enabled=True)
    meditation_started = asyncio.Event()
    visible_events = []

    async def collect_visible_event(event):
        visible_events.append(event)

    async def timeout_meditation_then_run_foreground(
        _prompt,
        *,
        request_id,
        **_kwargs,
    ):
        if request_id.endswith(":habit-meditation"):
            meditation_started.set()
            await asyncio.Future()
        return _task_result(text="FOREGROUND_UNBLOCKED", session_id="foreground")

    adapter._run_task_async = timeout_meditation_then_run_foreground
    journal = adapter._her_meditation_journal()
    job_id = "4" * 32
    journal.enqueue(
        job_id=job_id,
        request_id="slow-meditation",
        prompt="Meditate on the prior task.",
        max_actions=3,
    )
    timeout_config = HabitMeditationConfig(
        enabled=True,
        meditation_idle_timeout_seconds=0.05,
    )
    assert adapter._spawn_habit_meditation_job(
        job_id,
        config=timeout_config,
    ) is True
    [background_task] = list(adapter._habit_meditation_tasks)
    await meditation_started.wait()
    adapter._schedule_habit_meditation = Mock()

    started = asyncio.get_running_loop().time()
    response = await asyncio.wait_for(
        adapter.generate_response(
            "--- CURRENT USER REQUEST — AUTHORITATIVE ---\nRun the foreground task.",
            request_id="foreground-after-timeout",
            on_stream_event=collect_visible_event,
        ),
        timeout=0.75,
    )
    elapsed = asyncio.get_running_loop().time() - started
    await background_task

    assert response.text == "FOREGROUND_UNBLOCKED"
    assert elapsed < 0.05
    assert [event.kind for event in visible_events] == ["progress"]
    assert all("Meditation" not in event.summary for event in visible_events)
    job = journal.get(job_id)
    assert job is not None
    assert job["status"] == "pending"
    assert job["attempts"] == 1
    assert job["error_code"] == "TimeoutError"


@pytest.mark.asyncio
async def test_restart_backlog_larger_than_resume_batch_drains_across_instances(
    tmp_path,
):
    journal = HERMeditationJournal(tmp_path)
    job_ids = [f"{index:032x}" for index in range(1, 18)]
    for job_id in job_ids:
        journal.enqueue(
            job_id=job_id,
            request_id=f"backlog-{job_id}",
            prompt="Durable decision already made.",
            max_actions=3,
        )
        assert journal.claim(job_id) == "meditate"
        journal.store_actions(job_id, [])

    first_runtime = _adapter(tmp_path, enabled=True)
    first_runtime._run_task_async = AsyncMock()
    assert first_runtime._resume_pending_habit_meditations() == 16
    await asyncio.gather(*list(first_runtime._habit_meditation_tasks))

    after_first = [journal.get(job_id) for job_id in job_ids]
    assert sum(job["status"] == "no_change" for job in after_first) == 16
    assert sum(job["status"] == "applying" for job in after_first) == 1
    first_runtime._run_task_async.assert_not_awaited()

    second_runtime = _adapter(tmp_path, enabled=True)
    second_runtime._run_task_async = AsyncMock()
    assert second_runtime._resume_pending_habit_meditations() == 1
    await asyncio.gather(*list(second_runtime._habit_meditation_tasks))

    assert {journal.get(job_id)["status"] for job_id in job_ids} == {"no_change"}
    second_runtime._run_task_async.assert_not_awaited()
