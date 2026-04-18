#!/usr/bin/env python3
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.modules.setdefault("edge_tts", SimpleNamespace())

from orchestrator.agent_runtime import QueuedRequest
from orchestrator import flexible_agent_runtime as flex_module
from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime
from orchestrator.habits import HabitStore


def _make_item() -> QueuedRequest:
    return QueuedRequest(
        request_id="req-0001",
        chat_id=123,
        prompt="Please help",
        source="text",
        summary="Please help",
        created_at="2026-04-11T19:00:00+10:00",
    )


def test_build_habit_sections_populates_request_and_returns_prompt_section():
    runtime = FlexibleAgentRuntime.__new__(FlexibleAgentRuntime)
    runtime.habit_store = Mock()
    runtime.logger = Mock()
    runtime._log_maintenance = Mock()

    retrieved = [SimpleNamespace(habit_id="habit-1"), SimpleNamespace(habit_id="habit-2")]
    serialized = [
        {"habit_id": "habit-1", "instruction": "Verify before answering."},
        {"habit_id": "habit-2", "instruction": "State uncertainty clearly."},
    ]
    section = ("ACTIVE HABITS", "- DO: Verify before answering.")

    runtime.habit_store.retrieve.return_value = retrieved
    runtime.habit_store.serialize_habits.return_value = serialized
    runtime.habit_store.render_prompt_section.return_value = section

    item = _make_item()
    sections, habit_ids = runtime._build_habit_sections(item, item.prompt)

    assert sections == [section]
    assert habit_ids == ["habit-1", "habit-2"]
    assert item.active_habits == serialized
    runtime.habit_store.mark_triggered.assert_called_once_with(retrieved)


def test_record_habit_outcome_delegates_only_when_active_habits_exist():
    runtime = FlexibleAgentRuntime.__new__(FlexibleAgentRuntime)
    runtime.habit_store = Mock()
    runtime.error_logger = Mock()

    item = _make_item()
    item.active_habits = [{"habit_id": "habit-1"}]
    runtime._record_habit_outcome(item, success=True, response_text="Done")

    runtime.habit_store.record_execution_outcome.assert_called_once_with(
        request_id="req-0001",
        prompt="Please help",
        source="text",
        summary="Please help",
        active_habits=[{"habit_id": "habit-1"}],
        response_text="Done",
        error_text=None,
        success=True,
    )

    runtime.habit_store.reset_mock()
    item.active_habits = []
    runtime._record_habit_outcome(item, success=False, error_text="boom")
    runtime.habit_store.record_execution_outcome.assert_not_called()


def test_capture_followup_habit_feedback_uses_last_response_metadata():
    runtime = FlexibleAgentRuntime.__new__(FlexibleAgentRuntime)
    runtime.habit_store = Mock()
    runtime.error_logger = Mock()
    runtime.maintenance_logger = Mock()
    runtime.last_response = {
        "request_id": "req-0001",
        "responded_at": "2026-04-11T19:05:00+10:00",
    }
    runtime.habit_store.apply_user_feedback.return_value = SimpleNamespace(
        sentiment="positive",
        updated_events=1,
        updated_habits=["habit-1"],
    )

    runtime._capture_followup_habit_feedback("Thanks, that was right.")

    runtime.habit_store.apply_user_feedback.assert_called_once_with(
        request_id="req-0001",
        feedback_text="Thanks, that was right.",
        responded_at="2026-04-11T19:05:00+10:00",
    )


def test_habit_store_must_be_reinitialized_after_habits_db_is_deleted(tmp_path):
    project_root = tmp_path / "project"
    workspace_dir = project_root / "workspaces" / "akane"
    (project_root / "workspaces" / "lily").mkdir(parents=True)
    workspace_dir.mkdir(parents=True)

    store = HabitStore(
        workspace_dir=workspace_dir,
        project_root=project_root,
        agent_id="akane",
        agent_class="general",
    )
    store.upsert_habit(
        habit_type="do",
        title="Test Habit",
        instruction="Verify before answering.",
        trigger={"keywords": ["verify"]},
    )

    habits_db = workspace_dir / "habits.sqlite"
    habits_db.unlink()

    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        store.retrieve("please verify this", source="text", summary="please verify this")

    reinitialized = HabitStore(
        workspace_dir=workspace_dir,
        project_root=project_root,
        agent_id="akane",
        agent_class="general",
    )
    retrieved = reinitialized.retrieve("please verify this", source="text", summary="please verify this")

    assert retrieved == []


@pytest.mark.anyio
async def test_handle_message_captures_followup_before_enqueue(monkeypatch):
    runtime = FlexibleAgentRuntime.__new__(FlexibleAgentRuntime)
    runtime._is_authorized_user = Mock(return_value=True)
    runtime._should_redirect_after_transfer = Mock(return_value=False)
    runtime._transfer_redirect_text = Mock(return_value="redirect")
    runtime._reply_text = AsyncMock()
    runtime._long_buffer_active = False
    runtime._long_buffer = []
    runtime._capture_followup_habit_feedback = Mock()
    runtime.enqueue_request = AsyncMock()
    runtime.logger = Mock()
    runtime.name = "akane"
    runtime._active_chat_ids = {}

    monkeypatch.setattr(flex_module, "_print_user_message", lambda *args, **kwargs: None)

    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=123),
        effective_chat=SimpleNamespace(id=456),
        message=SimpleNamespace(text="Please continue"),
    )

    await runtime.handle_message(update, None)

    runtime._capture_followup_habit_feedback.assert_called_once_with("Please continue")
    runtime.enqueue_request.assert_awaited_once_with(456, "Please continue", "text", "Please continue")
