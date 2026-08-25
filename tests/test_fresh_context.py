from types import SimpleNamespace
from datetime import datetime
import json
import logging
import sys
import types

# ruff: noqa: E402 -- edge_tts must be stubbed before runtime imports.

import pytest

sys.modules.setdefault("edge_tts", types.ModuleType("edge_tts"))

from orchestrator.bridge_memory import BridgeContextAssembler, BridgeMemoryStore
from orchestrator import runtime_cross_session, runtime_scheduler_recovery
from orchestrator.context_compaction import CompactionStore, install_history_section
from orchestrator.dual_brain_mode import DualBrainObserver
from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime
from orchestrator.fresh_context import (
    automatic_context_suppressed,
    habit_context_suppressed,
    resume_automatic_context,
    resume_habit_context,
    start_boundary,
)
from orchestrator.fresh_context import state as fresh_context_state
from orchestrator.memory_plus_mode import is_memory_plus_enabled, set_memory_plus_enabled
from orchestrator.memory_search_mode import is_memory_search_enabled
from orchestrator.workspace_state import WorkspaceStateStore


class FakeMemoryStore:
    def __init__(self):
        self.turns_cleared = 0
        self.recent_calls = 0
        self.memory_calls = 0

    def get_last_user_turn_ts(self):
        return None

    def get_recent_turns(self, limit=10):
        self.recent_calls += 1
        return [{"role": "user", "text": "old turn"}]

    def retrieve_memories(self, query, limit=6):
        self.memory_calls += 1
        return [{"memory_type": "note", "source": "test", "content": "saved memory"}]

    def clear_turns(self):
        self.turns_cleared += 1
        return 1

    def get_stats(self):
        return {"turns": 1, "memories": 1}


class FakeMessage:
    def __init__(self):
        self.replies = []

    async def reply_text(self, text, **kwargs):
        self.replies.append(text)


def fake_update(user_id=123, chat_id=456):
    message = FakeMessage()
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id),
        effective_chat=SimpleNamespace(id=chat_id),
        message=message,
        _message=message,
    )


def fake_context(*args):
    return SimpleNamespace(args=list(args))


def test_bridge_context_assembler_splits_turn_and_saved_memory_flags():
    store = FakeMemoryStore()
    assembler = BridgeContextAssembler(store, system_md=None)

    prompt = assembler.build_prompt("hello", "deepseek-api")
    assert "RECENT CONTEXT" in prompt
    assert "OPTIONAL LONG-TERM MEMORY SEARCH RESULTS" not in prompt
    assert store.recent_calls == 1
    assert store.memory_calls == 0

    store.recent_calls = 0
    store.memory_calls = 0
    assembler.saved_memory_injection_enabled = True
    prompt = assembler.build_prompt("hello", "deepseek-api")
    assert "RECENT CONTEXT" in prompt
    assert "OPTIONAL LONG-TERM MEMORY SEARCH RESULTS" in prompt
    assert store.recent_calls == 1
    assert store.memory_calls == 1

    store.recent_calls = 0
    store.memory_calls = 0
    assembler.saved_memory_injection_enabled = False
    assembler.turns_injection_enabled = False
    prompt = assembler.build_prompt("hello", "deepseek-api")
    assert "RECENT CONTEXT" not in prompt
    assert "OPTIONAL LONG-TERM MEMORY SEARCH RESULTS" not in prompt
    assert store.recent_calls == 0
    assert store.memory_calls == 0


def test_managed_prompt_orders_policy_then_old_memory_then_recent_then_request(tmp_path):
    from orchestrator.context_compaction import MANAGED_HISTORY_TITLE

    system_md = tmp_path / "AGENT.md"
    system_md.write_text("SYSTEM-POLICY", encoding="utf-8")
    store = FakeMemoryStore()
    assembler = BridgeContextAssembler(store, system_md=system_md)
    assembler.saved_memory_injection_enabled = True

    prompt = assembler.build_prompt(
        "CURRENT-REQUEST",
        "her-v2",
        extra_sections=[
            ("WORKZONE", "WORKZONE-POLICY"),
            (MANAGED_HISTORY_TITLE, "OLD-CAPSULE\nRECENT-TIMELINE"),
        ],
    )

    assert prompt.index("SYSTEM-POLICY") < prompt.index("WORKZONE-POLICY")
    assert prompt.index("WORKZONE-POLICY") < prompt.index("saved memory")
    assert prompt.index("saved memory") < prompt.index("OLD-CAPSULE")
    assert prompt.index("OLD-CAPSULE") < prompt.index("RECENT-TIMELINE")
    assert prompt.index("RECENT-TIMELINE") < prompt.index("CURRENT-REQUEST")


@pytest.mark.asyncio
async def test_flex_runtime_new_is_guarded_for_non_cli_backend():
    runtime = FlexibleAgentRuntime.__new__(FlexibleAgentRuntime)
    runtime._authorized_telegram_ids = {123}
    runtime.config = SimpleNamespace(active_backend="openrouter-api")
    runtime.backend_manager = SimpleNamespace(current_backend=object())
    replies = []

    async def reply(update, text, **kwargs):
        replies.append(text)

    runtime._reply_text = reply

    await runtime.cmd_new(fake_update(), fake_context())

    assert "Use /fresh" in replies[0]


@pytest.mark.asyncio
async def test_her_v2_uses_fresh_not_new_even_with_cli_capable_stage_providers():
    runtime = FlexibleAgentRuntime.__new__(FlexibleAgentRuntime)
    runtime._authorized_telegram_ids = {123}
    runtime.config = SimpleNamespace(active_backend="her-v2")
    runtime.backend_manager = SimpleNamespace(current_backend=object())
    replies = []

    async def reply(update, text, **kwargs):
        replies.append(text)

    runtime._reply_text = reply

    await runtime.cmd_new(fake_update(), fake_context())

    assert replies == [
        "This agent is using a non-CLI backend. Use /fresh for a clean API context; /new is reserved for CLI session reset."
    ]


@pytest.mark.asyncio
async def test_flex_runtime_fresh_clears_turns_without_session_reset_llm_prompt():
    runtime = FlexibleAgentRuntime.__new__(FlexibleAgentRuntime)
    runtime._authorized_telegram_ids = {123}
    runtime.config = SimpleNamespace(active_backend="ollama-api")
    runtime.backend_manager = SimpleNamespace(current_backend=object())
    runtime._pending_auto_recall_context = "old"
    runtime._clear_transfer_state = lambda: None
    store = FakeMemoryStore()
    runtime.context_assembler = BridgeContextAssembler(store, system_md=None)
    replies = []
    enqueued = []

    async def reply(update, text, **kwargs):
        replies.append(text)

    async def enqueue_request(*args, **kwargs):
        enqueued.append((args, kwargs))

    runtime._reply_text = reply
    runtime.enqueue_request = enqueue_request

    await runtime.cmd_fresh(fake_update(), fake_context())

    assert store.turns_cleared == 1
    assert runtime.context_assembler.turns_injection_enabled is True
    assert runtime.context_assembler.saved_memory_injection_enabled is False
    assert runtime._pending_auto_recall_context is None
    assert enqueued == []


@pytest.mark.asyncio
async def test_flex_runtime_fresh_keeps_next_api_prompt_dual_brain_eligible(tmp_path):
    runtime = FlexibleAgentRuntime.__new__(FlexibleAgentRuntime)
    runtime._authorized_telegram_ids = {123}
    runtime.config = SimpleNamespace(active_backend="openrouter-api")
    runtime.backend_manager = SimpleNamespace(current_backend=object())
    runtime._pending_auto_recall_context = "old"
    runtime._clear_transfer_state = lambda: None
    store = FakeMemoryStore()
    runtime.context_assembler = BridgeContextAssembler(store, system_md=None)
    replies = []
    enqueued = []

    async def reply(update, text, **kwargs):
        replies.append(text)

    async def enqueue_request(*args, **kwargs):
        enqueued.append((args, kwargs))

    runtime._reply_text = reply
    runtime.enqueue_request = enqueue_request

    await runtime.cmd_fresh(fake_update(), fake_context())

    assert enqueued == []
    assert runtime.context_assembler.turns_injection_enabled is True
    assert runtime.context_assembler.saved_memory_injection_enabled is False

    workspace = tmp_path / "sakura"
    workspace.mkdir()
    (workspace / "state.json").write_text(json.dumps({"agent_mode": "dual-brain"}), encoding="utf-8")
    observer = DualBrainObserver(
        workspace_dir=workspace,
        backend_invoker=lambda *args, **kwargs: None,
        backend_context_getter=lambda: {"engine": "openrouter-api", "model": "test-model"},
    )

    assert not observer.should_provide("session_reset", is_bridge_request=False)
    assert not observer.should_observe("session_reset", is_bridge_request=False)
    assert observer.should_provide("api", is_bridge_request=False)
    assert observer.should_observe("api", is_bridge_request=False)


@pytest.mark.asyncio
async def test_her_v2_fresh_persists_hard_boundary_without_deleting_archives(tmp_path):
    runtime = FlexibleAgentRuntime.__new__(FlexibleAgentRuntime)
    runtime.name = "arale"
    runtime.logger = logging.getLogger("test.fresh")
    runtime.workspace_dir = tmp_path
    runtime._authorized_telegram_ids = {123}
    runtime.config = SimpleNamespace(active_backend="her-v2")
    manager = SimpleNamespace(
        current_backend=object(),
        state_store=WorkspaceStateStore(tmp_path),
        agent_mode="flex",
    )
    runtime.backend_manager = manager
    runtime._pending_auto_recall_context = "old recall"
    runtime._pending_session_primer = "old primer"
    runtime._clear_transfer_state = lambda: None
    runtime._context_compaction_tasks = set()
    runtime.memory_store = BridgeMemoryStore(tmp_path)
    runtime.memory_store.record_turn("user", "text", "OLD WORKING TURN")
    runtime.memory_store.record_turn("assistant", "her-v2", "OLD WORKING ANSWER")
    runtime.memory_store.record_completed_exchange(
        "OLD COMPLETED REQUEST",
        "OLD COMPLETED ANSWER",
        "text",
        user_ts="2026-08-24T10:00:00+10:00",
        assistant_ts="2026-08-24T10:01:00+10:00",
        origin_ref="old-completed",
    )
    agent_md = tmp_path / "AGENT.md"
    agent_md.write_text("AGENT POLICY ONLY", encoding="utf-8")
    runtime.context_assembler = BridgeContextAssembler(
        runtime.memory_store,
        system_md=agent_md,
    )
    runtime.context_assembler.saved_memory_injection_enabled = True
    runtime.reload_post_turn_observers = lambda: None
    replies = []

    async def reply(update, text, **kwargs):
        replies.append(text)

    runtime._reply_text = reply
    set_memory_plus_enabled(tmp_path, True)
    compaction_store = CompactionStore(tmp_path)
    compaction_store.archive_dir.mkdir(parents=True)
    compaction_store.capsule_dir.mkdir(parents=True)
    archived = compaction_store.archive_dir / "old.json"
    capsule = compaction_store.capsule_dir / "old.json"
    archived.write_text("archived raw history", encoding="utf-8")
    capsule.write_text("archived capsule", encoding="utf-8")
    compaction_store.state_path.write_text(
        json.dumps(
            {
                "format": "hashi-context-compaction-pointer-v1",
                "generation": 4,
                "active_capsule": {
                    "ref": "capsules/old.json",
                    "archive_ref": "archives/old.json",
                },
            }
        ),
        encoding="utf-8",
    )

    await runtime.cmd_fresh(fake_update(), fake_context())

    boundary = fresh_context_state(runtime)
    assert boundary["generation"] == 1
    assert boundary["cutoff_epoch"] > 0
    assert boundary["automatic_context_suppressed"] is True
    assert boundary["habit_context_suppressed"] is True
    assert runtime.memory_store.get_stats()["turns"] == 0
    assert runtime._pending_auto_recall_context is None
    assert runtime._pending_session_primer is None
    assert runtime.context_assembler.turns_injection_enabled is True
    assert runtime.context_assembler.saved_memory_injection_enabled is False
    assert is_memory_search_enabled(tmp_path) is False
    assert is_memory_plus_enabled(tmp_path) is True
    assert CompactionStore(tmp_path).read_state() == {
        "format": "hashi-context-compaction-pointer-v1",
        "generation": 5,
        "active_capsule": None,
    }
    assert archived.read_text(encoding="utf-8") == "archived raw history"
    assert capsule.read_text(encoding="utf-8") == "archived capsule"
    assert "Logs, completed exchanges, saved memories" in replies[-1]

    sections, _snapshot = install_history_section(runtime, [])
    payload = runtime.context_assembler.build_prompt_payload(
        "CURRENT REQUEST",
        "her-v2",
        extra_sections=sections,
    )
    assert "AGENT POLICY ONLY" in payload["final_prompt"]
    assert "CURRENT REQUEST" in payload["final_prompt"]
    assert "OLD WORKING" not in payload["final_prompt"]
    assert "OLD COMPLETED" not in payload["final_prompt"]


def test_fresh_boundary_filters_cross_session_receipts_by_request_start(tmp_path):
    runtime = SimpleNamespace(
        name="arale",
        workspace_dir=tmp_path,
        config=SimpleNamespace(active_backend="her-v2"),
        backend_manager=SimpleNamespace(state_store=WorkspaceStateStore(tmp_path)),
        _request_meta_by_id={},
        current_request_meta=None,
        get_current_model=lambda: "test-model",
    )
    response = SimpleNamespace(
        is_success=True,
        stop_reason="end_turn",
        stream_metadata={
            "pending_interaction": {
                "kind": "question",
                "question": "Continue?",
            }
        },
    )
    old_item = SimpleNamespace(
        request_id="old",
        chat_id=456,
        source="scheduler",
        summary="old scheduled task",
        prompt="old prompt",
        created_at="2026-08-25T10:00:00+10:00",
    )
    runtime_cross_session.record_turn_result(
        runtime,
        old_item,
        assistant_text="old result; continue?",
        response=response,
        delivered=True,
        completion_path="foreground",
    )
    start_boundary(
        runtime,
        now_epoch=datetime.fromisoformat(
            "2026-08-25T10:30:00+10:00"
        ).timestamp(),
    )
    user_item = SimpleNamespace(
        request_id="user",
        chat_id=456,
        source="text",
        prompt="yes",
        silent=False,
    )

    assert runtime_cross_session.timeline_entries(runtime, user_item) == []
    assert runtime_cross_session.capture_reply_target(runtime, user_item) is None

    new_item = SimpleNamespace(
        request_id="new",
        chat_id=456,
        source="scheduler",
        summary="new scheduled task",
        prompt="new prompt",
        created_at="2026-08-25T10:31:00+10:00",
    )
    runtime_cross_session.record_turn_result(
        runtime,
        new_item,
        assistant_text="new result; continue?",
        response=response,
        delivered=True,
        completion_path="foreground",
    )

    entries = runtime_cross_session.timeline_entries(runtime, user_item)
    assert [entry["request_id"] for entry in entries] == ["new"]


@pytest.mark.asyncio
async def test_fresh_boundary_does_not_intercept_reply_as_old_scheduler_recovery(
    tmp_path,
):
    calls = []

    async def handle_recovery_reply(**kwargs):
        calls.append(kwargs)
        return "old recovery handled"

    runtime = SimpleNamespace(
        name="arale",
        workspace_dir=tmp_path,
        config=SimpleNamespace(active_backend="her-v2"),
        backend_manager=SimpleNamespace(state_store=WorkspaceStateStore(tmp_path)),
        orchestrator=SimpleNamespace(
            scheduler=SimpleNamespace(handle_recovery_reply=handle_recovery_reply)
        ),
    )
    start_boundary(runtime)

    handled = await runtime_scheduler_recovery.handle_reply(
        runtime,
        text="yes",
        chat_id=456,
    )

    assert handled is False
    assert calls == []


def test_fresh_auxiliary_and_habit_fences_resume_independently(tmp_path):
    runtime = SimpleNamespace(
        workspace_dir=tmp_path,
        backend_manager=SimpleNamespace(state_store=WorkspaceStateStore(tmp_path)),
    )
    start_boundary(runtime)

    assert automatic_context_suppressed(runtime) is True
    assert habit_context_suppressed(runtime) is True

    assert resume_automatic_context(runtime) is True
    assert automatic_context_suppressed(runtime) is False
    assert habit_context_suppressed(runtime) is True

    assert resume_habit_context(runtime) is True
    assert habit_context_suppressed(runtime) is False
