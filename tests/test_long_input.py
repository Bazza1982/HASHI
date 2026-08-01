from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from orchestrator import runtime_long_input
from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime


def _runtime() -> FlexibleAgentRuntime:
    runtime = FlexibleAgentRuntime.__new__(FlexibleAgentRuntime)
    runtime.name = "lily"
    runtime._long_buffer = []
    runtime._long_buffer_active = True
    runtime._long_buffer_chat_id = 123
    runtime._long_buffer_session_id = "long-1"
    runtime._long_buffer_timeout_task = None
    runtime._pending_voice = {}
    runtime._is_authorized_user = Mock(return_value=True)
    runtime._reply_text = AsyncMock()
    runtime.enqueue_request = AsyncMock()
    return runtime


def test_long_buffer_accepts_only_matching_chat_and_session():
    runtime = _runtime()

    assert runtime._buffer_long_chunk(123, "first", session_id="long-1") is True
    assert runtime._buffer_long_chunk(999, "wrong chat", session_id="long-1") is False
    assert runtime._buffer_long_chunk(123, "stale", session_id="long-old") is False
    assert runtime._long_buffer == ["first"]


@pytest.mark.asyncio
async def test_end_submits_text_and_media_as_one_request(monkeypatch):
    runtime = _runtime()
    runtime._long_buffer = [
        "Please compare these inputs.",
        "[Photo]\nUser sent a photo (saved at /tmp/photo.jpg). View the image and respond.",
    ]
    monkeypatch.setattr(runtime_long_input, "_print_user_message", lambda *_args, **_kwargs: None)
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=1),
        effective_chat=SimpleNamespace(id=123),
    )

    await runtime.cmd_end(update, SimpleNamespace())

    runtime._reply_text.assert_awaited_once_with(
        update,
        "✅ Collected 2 items. Submitting...",
    )
    runtime.enqueue_request.assert_awaited_once_with(
        123,
        (
            "Please compare these inputs.\n"
            "[Photo]\nUser sent a photo (saved at /tmp/photo.jpg). "
            "View the image and respond."
        ),
        "text",
        "Please compare these inputs. [Photo] User sent a photo (saved at /tmp/photo.jpg). View the image and respond.",
    )
    assert runtime._long_buffer_active is False
    assert runtime._long_buffer_session_id is None


@pytest.mark.asyncio
async def test_end_waits_for_pending_safe_voice_confirmation():
    runtime = _runtime()
    runtime._long_buffer = ["Please review this."]
    runtime._pending_voice = {
        "123": {
            "prompt": "[Voice message transcription] Include this too.",
            "transcript": "Include this too.",
            "summary": "Voice: voice.ogg",
            "long_session_id": "long-1",
        }
    }
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=1),
        effective_chat=SimpleNamespace(id=123),
    )

    await runtime.cmd_end(update, SimpleNamespace())

    runtime._reply_text.assert_awaited_once_with(
        update,
        "⚠️ Confirm or discard the pending Safe Voice transcript before /end.",
    )
    runtime.enqueue_request.assert_not_awaited()
    assert runtime._long_buffer_active is True
    assert runtime._long_buffer == ["Please review this."]


@pytest.mark.asyncio
async def test_safe_voice_confirmation_adds_transcript_to_original_long_session():
    runtime = _runtime()
    runtime._pending_voice = {
        "123": {
            "prompt": "[Voice message transcription] Review both files.",
            "transcript": "Review both files.",
            "summary": "Voice: voice.ogg",
            "long_session_id": "long-1",
        }
    }
    query = SimpleNamespace(
        from_user=SimpleNamespace(id=1),
        data="safevoice:yes:123",
        edit_message_text=AsyncMock(),
        answer=AsyncMock(),
    )
    update = SimpleNamespace(callback_query=query)

    await runtime.callback_safevoice(update, SimpleNamespace())

    assert runtime._long_buffer == [
        "[Voice message transcription] Review both files."
    ]
    runtime.enqueue_request.assert_not_awaited()
    assert "Added to /long buffer" in query.edit_message_text.await_args.args[0]
    query.answer.assert_awaited_once_with("Added to /long")


@pytest.mark.asyncio
async def test_late_safe_voice_confirmation_sends_separately():
    runtime = _runtime()
    runtime._long_buffer_session_id = "long-new"
    runtime._pending_voice = {
        "123": {
            "prompt": "[Voice message transcription] Late transcript.",
            "transcript": "Late transcript.",
            "summary": "Voice: voice.ogg",
            "long_session_id": "long-old",
        }
    }
    query = SimpleNamespace(
        from_user=SimpleNamespace(id=1),
        data="safevoice:yes:123",
        edit_message_text=AsyncMock(),
        answer=AsyncMock(),
    )

    await runtime.callback_safevoice(
        SimpleNamespace(callback_query=query),
        SimpleNamespace(),
    )

    assert runtime._long_buffer == []
    runtime.enqueue_request.assert_awaited_once_with(
        123,
        "[Voice message transcription] Late transcript.",
        "voice_transcript",
        "Voice: voice.ogg",
    )
    assert "original /long session has ended" in query.edit_message_text.await_args.args[0]
