from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from orchestrator import runtime_control
from orchestrator.runtime_command_binding import BOT_COMMAND_BINDINGS, COMMAND_BINDINGS


def _update(chat_id: int = 42) -> SimpleNamespace:
    message = SimpleNamespace(text="/focus", chat=SimpleNamespace(id=chat_id))
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=1),
        effective_chat=SimpleNamespace(id=chat_id),
        effective_message=message,
        message=message,
    )


def test_focus_is_registered_and_prompt_enforces_user_scope():
    assert any(b.name == "focus" and b.method_name == "cmd_focus" for b in COMMAND_BINDINGS)
    focus_binding = next(b for b in BOT_COMMAND_BINDINGS if b.name == "focus")
    assert focus_binding.description == "Narrow scope and continue the original task"

    prompt = runtime_control.build_focus_prompt(
        original_prompt="Import the specified update into Aptenra HASHI",
        backend="codex-cli",
    )
    assert "[HASHI /focus" in prompt
    assert "scope correction, not a stop, pause, cancellation" in prompt
    assert "original user task shown below as the complete source of authority" in prompt
    assert "Take the next concrete, in-scope action" in prompt
    assert "until the original requested outcome is genuinely complete" in prompt
    assert "Do not stop merely because the task is difficult" in prompt
    assert "Otherwise, continue working without asking for confirmation" in prompt
    assert "memories, open items" in prompt
    assert "Import the specified update" in prompt
    assert "codex-cli" in prompt


def test_repeated_focus_keeps_one_clean_copy_of_the_original_task():
    first = runtime_control.build_focus_prompt(
        original_prompt="Implement only the requested parser fix",
        backend="codex-cli",
    )
    second = runtime_control.build_focus_prompt(
        original_prompt=first,
        backend="codex-cli",
    )

    assert second.count("[HASHI /focus") == 1
    assert second.count("Implement only the requested parser fix") == 1
    assert second.count("--- Original user task") == 1


@pytest.mark.asyncio
async def test_cmd_focus_busy_interrupts_preserves_context_and_uses_focus_source():
    replies: list[str] = []
    enqueued: list[tuple] = []
    shutdown = AsyncMock()

    async def _reply(_update, text, **_kwargs):
        replies.append(text)

    async def _enqueue(chat_id, prompt, source, summary, **_kwargs):
        enqueued.append((chat_id, prompt, source, summary))
        return "req-focus-1"

    queue: asyncio.Queue = asyncio.Queue()
    await queue.put(object())
    runtime = SimpleNamespace(
        name="zelda",
        logger=SimpleNamespace(warning=lambda *a, **k: None),
        config=SimpleNamespace(active_backend="codex-cli", engine="codex-cli"),
        queue=queue,
        backend_manager=SimpleNamespace(
            current_backend=SimpleNamespace(shutdown=shutdown),
            initialize_active_backend=AsyncMock(return_value=True),
        ),
        current_request_meta={
            "request_id": "req-old",
            "chat_id": 42,
            "prompt": "Import only update 7a1b41d",
            "source": "text",
            "summary": "Import update",
        },
        last_prompt=SimpleNamespace(prompt="Import only update 7a1b41d", chat_id=42),
        is_generating=True,
        _is_authorized_user=lambda _uid: True,
        _reply_text=_reply,
        _notify_right_brain_interrupted=lambda *a, **k: None,
        enqueue_request=_enqueue,
    )

    await runtime_control.cmd_focus(runtime, _update(), SimpleNamespace(args=[]))

    shutdown.assert_awaited_once()
    assert queue.empty()
    assert len(enqueued) == 1
    chat_id, prompt, source, summary = enqueued[0]
    assert chat_id == 42
    assert source == "focus"
    assert "Import only update 7a1b41d" in prompt
    assert "Do not create extra branches" in prompt
    assert "Focus:" in summary
    assert replies and "Focus applied" in replies[0]
    assert "task must continue until its requested outcome is complete" in replies[0]
    assert runtime._user_interrupt["reason"] == "user_focus"
    assert runtime_control.consume_user_interrupt(runtime, "req-old") == "user_focus"


@pytest.mark.asyncio
async def test_cmd_focus_idle_uses_most_recent_task_without_interrupting_backend():
    replies: list[str] = []
    enqueued: list[tuple] = []
    shutdown = AsyncMock()

    async def _reply(_update, text, **_kwargs):
        replies.append(text)

    async def _enqueue(chat_id, prompt, source, summary, **_kwargs):
        enqueued.append((chat_id, prompt, source, summary))
        return "req-focus-idle"

    runtime = SimpleNamespace(
        name="zelda",
        logger=SimpleNamespace(warning=lambda *a, **k: None),
        config=SimpleNamespace(active_backend="codex-cli", engine="codex-cli"),
        queue=asyncio.Queue(),
        backend_manager=SimpleNamespace(
            current_backend=SimpleNamespace(shutdown=shutdown),
            initialize_active_backend=AsyncMock(return_value=True),
        ),
        current_request_meta=None,
        last_prompt=SimpleNamespace(prompt="Review this pull request only", chat_id=42),
        is_generating=False,
        _is_authorized_user=lambda _uid: True,
        _reply_text=_reply,
        enqueue_request=_enqueue,
    )

    await runtime_control.cmd_focus(runtime, _update(), SimpleNamespace(args=[]))

    shutdown.assert_not_awaited()
    assert len(enqueued) == 1
    assert enqueued[0][2] == "focus"
    assert "Review this pull request only" in enqueued[0][1]
    assert replies and "most recent task" in replies[0]
    assert "must keep working on the original outcome" in replies[0]
    assert getattr(runtime, "_user_interrupt", None) is None


@pytest.mark.asyncio
async def test_cmd_focus_idle_without_recent_task_is_noop():
    replies: list[str] = []
    enqueue = AsyncMock()

    async def _reply(_update, text, **_kwargs):
        replies.append(text)

    runtime = SimpleNamespace(
        name="zelda",
        logger=SimpleNamespace(warning=lambda *a, **k: None),
        config=SimpleNamespace(active_backend="codex-cli", engine="codex-cli"),
        queue=asyncio.Queue(),
        backend_manager=SimpleNamespace(current_backend=None),
        current_request_meta=None,
        last_prompt=None,
        is_generating=False,
        _is_authorized_user=lambda _uid: True,
        _reply_text=_reply,
        enqueue_request=enqueue,
    )

    await runtime_control.cmd_focus(runtime, _update(), SimpleNamespace(args=[]))

    enqueue.assert_not_awaited()
    assert replies and "already idle" in replies[0]
