from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from orchestrator import runtime_control
from orchestrator.runtime_command_binding import BOT_COMMAND_BINDINGS, COMMAND_BINDINGS


def test_steer_is_registered_in_command_and_telegram_menus():
    assert any(b.name == "steer" and b.method_name == "cmd_steer" for b in COMMAND_BINDINGS)
    assert any(b.name == "steer" for b in BOT_COMMAND_BINDINGS)
    binding = next(b for b in BOT_COMMAND_BINDINGS if b.name == "steer")
    assert "direction" in binding.description.lower() or "continue" in binding.description.lower()


def test_extract_steer_direction_preserves_text():
    msg = SimpleNamespace(text="/steer also include unit tests for auth")
    update = SimpleNamespace(effective_message=msg, message=None)
    context = SimpleNamespace(args=["also", "include", "unit", "tests", "for", "auth"])
    assert runtime_control.extract_steer_direction(update, context) == "also include unit tests for auth"

    msg2 = SimpleNamespace(text="/steer@XishiBot keep artefacts\nand add logging")
    update2 = SimpleNamespace(effective_message=msg2, message=None)
    direction = runtime_control.extract_steer_direction(update2, SimpleNamespace(args=[]))
    assert "keep artefacts" in direction
    assert "add logging" in direction


def test_build_steer_prompt_keeps_original_and_forbids_reset():
    prompt = runtime_control.build_steer_prompt(
        direction="also include xxx in your tasks",
        original_prompt="Build the OAuth login flow",
        backend="claw-cli",
    )
    assert "[HASHI /steer" in prompt
    assert "also include xxx in your tasks" in prompt
    assert "Build the OAuth login flow" in prompt
    assert "KEEP all interim progress" in prompt
    assert "Do NOT call session-reset" in prompt
    assert "claw-cli" in prompt


@pytest.mark.asyncio
async def test_cmd_steer_requires_direction():
    replies: list[str] = []

    async def _reply(_update, text, **_kwargs):
        replies.append(text)

    runtime = SimpleNamespace(
        name="xishi",
        logger=SimpleNamespace(warning=lambda *a, **k: None),
        config=SimpleNamespace(active_backend="codex-cli", engine="codex-cli"),
        queue=asyncio.Queue(),
        backend_manager=SimpleNamespace(current_backend=None),
        current_request_meta=None,
        last_prompt=None,
        is_generating=False,
        _is_authorized_user=lambda _uid: True,
        _reply_text=_reply,
        enqueue_request=AsyncMock(),
    )
    msg = SimpleNamespace(text="/steer", chat=SimpleNamespace(id=42))
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=1),
        effective_chat=SimpleNamespace(id=42),
        effective_message=msg,
        message=msg,
    )
    await runtime_control.cmd_steer(runtime, update, SimpleNamespace(args=[]))
    assert replies
    assert "Usage: /steer" in replies[0]
    runtime.enqueue_request.assert_not_awaited()


@pytest.mark.asyncio
async def test_cmd_steer_stops_clears_queue_and_enqueues_continuation():
    replies: list[str] = []
    shutdown = AsyncMock()
    enqueued: list[tuple] = []

    async def _reply(_update, text, **_kwargs):
        replies.append(text)

    async def _enqueue(chat_id, prompt, source, summary, **_kwargs):
        enqueued.append((chat_id, prompt, source, summary))
        return "req-steer-1"

    queue: asyncio.Queue = asyncio.Queue()
    await queue.put(object())
    await queue.put(object())

    runtime = SimpleNamespace(
        name="xishi",
        logger=SimpleNamespace(warning=lambda *a, **k: None),
        config=SimpleNamespace(active_backend="grok-cli", engine="grok-cli"),
        queue=queue,
        backend_manager=SimpleNamespace(
            current_backend=SimpleNamespace(shutdown=shutdown),
            initialize_active_backend=AsyncMock(return_value=True),
        ),
        current_request_meta={
            "request_id": "req-old",
            "chat_id": 42,
            "prompt": "Implement feature X end to end",
            "source": "text",
            "summary": "Feature X",
        },
        last_prompt=SimpleNamespace(prompt="Implement feature X end to end", chat_id=42),
        is_generating=True,
        _is_authorized_user=lambda _uid: True,
        _reply_text=_reply,
        _notify_right_brain_interrupted=lambda *a, **k: None,
        enqueue_request=_enqueue,
    )
    text = "/steer also include xxx in your tasks"
    msg = SimpleNamespace(text=text, chat=SimpleNamespace(id=42))
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=1),
        effective_chat=SimpleNamespace(id=42),
        effective_message=msg,
        message=msg,
    )
    await runtime_control.cmd_steer(
        runtime,
        update,
        SimpleNamespace(args=["also", "include", "xxx", "in", "your", "tasks"]),
    )

    shutdown.assert_awaited()
    assert queue.empty()
    assert len(enqueued) == 1
    chat_id, prompt, source, summary = enqueued[0]
    assert chat_id == 42
    assert source == "steer"
    assert "also include xxx" in prompt
    assert "Implement feature X end to end" in prompt
    assert "KEEP all interim progress" in prompt
    assert replies and "Steered" in replies[0]
    assert "req-steer-1" in replies[0]
