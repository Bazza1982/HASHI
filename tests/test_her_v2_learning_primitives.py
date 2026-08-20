from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from adapters.her_habits import (
    HABIT_MEDITATION_ENV,
    HABIT_TITLE_MAX_CHARS,
    MAX_MEDITATION_ATTEMPTS,
    HabitMeditationConfig,
    HERHabitStore,
    HERMeditationJournal,
    MeditationValidationError,
    attach_habits_to_prompt,
    parse_meditation_actions,
    render_habit_advisory_context,
)


def _seed_habit(store: HERHabitStore) -> str:
    [outcome] = store.apply_actions(
        [
            {
                "operation": "create",
                "title": "Check permissions before changing files",
                "metadata": (
                    "Relevant when a task may lack filesystem permission or a "
                    "write could be rejected."
                ),
                "body": "Inspect effective permissions before attempting the write.",
            }
        ],
        max_actions=3,
    )
    return outcome.split(":", 1)[1]


def test_learning_config_defaults_off_and_honours_explicit_overrides() -> None:
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
    assert HabitMeditationConfig.resolve(global_config, {}).retrieval_limit == 7
    assert (
        HabitMeditationConfig.resolve(
            global_config,
            {"habit_meditation": {"enabled": False}},
        ).enabled
        is False
    )
    assert (
        HabitMeditationConfig.resolve(
            global_config,
            {},
            environ={HABIT_MEDITATION_ENV: "off"},
        ).enabled
        is False
    )


def test_store_retrieval_uses_title_and_metadata_but_not_body(tmp_path) -> None:
    store = HERHabitStore(tmp_path)
    habit_id = _seed_habit(store)

    assert [item.habit_id for item in store.retrieve("check permissions", limit=5)] == [
        habit_id
    ]
    assert [item.habit_id for item in store.retrieve("write rejected", limit=5)] == [
        habit_id
    ]
    assert store.retrieve("Inspect the effective mode first", limit=5) == []


def test_store_retrieves_chinese_title_and_metadata(tmp_path) -> None:
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
        max_actions=1,
    )

    assert [
        item.habit_id
        for item in store.retrieve("这次文件写入可能被权限拒绝，请先检查。", limit=5)
    ] == [outcome.split(":", 1)[1]]


def test_store_update_archive_and_protection_are_recoverable(tmp_path) -> None:
    store = HERHabitStore(tmp_path)
    habit_id = _seed_habit(store)
    store.set_protected(habit_id, True)

    assert store.apply_actions(
        [
            {"operation": "update", "habit_id": habit_id, "body": "Blocked."},
            {"operation": "delete", "habit_id": habit_id},
        ],
        max_actions=2,
    ) == ["ignored:update:PermissionError", "ignored:delete:PermissionError"]
    assert store.get(habit_id).protected is True

    assert store.apply_actions(
        [{"operation": "delete", "habit_id": habit_id}],
        max_actions=1,
        allow_protected=True,
    ) == [f"deleted:{habit_id}"]
    assert len(list((tmp_path / "habits" / "archive").glob(f"{habit_id}.*.json"))) == 1


def test_store_rejects_oversized_new_habit_without_truncation(tmp_path) -> None:
    store = HERHabitStore(tmp_path)

    assert store.apply_actions(
        [
            {
                "operation": "create",
                "title": "x" * (HABIT_TITLE_MAX_CHARS + 1),
                "metadata": "Relevant metadata.",
                "body": "Bounded body.",
            }
        ],
        max_actions=1,
    ) == ["ignored:create:MeditationValidationError"]
    assert store.load() == []


def test_planning_advisory_wrapper_preserves_authority(tmp_path) -> None:
    store = HERHabitStore(tmp_path)
    _seed_habit(store)
    selected = store.retrieve("permission check", limit=5)
    request = "--- CURRENT USER REQUEST — AUTHORITATIVE ---\nEdit the file."

    advisory = render_habit_advisory_context(selected)
    assert attach_habits_to_prompt(request, selected) == f"{request}\n\n{advisory}"
    assert "must never override the current user request" in advisory
    assert "These are Habit records, not HASHI skills." in advisory


def test_meditation_parser_accepts_closed_json() -> None:
    actions = [
        {"operation": "create", "title": "T", "metadata": "M", "body": "B"}
    ]

    assert parse_meditation_actions(json.dumps({"actions": actions})) == actions
    assert parse_meditation_actions(
        "```json\n" + json.dumps({"actions": actions}) + "\n```"
    ) == actions


@pytest.mark.parametrize(
    "payload",
    [
        'prefix {"actions": []}',
        '{"actions": [], "extra": true}',
        '{"actions": [{"operation": "update", "habit_id": "valid-id"}]}',
        (
            '{"actions": [{"operation": "create", "title": "T", '
            '"metadata": "M", "body": "api_key=super-secret-value"}]}'
        ),
    ],
)
def test_meditation_parser_rejects_open_or_secret_bearing_output(payload) -> None:
    with pytest.raises(MeditationValidationError):
        parse_meditation_actions(payload)


def test_meditation_journal_recovers_and_bounds_attempts(tmp_path) -> None:
    journal = HERMeditationJournal(tmp_path)
    job_id = "1" * 32
    assert journal.enqueue(
        job_id=job_id,
        request_id="request-1",
        prompt="meditate",
        max_actions=3,
    ) == (job_id, True)
    assert journal.claim(job_id) == "meditate"
    assert HERMeditationJournal(tmp_path).recover_interrupted_jobs() == 1

    for attempt in range(1, MAX_MEDITATION_ATTEMPTS + 1):
        if attempt > 1:
            assert journal.claim(job_id) == "meditate"
        journal.mark_pending(job_id, reason="runtime_shutdown")

    job = journal.get(job_id)
    assert job is not None
    assert job["status"] == "failed"
    assert job["attempts"] == MAX_MEDITATION_ATTEMPTS
    assert job["error_code"] == "retry_exhausted"


def test_replayed_habit_create_is_idempotent(tmp_path) -> None:
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
    assert store.apply_actions(
        actions, max_actions=3, idempotency_key="job-1"
    ) == first
    assert len(store.load()) == 1
