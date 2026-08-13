from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from adapters.her import HERAdapter
from adapters.her_habits import HERHabitStore, HERMeditationJournal
from orchestrator import runtime_her_habits
from orchestrator.command_specs import COMMAND_SPEC_BY_NAME, SENSITIVE_COMMAND_NAMES
from orchestrator.config import FlexibleAgentConfig, GlobalConfig
from orchestrator.flexible_backend_manager import FlexibleBackendManager
from orchestrator.slash_command_audit import redact_args


class FakeBackendManager:
    def __init__(self, adapter: Any):
        self.current_backend = adapter
        self.override: bool | None = None

    def get_habit_meditation_override(self) -> bool | None:
        return self.override

    def set_habit_meditation_override(self, enabled: bool | None) -> None:
        self.override = enabled
        extra = self.current_backend.config.extra
        if enabled is None:
            extra.pop("habit_meditation_enabled", None)
        else:
            extra["habit_meditation_enabled"] = enabled


class FakeRuntime:
    def __init__(self, workspace: Path, *, engine: str = "her"):
        self.name = "zelda"
        self.workspace_dir = workspace
        self.logger = logging.getLogger(f"test.habit.{engine}")
        self.config = SimpleNamespace(active_backend=engine)
        self.global_config = SimpleNamespace(
            her_providers={"habit_meditation": {"enabled": True}},
        )
        if engine == "her":
            adapter_config = SimpleNamespace(
                name=self.name,
                workspace_dir=workspace,
                model="test-model",
                engine="her",
                extra={},
                resolve_access_root=lambda: workspace,
            )
            adapter = HERAdapter(adapter_config, self.global_config, api_key="test")
        else:
            adapter = SimpleNamespace(config=SimpleNamespace(engine=engine))
        self.backend_manager = FakeBackendManager(adapter)
        self.replies: list[dict[str, Any]] = []
        self.long_messages: list[tuple[int, str, dict[str, Any]]] = []
        self.sent_messages: list[tuple[int, str, dict[str, Any]]] = []
        self.telegram_connected = True
        self.is_generating = False
        self.queue = SimpleNamespace(empty=lambda: True)

    def _is_authorized_user(self, user_id: int | None) -> bool:
        return user_id == 1

    def _backend_busy(self) -> bool:
        return False

    async def _reply_text(self, update: Any, text: str, **kwargs: Any) -> None:
        self.replies.append({"text": text, **kwargs})

    async def send_long_message(self, chat_id: int, text: str, **kwargs: Any) -> None:
        self.long_messages.append((chat_id, text, kwargs))

    async def _send_text(self, chat_id: int, text: str, **kwargs: Any) -> object:
        self.sent_messages.append((chat_id, text, kwargs))
        return object()

    async def _deliver_her_habit_notification(self, job: dict[str, Any]) -> bool | None:
        return await runtime_her_habits.deliver_habit_notification(self, job)


class FakeQuery:
    def __init__(self, data: str):
        self.data = data
        self.message = SimpleNamespace(
            chat_id=99,
            chat=SimpleNamespace(id=99),
        )
        self.edits: list[dict[str, Any]] = []
        self.answers: list[tuple[str | None, bool]] = []

    async def edit_message_text(self, text: str, **kwargs: Any) -> None:
        self.edits.append({"text": text, **kwargs})

    async def answer(self, text: str | None = None, show_alert: bool = False) -> None:
        self.answers.append((text, show_alert))


def _command_update() -> SimpleNamespace:
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=1),
        effective_chat=SimpleNamespace(id=99),
        message=SimpleNamespace(chat_id=99),
        callback_query=None,
    )


def _callback_update(data: str) -> tuple[SimpleNamespace, FakeQuery]:
    query = FakeQuery(data)
    return (
        SimpleNamespace(
            effective_user=SimpleNamespace(id=1),
            effective_chat=SimpleNamespace(id=99),
            callback_query=query,
        ),
        query,
    )


def _callback_data(markup: Any, prefix: str) -> str:
    for row in markup.inline_keyboard:
        for button in row:
            if str(button.callback_data or "").startswith(prefix):
                return str(button.callback_data)
    raise AssertionError(f"callback starting with {prefix!r} was not found")


def _seed_habit(store: HERHabitStore, title: str) -> str:
    [outcome] = store.apply_actions(
        [
            {
                "operation": "create",
                "title": title,
                "metadata": f"Use when {title.casefold()} is relevant.",
                "body": f"Apply the complete behaviour for {title}.",
            }
        ],
        max_actions=1,
    )
    return outcome.split(":", 1)[1]


@pytest.mark.asyncio
async def test_non_her_command_does_not_read_habits_and_offers_switch(
    tmp_path,
    monkeypatch,
):
    runtime = FakeRuntime(tmp_path, engine="codex-cli")

    def forbidden_store_read(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("non-HER command must not inspect dormant Habit state")

    monkeypatch.setattr(runtime_her_habits, "_habit_store", forbidden_store_read)

    await runtime_her_habits.cmd_habit(
        runtime,
        _command_update(),
        SimpleNamespace(args=["delete", "debug-habit-42"]),
    )

    reply = runtime.replies[-1]
    assert "available only" in reply["text"]
    assert "No dormant HER Habit data was read" in reply["text"]
    assert (
        _callback_data(reply["reply_markup"], "backend:her:plain")
        == "backend:her:plain"
    )
    audit = [
        json.loads(line)
        for line in (tmp_path / "backend_state" / "her_habit_audit.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert audit[0]["event"] == "habit_command_received"
    assert audit[0]["args"] == ["delete", "debug-habit-42"]
    assert audit[1]["event"] == "habit_command_blocked"


@pytest.mark.asyncio
async def test_habit_menu_escapes_content_and_controls_agent_override(tmp_path):
    runtime = FakeRuntime(tmp_path)
    store = runtime.backend_manager.current_backend._her_habit_store()
    _seed_habit(store, "Inspect <unsafe> output")

    await runtime_her_habits.cmd_habit(
        runtime,
        _command_update(),
        SimpleNamespace(args=[]),
    )

    reply = runtime.replies[-1]
    assert "Inspect &lt;unsafe&gt; output" in reply["text"]
    assert "Inspect <unsafe> output" not in reply["text"]
    assert _callback_data(reply["reply_markup"], "habit:set:off") == "habit:set:off"

    await runtime_her_habits.cmd_habit(
        runtime,
        _command_update(),
        SimpleNamespace(args=["off"]),
    )
    assert runtime.backend_manager.override is False
    assert (
        runtime.backend_manager.current_backend._habit_meditation_config().enabled
        is False
    )

    await runtime_her_habits.cmd_habit(
        runtime,
        _command_update(),
        SimpleNamespace(args=["default"]),
    )
    assert runtime.backend_manager.override is None
    assert (
        runtime.backend_manager.current_backend._habit_meditation_config().enabled
        is True
    )


def test_habit_detail_bounds_escaped_html_to_telegram_limit(tmp_path):
    store = HERHabitStore(tmp_path)
    [outcome] = store.apply_actions(
        [
            {
                "operation": "create",
                "title": "<" * 160,
                "metadata": "&" * 2_000,
                "body": ">" * 8_000,
            }
        ],
        max_actions=1,
    )
    habit = store.get(outcome.split(":", 1)[1])

    text, _markup = runtime_her_habits._detail_view(habit, offset=0)

    assert len(text) < 4_096
    assert "<" * 20 not in text
    assert "&lt;" in text
    assert "&amp;" in text
    assert "&gt;" in text


@pytest.mark.asyncio
async def test_environment_override_blocks_command_toggle_without_persisting(
    tmp_path,
    monkeypatch,
):
    runtime = FakeRuntime(tmp_path)
    monkeypatch.setenv("HASHI_HER_HABIT_MEDITATION", "off")

    await runtime_her_habits.cmd_habit(
        runtime,
        _command_update(),
        SimpleNamespace(args=["on"]),
    )

    assert runtime.backend_manager.override is None
    assert (
        runtime.backend_manager.current_backend._habit_meditation_config().enabled
        is False
    )
    assert "environment override locks" in runtime.replies[-1]["text"]
    audit_events = [
        json.loads(line)["event"]
        for line in (tmp_path / "backend_state" / "her_habit_audit.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert audit_events == ["habit_command_received", "habit_control_blocked"]


@pytest.mark.asyncio
async def test_delete_requires_confirmation_and_archives_with_full_audit(tmp_path):
    runtime = FakeRuntime(tmp_path)
    store = runtime.backend_manager.current_backend._her_habit_store()
    habit_id = _seed_habit(store, "Keep complete debugging evidence")

    await runtime_her_habits.cmd_habit(
        runtime,
        _command_update(),
        SimpleNamespace(args=["delete", habit_id]),
    )

    assert store.get(habit_id) is not None
    confirm_data = _callback_data(
        runtime.replies[-1]["reply_markup"],
        "habit:confirm_delete:",
    )
    callback_update, query = _callback_update(confirm_data)
    await runtime_her_habits.callback_habit(runtime, callback_update, SimpleNamespace())

    assert store.get(habit_id) is None
    assert store.archived_count() == 1
    assert "Archived" in query.edits[-1]["text"]
    audit_rows = [
        json.loads(line)
        for line in (tmp_path / "backend_state" / "her_habit_audit.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    completed = next(
        row for row in audit_rows if row["event"] == "habit_command_delete_completed"
    )
    assert completed["target"]["id"] == habit_id
    assert (
        completed["target"]["body"]
        == "Apply the complete behaviour for Keep complete debugging evidence."
    )


@pytest.mark.asyncio
async def test_stale_single_delete_confirmation_cannot_remove_changed_habit(tmp_path):
    runtime = FakeRuntime(tmp_path)
    store = runtime.backend_manager.current_backend._her_habit_store()
    habit_id = _seed_habit(store, "Review before deleting")
    await runtime_her_habits.cmd_habit(
        runtime,
        _command_update(),
        SimpleNamespace(args=["delete", habit_id]),
    )
    stale_confirmation = _callback_data(
        runtime.replies[-1]["reply_markup"],
        "habit:confirm_delete:",
    )
    store.apply_actions(
        [
            {
                "operation": "update",
                "habit_id": habit_id,
                "body": "This changed after the confirmation card was rendered.",
            }
        ],
        max_actions=1,
    )

    callback_update, query = _callback_update(stale_confirmation)
    await runtime_her_habits.callback_habit(runtime, callback_update, SimpleNamespace())

    assert store.get(habit_id) is not None
    assert store.archived_count() == 0
    assert query.answers[-1] == ("Habit changed; confirm deletion again.", True)
    assert "changed or no longer exists" in query.edits[-1]["text"]


@pytest.mark.asyncio
async def test_delete_all_then_reset_creates_recoverable_snapshot(tmp_path):
    runtime = FakeRuntime(tmp_path)
    adapter = runtime.backend_manager.current_backend
    store = adapter._her_habit_store()
    _seed_habit(store, "First Habit")
    _seed_habit(store, "Second Habit")

    delete_update, delete_query = _callback_update("habit:delete_all")
    await runtime_her_habits.callback_habit(runtime, delete_update, SimpleNamespace())
    confirm_all = _callback_data(
        delete_query.edits[-1]["reply_markup"],
        "habit:confirm_all:",
    )
    confirm_update, _confirm_query = _callback_update(confirm_all)
    await runtime_her_habits.callback_habit(runtime, confirm_update, SimpleNamespace())

    assert store.load() == []
    assert store.archived_count() == 2

    adapter._her_meditation_journal().enqueue(
        job_id="a" * 32,
        request_id="req-reset",
        prompt="debuggable meditation prompt",
        max_actions=3,
    )
    runtime.backend_manager.override = False
    reset_update, reset_query = _callback_update("habit:reset")
    await runtime_her_habits.callback_habit(runtime, reset_update, SimpleNamespace())
    confirm_reset = _callback_data(
        reset_query.edits[-1]["reply_markup"],
        "habit:confirm_reset:",
    )
    confirm_update, confirm_query = _callback_update(confirm_reset)
    await runtime_her_habits.callback_habit(runtime, confirm_update, SimpleNamespace())

    snapshots = list((tmp_path / "backend_state" / "her_habit_resets").iterdir())
    assert len(snapshots) == 1
    assert (snapshots[0] / "manifest.json").is_file()
    assert (snapshots[0] / "habits" / "archive").is_dir()
    assert (snapshots[0] / "her_habit_meditation" / f"{'a' * 32}.json").is_file()
    assert runtime.backend_manager.override is False
    assert (tmp_path / "backend_state" / "her_habit_audit.jsonl").is_file()
    assert "recoverable snapshot" in confirm_query.edits[-1]["text"]


def test_notification_outbox_uses_task_start_verbose_and_no_change_is_silent(tmp_path):
    journal = HERMeditationJournal(tmp_path)
    store = HERHabitStore(tmp_path)
    job_id = "b" * 32
    journal.enqueue(
        job_id=job_id,
        request_id="req-notify",
        prompt="meditate",
        max_actions=3,
        notification_context={
            "chat_id": 99,
            "verbose_at_start": True,
            "silent": False,
            "deliver_to_telegram": True,
            "request_summary": "Build the report",
            "request_source": "telegram",
        },
    )
    assert journal.claim(job_id) == "meditate"
    actions = [
        {
            "operation": "create",
            "title": "Verify report output",
            "metadata": "Relevant after building reports.",
            "body": "Inspect the generated report before delivery.",
        }
    ]
    journal.store_actions(job_id, actions)
    outcomes, changes = store.apply_actions_with_changes(
        actions,
        max_actions=3,
        idempotency_key=job_id,
    )
    journal.mark_complete(
        job_id,
        outcomes,
        changes=[change.to_payload() for change in changes],
    )

    [pending] = journal.pending_notifications()
    assert pending["notification"]["status"] == "pending"
    assert (
        pending["changes"][0]["after"]["body"]
        == "Inspect the generated report before delivery."
    )
    claimed = journal.claim_notification(job_id)
    assert claimed["notification"]["status"] == "sending"
    journal.mark_notification_sent(job_id)
    assert journal.get(job_id)["notification"]["status"] == "sent"

    no_op_id = "2" * 32
    habit = store.get(changes[0].habit_id)
    no_op_actions = [
        {
            "operation": "update",
            "habit_id": habit.habit_id,
            "title": habit.title,
            "metadata": habit.metadata,
            "body": habit.body,
        }
    ]
    journal.enqueue(
        job_id=no_op_id,
        request_id="req-no-op-update",
        prompt="meditate",
        max_actions=3,
        notification_context={
            "chat_id": 99,
            "verbose_at_start": True,
            "silent": False,
            "deliver_to_telegram": True,
        },
    )
    assert journal.claim(no_op_id) == "meditate"
    journal.store_actions(no_op_id, no_op_actions)
    no_op_outcomes, no_op_changes = store.apply_actions_with_changes(
        no_op_actions,
        max_actions=3,
        idempotency_key=no_op_id,
    )
    journal.mark_complete(no_op_id, no_op_outcomes, changes=[])
    assert no_op_outcomes == [f"unchanged:{habit.habit_id}"]
    assert no_op_changes == []
    assert journal.get(no_op_id)["notification"]["reason"] == "no_habit_change"

    no_change_id = "c" * 32
    journal.enqueue(
        job_id=no_change_id,
        request_id="req-no-change",
        prompt="meditate",
        max_actions=3,
        notification_context={
            "chat_id": 99,
            "verbose_at_start": True,
            "silent": False,
            "deliver_to_telegram": True,
        },
    )
    assert journal.claim(no_change_id) == "meditate"
    journal.store_actions(no_change_id, [])
    journal.mark_complete(no_change_id, [], changes=[])
    no_change_notification = journal.get(no_change_id)["notification"]
    assert no_change_notification["status"] == "skipped"
    assert no_change_notification["reason"] == "no_habit_change"
    assert journal.pending_notifications() == []

    quiet_id = "d" * 32
    journal.enqueue(
        job_id=quiet_id,
        request_id="req-quiet",
        prompt="meditate",
        max_actions=3,
        notification_context={
            "chat_id": 99,
            "verbose_at_start": False,
            "silent": False,
            "deliver_to_telegram": True,
        },
    )
    assert (
        journal.get(quiet_id)["notification"]["reason"] == "verbose_off_at_task_start"
    )


@pytest.mark.asyncio
async def test_notification_card_is_html_safe_and_links_to_habit(tmp_path):
    runtime = FakeRuntime(tmp_path)
    job = {
        "job_id": "e" * 32,
        "request_id": "req-card",
        "notification": {
            "chat_id": 99,
            "request_summary": "Review <unsafe> input",
        },
        "changes": [
            {
                "operation": "created",
                "habit_id": "review-output-1234",
                "before": None,
                "after": {
                    "title": "Review <unsafe> output",
                    "body": "Inspect it.",
                },
            }
        ],
    }

    assert await runtime_her_habits.deliver_habit_notification(runtime, job) is True

    _chat_id, text, kwargs = runtime.sent_messages[-1]
    assert "Review &lt;unsafe&gt; input" in text
    assert "Review &lt;unsafe&gt; output" in text
    assert "Review <unsafe>" not in text
    assert _callback_data(kwargs["reply_markup"], "habit:view:").startswith(
        "habit:view:"
    )


@pytest.mark.asyncio
async def test_adapter_write_delivers_durable_verbose_notification(tmp_path):
    runtime = FakeRuntime(tmp_path)
    adapter = runtime.backend_manager.current_backend
    adapter.config._hashi_runtime = runtime
    journal = adapter._her_meditation_journal()
    job_id = "f" * 32
    actions = [
        {
            "operation": "create",
            "title": "Notify after a real change",
            "metadata": "Relevant when Meditation forms a reusable Habit.",
            "body": "Send the durable Verbose notice after Write completes.",
        }
    ]
    journal.enqueue(
        job_id=job_id,
        request_id="req-adapter-notice",
        prompt="meditate",
        max_actions=3,
        notification_context={
            "chat_id": 99,
            "verbose_at_start": True,
            "silent": False,
            "deliver_to_telegram": True,
        },
    )
    assert journal.claim(job_id) == "meditate"
    journal.store_actions(job_id, actions)

    await adapter._run_habit_meditation(
        job_id=job_id,
        config=adapter._habit_meditation_config(),
    )
    pending_delivery_tasks = list(adapter._habit_notification_tasks)
    assert pending_delivery_tasks
    await asyncio.gather(*pending_delivery_tasks)

    completed = journal.get(job_id)
    assert completed["status"] == "completed"
    assert completed["notification"]["status"] == "sent"
    assert completed["notification"]["attempts"] == 1
    assert len(completed["changes"]) == 1
    assert "Notify after a real change" in runtime.sent_messages[-1][1]


@pytest.mark.asyncio
async def test_interrupted_write_replay_reconstructs_and_delivers_change_notice(
    tmp_path,
):
    runtime = FakeRuntime(tmp_path)
    adapter = runtime.backend_manager.current_backend
    adapter.config._hashi_runtime = runtime
    store = adapter._her_habit_store()
    journal = adapter._her_meditation_journal()
    job_id = "4" * 32
    actions = [
        {
            "operation": "create",
            "title": "Report a change after replay",
            "metadata": "Relevant when Write completed just before a process interruption.",
            "body": "Use the durable pre-Write baseline to reconstruct the notification.",
        }
    ]
    journal.enqueue(
        job_id=job_id,
        request_id="req-replayed-notice",
        prompt="meditate",
        max_actions=1,
        notification_context={
            "chat_id": 99,
            "verbose_at_start": True,
            "silent": False,
            "deliver_to_telegram": True,
        },
    )
    assert journal.claim(job_id) == "meditate"
    baseline = store.capture_action_baseline(
        actions,
        max_actions=1,
        idempotency_key=job_id,
    )
    journal.store_actions(job_id, actions, action_baseline=baseline)
    # Simulate a process exit after the atomic Habit write but before the job
    # could persist outcomes/change-notification state.
    store.apply_actions_with_changes(
        actions,
        max_actions=1,
        idempotency_key=job_id,
        action_baseline=baseline,
    )

    await adapter._run_habit_meditation(
        job_id=job_id,
        config=adapter._habit_meditation_config(),
    )
    await asyncio.gather(*list(adapter._habit_notification_tasks))

    completed = journal.get(job_id)
    assert len(store.load()) == 1
    assert completed["notification"]["status"] == "sent"
    assert completed["changes"][0]["operation"] == "created"
    assert "Report a change after replay" in runtime.sent_messages[-1][1]


@pytest.mark.asyncio
async def test_notification_delivery_retries_are_bounded_and_audited(
    tmp_path,
    monkeypatch,
):
    import adapters.her as her_module

    runtime = FakeRuntime(tmp_path)
    adapter = runtime.backend_manager.current_backend
    adapter.config._hashi_runtime = runtime
    journal = adapter._her_meditation_journal()
    job_id = "1" * 32
    actions = [
        {
            "operation": "create",
            "title": "Retry a temporary notification failure",
            "metadata": "Relevant when Telegram delivery fails temporarily.",
            "body": "Retry a bounded number of times and retain detailed audit evidence.",
        }
    ]
    journal.enqueue(
        job_id=job_id,
        request_id="req-retry-notice",
        prompt="meditate",
        max_actions=3,
        notification_context={
            "chat_id": 99,
            "verbose_at_start": True,
            "silent": False,
            "deliver_to_telegram": True,
        },
    )
    assert journal.claim(job_id) == "meditate"
    journal.store_actions(job_id, actions)
    outcomes, changes = adapter._her_habit_store().apply_actions_with_changes(
        actions,
        max_actions=3,
        idempotency_key=job_id,
    )
    journal.mark_complete(
        job_id,
        outcomes,
        changes=[change.to_payload() for change in changes],
    )
    attempts = 0

    async def flaky_sender(_job: dict[str, Any]) -> bool:
        nonlocal attempts
        attempts += 1
        return attempts == 3

    async def no_delay(_seconds: float) -> None:
        return None

    runtime._deliver_her_habit_notification = flaky_sender
    monkeypatch.setattr(her_module.asyncio, "sleep", no_delay)

    await adapter._run_habit_notification(job_id)

    notification = journal.get(job_id)["notification"]
    assert attempts == 3
    assert notification["attempts"] == 3
    assert notification["status"] == "sent"
    audit_events = [
        json.loads(line)["event"]
        for line in (tmp_path / "backend_state" / "her_habit_audit.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert audit_events.count("habit_notification_failed") == 2
    assert audit_events.count("habit_notification_sent") == 1


@pytest.mark.asyncio
async def test_delivery_outage_defers_without_consuming_retry_budget(tmp_path):
    runtime = FakeRuntime(tmp_path)
    runtime.telegram_connected = False
    adapter = runtime.backend_manager.current_backend
    adapter.config._hashi_runtime = runtime
    journal = adapter._her_meditation_journal()
    job_id = "3" * 32
    journal.enqueue(
        job_id=job_id,
        request_id="req-deferred-notice",
        prompt="meditate",
        max_actions=1,
        notification_context={
            "chat_id": 99,
            "verbose_at_start": True,
            "silent": False,
            "deliver_to_telegram": True,
        },
    )
    assert journal.claim(job_id) == "meditate"
    actions = [
        {
            "operation": "create",
            "title": "Defer while Telegram is unavailable",
            "metadata": "Relevant during a transport outage.",
            "body": "Keep the durable notice pending without spending its retry budget.",
        }
    ]
    journal.store_actions(job_id, actions)
    outcomes, changes = adapter._her_habit_store().apply_actions_with_changes(
        actions,
        max_actions=1,
        idempotency_key=job_id,
    )
    journal.mark_complete(
        job_id,
        outcomes,
        changes=[change.to_payload() for change in changes],
    )

    await adapter._run_habit_notification(job_id)

    notification = journal.get(job_id)["notification"]
    assert notification["status"] == "pending"
    assert notification["reason"] == "delivery_deferred"
    assert notification["attempts"] == 0
    assert [job["job_id"] for job in journal.pending_notifications()] == [job_id]


def test_adapter_notification_context_is_captured_from_matching_task_start(tmp_path):
    runtime = FakeRuntime(tmp_path)
    adapter = runtime.backend_manager.current_backend
    adapter.config._hashi_runtime = runtime
    runtime.current_request_meta = {
        "request_id": "req-match",
        "chat_id": 99,
        "verbose_at_start": True,
        "silent": False,
        "deliver_to_telegram": True,
        "source": "telegram",
        "summary": "Matching task",
    }

    captured = adapter._habit_notification_context("req-match", silent=False)

    assert captured == {
        "chat_id": 99,
        "verbose_at_start": True,
        "silent": False,
        "deliver_to_telegram": True,
        "request_source": "telegram",
        "request_summary": "Matching task",
    }
    assert adapter._habit_notification_context("req-other", silent=False) == {
        "chat_id": None,
        "verbose_at_start": False,
        "silent": False,
        "deliver_to_telegram": False,
        "request_source": None,
        "request_summary": None,
    }


def test_habit_command_is_visible_but_not_sensitive():
    spec = COMMAND_SPEC_BY_NAME["habit"]

    assert spec.menu_visible is True
    assert spec.sensitive is False
    assert "habit" not in SENSITIVE_COMMAND_NAMES
    assert redact_args("habit", ["delete", "debug-habit-42"]) == [
        "delete",
        "debug-habit-42",
    ]


def test_habit_override_persists_and_applies_only_to_her_adapter_config(tmp_path):
    workspace = tmp_path / "agent"
    workspace.mkdir()
    config = FlexibleAgentConfig(
        name="zelda",
        workspace_dir=workspace,
        system_md=workspace / "AGENT.md",
        telegram_token_key="zelda",
        allowed_backends=[
            {"engine": "her", "model": "test-model"},
            {"engine": "codex-cli", "model": "gpt-test"},
        ],
        active_backend="her",
        project_root=workspace,
    )
    global_config = GlobalConfig(
        authorized_id=1,
        base_logs_dir=workspace / "logs",
        base_media_dir=workspace / "media",
        project_root=workspace,
        her_providers={"habit_meditation": {"enabled": False}},
    )
    manager = FlexibleBackendManager(config, global_config, secrets={})
    manager.current_backend = SimpleNamespace(
        config=SimpleNamespace(engine="her", model="test-model", extra={}),
    )

    manager.set_habit_meditation_override(True)

    assert manager.get_habit_meditation_override() is True
    assert manager.current_backend.config.extra["habit_meditation_enabled"] is True
    assert (
        manager._build_adapter_config(
            "her",
            config.allowed_backends[0],
        ).extra["habit_meditation_enabled"]
        is True
    )
    assert (
        "habit_meditation_enabled"
        not in manager._build_adapter_config(
            "codex-cli",
            config.allowed_backends[1],
        ).extra
    )
    reloaded = FlexibleBackendManager(config, global_config, secrets={})
    assert reloaded.get_habit_meditation_override() is True

    reloaded.set_habit_meditation_override(None)
    assert reloaded.get_habit_meditation_override() is None
    state = json.loads((workspace / "state.json").read_text(encoding="utf-8"))
    assert "her_habit_meditation" not in state
