from __future__ import annotations

from types import SimpleNamespace
import sys
import types

import pytest

sys.modules.setdefault("edge_tts", types.ModuleType("edge_tts"))

from orchestrator.bridge_memory import BridgeContextAssembler
from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime
from orchestrator import runtime_session


class FakeMemoryStore:
    def __init__(self):
        self.turns_cleared = 0

    def clear_turns(self):
        self.turns_cleared += 1
        return 1


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

    await runtime_session.cmd_new(runtime, fake_update(), fake_context())

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

    await runtime_session.cmd_fresh(runtime, fake_update(), fake_context())

    assert store.turns_cleared == 1
    assert runtime.context_assembler.turns_injection_enabled is True
    assert runtime.context_assembler.saved_memory_injection_enabled is False
    assert runtime._pending_auto_recall_context is None
    assert enqueued[0][1]["skip_memory_injection"] is True


@pytest.mark.asyncio
async def test_flex_runtime_fresh_is_guarded_for_cli_backend():
    runtime = FlexibleAgentRuntime.__new__(FlexibleAgentRuntime)
    runtime._authorized_telegram_ids = {123}
    runtime.config = SimpleNamespace(active_backend="codex-cli")
    runtime.backend_manager = SimpleNamespace(current_backend=object())
    replies = []

    async def reply(update, text, **kwargs):
        replies.append(text)

    runtime._reply_text = reply

    await runtime_session.cmd_fresh(runtime, fake_update(), fake_context())

    assert "Use /new" in replies[0]
