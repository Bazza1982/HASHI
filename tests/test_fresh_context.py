import asyncio
import json
import sys
import types
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

# ruff: noqa: E402 -- edge_tts must be stubbed before runtime imports.
import pytest

sys.modules.setdefault("edge_tts", types.ModuleType("edge_tts"))

from orchestrator import (
    runtime_cross_session,
    runtime_scheduler_recovery,
    runtime_session,
)
from orchestrator.admin_local_testing import execute_local_command
from orchestrator.bridge_memory import BridgeContextAssembler, BridgeMemoryStore
from orchestrator.dual_brain_mode import DualBrainObserver
from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime
from orchestrator.fresh_context import (
    automatic_context_suppressed,
    habit_context_suppressed,
    resume_automatic_context,
    resume_habit_context,
    start_boundary,
)
from orchestrator.pcm import render_pcm_document
from orchestrator.runtime_common import QueuedRequest
from orchestrator.workspace_state import WorkspaceStateStore


class FakeMemoryStore:
    def __init__(self):
        self.turns_cleared = 0
        self.recent_calls = 0
        self.memory_calls = 0

    def get_last_user_turn_ts(self):
        return None

    def get_completed_exchanges(self, limit=10):
        self.recent_calls += 1
        return [
            {
                "sequence": 1,
                "exchange_id": 1,
                "user_ts": "2026-08-26T00:00:00+00:00",
                "assistant_ts": "2026-08-26T00:00:01+00:00",
                "user_text": "old turn",
                "assistant_text": "old answer",
            }
        ]

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
    assert "RECENT COMPLETED EXCHANGES" in prompt
    assert "OPTIONAL SEARCHED LONG-TERM MEMORY" not in prompt
    assert store.recent_calls == 1
    assert store.memory_calls == 0

    store.recent_calls = 0
    store.memory_calls = 0
    assembler.saved_memory_injection_enabled = True
    prompt = assembler.build_prompt("hello", "deepseek-api")
    assert "RECENT COMPLETED EXCHANGES" in prompt
    assert "OPTIONAL SEARCHED LONG-TERM MEMORY" in prompt
    assert store.recent_calls == 1
    assert store.memory_calls == 1

    store.recent_calls = 0
    store.memory_calls = 0
    assembler.saved_memory_injection_enabled = False
    assembler.turns_injection_enabled = False
    prompt = assembler.build_prompt("hello", "deepseek-api")
    assert "RECENT COMPLETED EXCHANGES" not in prompt
    assert "OPTIONAL SEARCHED LONG-TERM MEMORY" not in prompt
    assert store.recent_calls == 0
    assert store.memory_calls == 0


def test_managed_prompt_preserves_typed_authority_without_flat_position_assertions(tmp_path):
    from orchestrator.context_compaction import MANAGED_HISTORY_TITLE

    system_md = tmp_path / "agent.md"
    system_md.write_text(
        render_pcm_document(persona="PERSONA", system="SYSTEM-POLICY"),
        encoding="utf-8",
    )
    store = FakeMemoryStore()
    assembler = BridgeContextAssembler(store, system_md=system_md)
    assembler.saved_memory_injection_enabled = True

    payload = assembler.build_prompt_payload(
        "CURRENT-REQUEST",
        "her-v2",
        extra_sections=[
            ("WORKZONE", "WORKZONE-POLICY"),
            (MANAGED_HISTORY_TITLE, "OLD-CAPSULE\nRECENT-TIMELINE"),
        ],
    )

    prompt = payload["final_prompt"]
    assert all(
        value in prompt
        for value in (
            "SYSTEM-POLICY",
            "WORKZONE-POLICY",
            "saved memory",
            "OLD-CAPSULE",
            "RECENT-TIMELINE",
            "CURRENT-REQUEST",
        )
    )
    ranks = {item["key"]: item["rank"] for item in payload["envelope"]["sections"]}
    assert ranks["permanent_system"] > ranks["current_user_request"]
    assert ranks["current_user_request"] > ranks[
        "extra:hashi_managed_conversation_history"
    ]


def _session_command_runtime(tmp_path, *, engine="openrouter-api", supports_sessions=False):
    runtime = FlexibleAgentRuntime.__new__(FlexibleAgentRuntime)
    runtime.name = "arale"
    runtime.workspace_dir = tmp_path / "workspaces" / "arale"
    runtime.workspace_dir.mkdir(parents=True)
    runtime.global_config = SimpleNamespace(
        bridge_home=tmp_path,
        project_root=tmp_path,
        instance_id="HASHI1",
        authorized_id=123,
    )
    runtime.config = SimpleNamespace(active_backend=engine)
    resets = []

    async def reset_backend():
        resets.append(True)

    runtime.backend_manager = SimpleNamespace(
        current_backend=SimpleNamespace(
            capabilities=SimpleNamespace(supports_sessions=supports_sessions),
            handle_new_session=reset_backend,
        )
    )
    runtime._authorized_telegram_ids = {123}
    runtime._backend_busy = lambda: False
    runtime._workzone_dir = None
    runtime._sync_workzone_to_backend_config = lambda: None
    runtime._clear_transfer_state = lambda: None
    runtime._context_compaction_tasks = set()
    runtime.context_assembler = SimpleNamespace(
        turns_injection_enabled=True,
        saved_memory_injection_enabled=True,
    )
    runtime.memory_store = BridgeMemoryStore(runtime.workspace_dir)
    replies = []

    async def reply(update, text, **kwargs):
        replies.append(text)

    runtime._reply_text = reply
    default = runtime_session.initialize_runtime_sessions(runtime)
    return runtime, default, replies, resets


@pytest.mark.asyncio
async def test_new_creates_and_binds_a_hashi_session_for_any_backend(tmp_path):
    runtime, default, replies, resets = _session_command_runtime(tmp_path)
    update = fake_update()

    await runtime.cmd_new(update, fake_context())

    current = runtime_session.current_session_for_update(runtime, update)
    assert current["session_id"] != default["session_id"]
    assert current["context_generation"] == 1
    assert len(runtime.session_store.list_sessions(
        owner_id="user:123", agent_id="arale"
    )) == 2
    assert replies[-1].startswith("New Session active:")
    assert resets == []


@pytest.mark.asyncio
async def test_new_resets_only_the_new_session_cli_binding(tmp_path):
    runtime, default, _replies, resets = _session_command_runtime(
        tmp_path, engine="codex-cli", supports_sessions=True
    )

    await runtime.cmd_new(fake_update(), fake_context())

    assert resets == [True]
    assert runtime.session_store.get_session(default["session_id"])["is_default"] is True


@pytest.mark.asyncio
async def test_whatsapp_session_command_binds_only_the_originating_chat(tmp_path):
    runtime, default, _replies, _resets = _session_command_runtime(tmp_path)
    chat_key = "61400000000@s.whatsapp.net"

    result = await execute_local_command(
        runtime,
        "/new",
        chat_id=chat_key,
        source_channel="whatsapp_forwarded",
    )

    assert result["ok"] is True
    whatsapp = runtime_session.current_session(
        runtime, surface="whatsapp", channel_key=chat_key
    )
    telegram = runtime_session.current_session(
        runtime, surface="telegram", channel_key=chat_key
    )
    assert whatsapp["session_id"] != default["session_id"]
    assert telegram["session_id"] == default["session_id"]


@pytest.mark.asyncio
async def test_workbench_slash_command_honors_explicit_session(tmp_path):
    runtime, default, _replies, _resets = _session_command_runtime(tmp_path)
    target = runtime.session_store.create_session(
        owner_id="user:123", agent_id="arale", title="Workbench target"
    )

    result = await execute_local_command(
        runtime,
        "/fresh",
        source_channel="api_chat",
        session_metadata={
            "session_id": target["session_id"],
            "owner_id": "user:123",
            "session_surface": "workbench",
            "session_channel_key": "window-a",
        },
    )

    assert result["ok"] is True
    assert runtime.session_store.get_session(target["session_id"])[
        "context_generation"
    ] == 2
    assert runtime.session_store.get_session(default["session_id"])[
        "context_generation"
    ] == 1


@pytest.mark.asyncio
async def test_queue_commands_only_show_and_clear_the_current_session(tmp_path):
    runtime, default, _replies, _resets = _session_command_runtime(tmp_path)
    other = runtime.session_store.create_session(
        owner_id="user:123", agent_id="arale", title="Other"
    )
    runtime.queue = asyncio.Queue()
    runtime.is_generating = False
    runtime.current_request_meta = None
    runtime.last_prompt = None
    runtime.last_response = None
    for request_id, prompt, session in (
        ("req-current", "CURRENT SESSION TASK", default),
        ("req-other", "OTHER SESSION SECRET", other),
    ):
        await runtime.queue.put(
            QueuedRequest(
                request_id=request_id,
                chat_id=456,
                prompt=prompt,
                source="text",
                summary=prompt,
                created_at=datetime.now().isoformat(),
                session_id=session["session_id"],
            )
        )

    listed = await execute_local_command(
        runtime, "/queue", chat_id=456, source_channel="telegram"
    )
    text = listed["messages"][0]["text"]
    assert "CURRENT SESSION TASK" in text
    assert "OTHER SESSION SECRET" not in text

    await execute_local_command(
        runtime, "/queue clear", chat_id=456, source_channel="telegram"
    )
    assert [item.request_id for item in runtime.queue._queue] == ["req-other"]


@pytest.mark.asyncio
async def test_stop_from_one_session_does_not_interrupt_another_session(tmp_path):
    runtime, default, replies, _resets = _session_command_runtime(tmp_path)
    other = runtime.session_store.create_session(
        owner_id="user:123", agent_id="arale", title="Other"
    )
    shutdown = AsyncMock()
    runtime.queue = asyncio.Queue()
    runtime.logger = SimpleNamespace(warning=lambda *_args, **_kwargs: None)
    runtime.is_generating = True
    runtime.current_request_meta = {
        "request_id": "req-running-other",
        "prompt": "OTHER SESSION ACTIVE TASK",
        "source": "text",
        "hashi_session_id": other["session_id"],
    }
    runtime.backend_manager.current_backend.shutdown = shutdown
    await runtime.queue.put(
        QueuedRequest(
            request_id="req-current-waiting",
            chat_id=456,
            prompt="current waiting",
            source="text",
            summary="current waiting",
            created_at=datetime.now().isoformat(),
            session_id=default["session_id"],
        )
    )

    await runtime.cmd_stop(fake_update(), fake_context())

    shutdown.assert_not_awaited()
    assert runtime.queue.empty()
    assert runtime.current_request_meta["request_id"] == "req-running-other"
    assert "other Session continues" in replies[-1]


@pytest.mark.asyncio
async def test_fresh_starts_new_generation_without_deleting_session_or_agent_memory(tmp_path):
    runtime, session, replies, _resets = _session_command_runtime(tmp_path)
    store = runtime.session_store
    accepted = store.accept_run(
        session_id=session["session_id"],
        owner_id="user:123",
        agent_id="arale",
        request_id="req-before-fresh",
        text="OLD SESSION CONTENT",
        source="text",
        idempotency_key="before-fresh",
    )
    store.mark_request_running("req-before-fresh", worker_id="test")
    store.finish_request(
        "req-before-fresh",
        success=True,
        assistant_text="OLD SESSION ANSWER",
        assistant_source="test",
    )
    runtime.memory_store.record_memory(
        "episodic", "promoted", "PROMOTED AGENT MEMORY"
    )

    await runtime.cmd_fresh(fake_update(), fake_context())

    updated = store.get_session(session["session_id"])
    assert updated["context_generation"] == 2
    assert store.recent_exchanges(session["session_id"]) == []
    assert [row["text"] for row in store.messages(session["session_id"])] == [
        "OLD SESSION CONTENT",
        "OLD SESSION ANSWER",
    ]
    assert runtime.memory_store.retrieve_memories("PROMOTED AGENT MEMORY")
    assert accepted.context_generation == 1
    assert replies[-1].startswith("Fresh context generation 2 started")


def test_dual_brain_remains_eligible_after_session_commands(tmp_path):
    workspace = tmp_path / "sakura"
    workspace.mkdir()
    (workspace / "state.json").write_text(
        json.dumps({"agent_mode": "dual-brain"}), encoding="utf-8"
    )
    observer = DualBrainObserver(
        workspace_dir=workspace,
        backend_invoker=lambda *args, **kwargs: None,
        backend_context_getter=lambda: {
            "engine": "openrouter-api",
            "model": "test-model",
        },
    )

    assert not observer.should_provide("session_reset", is_bridge_request=False)
    assert observer.should_provide("api", is_bridge_request=False)


def test_pending_primer_is_consumed_only_by_its_session(tmp_path):
    runtime, default, _replies, _resets = _session_command_runtime(tmp_path)
    other = runtime.session_store.create_session(
        owner_id="user:123", agent_id="arale", title="Other"
    )
    runtime._pending_session_primer = "SESSION_A_PRIMER"
    runtime._pending_session_primer_session_id = default["session_id"]
    runtime._pending_auto_recall_context = "SESSION_A_RECALL"
    runtime._pending_auto_recall_session_id = default["session_id"]

    other_item = SimpleNamespace(
        source="text", silent=False, prompt="other", session_id=other["session_id"]
    )
    assert runtime._consume_session_primer(other_item) == "other"
    assert runtime._pending_session_primer == "SESSION_A_PRIMER"

    default_item = SimpleNamespace(
        source="text", silent=False, prompt="default", session_id=default["session_id"]
    )
    rendered = runtime._consume_session_primer(default_item)
    assert "SESSION_A_PRIMER" in rendered
    assert "SESSION_A_RECALL" in rendered


def test_command_sources_follow_telegram_session_and_scheduled_work_uses_default(tmp_path):
    runtime, default, _replies, _resets = _session_command_runtime(tmp_path)
    other = runtime.session_store.create_session(
        owner_id="user:123", agent_id="arale", title="Other"
    )
    runtime.session_store.bind_channel(
        owner_id="user:123",
        agent_id="arale",
        surface="telegram",
        channel_key="456",
        session_id=other["session_id"],
    )

    skill_session, *_ = runtime_session.resolve_request_session(
        runtime, source="skill:research", chat_id=456
    )
    scheduled_session, *_ = runtime_session.resolve_request_session(
        runtime, source="scheduler-skill", chat_id=456
    )

    assert skill_session["session_id"] == other["session_id"]
    assert scheduled_session["session_id"] == default["session_id"]


def test_promotion_unifies_completed_session_exchanges_idempotently(tmp_path):
    runtime, default, _replies, _resets = _session_command_runtime(tmp_path)
    other = runtime.session_store.create_session(
        owner_id="user:123", agent_id="arale", title="Other"
    )
    origin_refs = []
    for index, (session, marker) in enumerate(
        ((default, "DEFAULT_MEMORY"), (other, "OTHER_MEMORY")), start=1
    ):
        request_id = f"req-promote-{index}"
        accepted = runtime.session_store.accept_run(
            session_id=session["session_id"],
            owner_id="user:123",
            agent_id="arale",
            request_id=request_id,
            text=marker,
            source="text",
            idempotency_key=request_id,
        )
        runtime.session_store.mark_request_running(request_id, worker_id="test")
        runtime.session_store.finish_request(
            request_id,
            success=True,
            assistant_text=f"answer {marker}",
            assistant_source="test",
        )
        origin_refs.append(
            f"session:{session['session_id']}:run:{accepted.run_id}"
        )

    first = runtime_session.promote_sessions(runtime, trigger="test")
    second = runtime_session.promote_sessions(runtime, trigger="test")

    assert first["promoted_count"] == 2
    assert first["pending_count"] == 0
    assert second["promoted_count"] == 0
    assert all(runtime.memory_store.memory_origin_exists(ref) for ref in origin_refs)
    memories = runtime.memory_store.retrieve_memories("DEFAULT_MEMORY OTHER_MEMORY", limit=10)
    rendered = "\n".join(row["content"] for row in memories)
    assert "DEFAULT_MEMORY" in rendered
    assert "OTHER_MEMORY" in rendered


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
