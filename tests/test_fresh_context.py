from types import SimpleNamespace
import sys
import types

import pytest

sys.modules.setdefault("edge_tts", types.ModuleType("edge_tts"))

from orchestrator.agent_runtime import BridgeAgentRuntime
from orchestrator.bridge_memory import BridgeContextAssembler
from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime


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
    assert "RELEVANT LONG-TERM MEMORY" in prompt
    assert store.recent_calls == 1
    assert store.memory_calls == 1

    store.recent_calls = 0
    store.memory_calls = 0
    assembler.saved_memory_injection_enabled = False
    prompt = assembler.build_prompt("hello", "deepseek-api")
    assert "RECENT CONTEXT" in prompt
    assert "RELEVANT LONG-TERM MEMORY" not in prompt
    assert store.recent_calls == 1
    assert store.memory_calls == 0

    store.recent_calls = 0
    assembler.turns_injection_enabled = False
    prompt = assembler.build_prompt("hello", "deepseek-api")
    assert "RECENT CONTEXT" not in prompt
    assert "RELEVANT LONG-TERM MEMORY" not in prompt
    assert store.recent_calls == 0


@pytest.mark.asyncio
async def test_fixed_runtime_new_is_guarded_for_non_cli_backend():
    runtime = BridgeAgentRuntime.__new__(BridgeAgentRuntime)
    runtime.global_config = SimpleNamespace(authorized_id=123)
    runtime.config = SimpleNamespace(engine="deepseek-api")

    update = fake_update()
    await runtime.cmd_new(update, fake_context())

    assert "Use /fresh" in update.message.replies[0]


@pytest.mark.asyncio
async def test_fixed_runtime_fresh_clears_turns_and_disables_saved_memory():
    runtime = BridgeAgentRuntime.__new__(BridgeAgentRuntime)
    runtime.global_config = SimpleNamespace(authorized_id=123)
    runtime.config = SimpleNamespace(engine="deepseek-api")
    runtime._pending_auto_recall_context = "old"
    store = FakeMemoryStore()
    runtime.memory_store = store
    runtime.context_assembler = BridgeContextAssembler(store, system_md=None)
    enqueued = []

    async def enqueue_request(*args, **kwargs):
        enqueued.append((args, kwargs))

    runtime.enqueue_request = enqueue_request

    update = fake_update()
    await runtime.cmd_fresh(update, fake_context())

    assert store.turns_cleared == 1
    assert runtime.context_assembler.turns_injection_enabled is True
    assert runtime.context_assembler.saved_memory_injection_enabled is False
    assert runtime._pending_auto_recall_context is None
    assert enqueued[0][1]["skip_memory_injection"] is True


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
async def test_flex_runtime_fresh_clears_turns_and_skips_memory_on_prompt():
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
    assert enqueued[0][1]["skip_memory_injection"] is True
