import asyncio
from types import SimpleNamespace

import pytest

from orchestrator import runtime_long
from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime


def _update(chat_id: int = 123, user_id: int = 1):
    return SimpleNamespace(
        effective_chat=SimpleNamespace(id=chat_id),
        effective_user=SimpleNamespace(id=user_id),
    )


def _runtime():
    replies = []
    enqueued = []
    delivered = []
    runtime = SimpleNamespace(
        name="lily",
        _long_buffer=[],
        _long_buffer_kinds=[],
        _long_buffer_summaries=[],
        _long_buffer_ids=[],
        _long_buffer_metadata=[],
        _long_buffer_active=False,
        _long_buffer_state="idle",
        _long_buffer_chat_id=None,
        _long_batch_id=None,
        _long_buffer_timeout_task=None,
        _long_finalize_task=None,
        _long_finalize_update=None,
        _long_finalize_reason=None,
        _long_pending_media_ids=set(),
        _long_batch_quiet_seconds=0,
        _long_pending_voice_keys=set(),
        _pending_voice={},
    )
    runtime._is_authorized_user = lambda user_id: user_id == 1

    async def reply_text(update, text, **kwargs):
        replies.append({"text": text, **kwargs})

    async def enqueue_request(chat_id, prompt, source, summary, **kwargs):
        enqueued.append(
            {
                "chat_id": chat_id,
                "prompt": prompt,
                "source": source,
                "summary": summary,
                **kwargs,
            }
        )
        return "req-0001"

    async def send_long_message(chat_id, text, **kwargs):
        delivered.append({"chat_id": chat_id, "text": text, **kwargs})

    runtime._reply_text = reply_text
    runtime.enqueue_request = enqueue_request
    runtime.send_long_message = send_long_message
    runtime.replies = replies
    runtime.enqueued = enqueued
    runtime.delivered = delivered
    return runtime


def test_text_only_batch_preserves_original_prompt_shape():
    runtime = _runtime()
    runtime_long.begin_batch(runtime, 123)

    assert runtime_long.collect_text(runtime, 123, "first\nsecond") is True
    assert runtime_long.collect_text(runtime, 123, "third") is True

    submission = runtime_long.consume_batch(runtime, 123)

    assert submission is not None
    assert submission.prompt == "first\nsecond\nthird"
    assert submission.source == "text"
    assert submission.text_count == 2
    assert submission.media_count == 0
    assert submission.line_count == 3


def test_multimodal_batch_preserves_item_order_and_requests_one_response():
    runtime = _runtime()
    runtime_long.begin_batch(runtime, 123)

    runtime_long.collect_text(runtime, 123, "Compare everything and flag inconsistencies.")
    runtime_long.collect_media(
        runtime,
        123,
        'User sent a PDF document "contract.pdf" (saved at /tmp/contract.pdf). Extract the text, analyze the contents thoroughly, and respond.',
        "document",
        "contract.pdf",
    )
    runtime_long.collect_media(
        runtime,
        123,
        "User sent a photo (saved at /tmp/photo.jpg). View the image and respond.",
        "photo",
        "photo.jpg",
    )
    runtime_long.collect_text(runtime, 123, "Return one risk summary.")

    submission = runtime_long.consume_batch(runtime, 123)

    assert submission is not None
    assert submission.source == "multimodal"
    assert submission.text_count == 2
    assert submission.media_count == 2
    assert "Return one consolidated response" in submission.prompt
    ordered_markers = [
        "[Item 1 — User text]",
        "[Item 2 — Document: contract.pdf]",
        "[Item 3 — Photo: photo.jpg]",
        "[Item 4 — User text]",
    ]
    positions = [submission.prompt.index(marker) for marker in ordered_markers]
    assert positions == sorted(positions)
    assert "/tmp/contract.pdf" in submission.prompt
    assert "/tmp/photo.jpg" in submission.prompt
    assert "and respond" not in submission.prompt
    assert "do not inspect an attachment merely because it exists" in submission.prompt
    assert "Each Transport receipt below proves intake only" in submission.prompt
    assert "Read or inspect every referenced file before replying" not in submission.prompt


@pytest.mark.asyncio
async def test_cmd_end_enqueues_media_batch_once():
    runtime = _runtime()
    runtime_long.begin_batch(runtime, 123)
    runtime_long.collect_text(runtime, 123, "Review these together.")
    runtime_long.collect_media(
        runtime,
        123,
        'User sent a PDF document "a.pdf" (saved at /tmp/a.pdf). Read it.',
        "document",
        "a.pdf",
    )

    await runtime_long.cmd_end(runtime, _update(), SimpleNamespace())

    assert len(runtime.enqueued) == 1
    assert runtime.enqueued[0]["source"] == "multimodal"
    assert "Review these together." in runtime.enqueued[0]["prompt"]
    assert "/tmp/a.pdf" in runtime.enqueued[0]["prompt"]
    assert runtime._long_buffer_active is False
    assert "Submitting as one request" in runtime.replies[-1]["text"]


@pytest.mark.asyncio
async def test_cmd_end_submits_media_only_batch():
    runtime = _runtime()
    runtime_long.begin_batch(runtime, 123)
    runtime_long.collect_media(
        runtime,
        123,
        "User sent a photo (saved at /tmp/photo.jpg). View it.",
        "photo",
        "photo.jpg",
    )

    await runtime_long.cmd_end(runtime, _update(), SimpleNamespace())

    assert len(runtime.enqueued) == 1
    assert runtime.enqueued[0]["source"] == "multimodal"
    assert "If there is no explicit task text" in runtime.enqueued[0]["prompt"]


@pytest.mark.asyncio
async def test_cmd_end_rejects_empty_batch_without_enqueuing():
    runtime = _runtime()
    runtime_long.begin_batch(runtime, 123)

    await runtime_long.cmd_end(runtime, _update(), SimpleNamespace())

    assert runtime.enqueued == []
    assert runtime._long_buffer_active is False
    assert runtime.replies[-1]["text"] == "⚠️ /long buffer was empty, nothing to submit."


@pytest.mark.asyncio
async def test_cmd_end_waits_for_safevoice_confirmation():
    runtime = _runtime()
    runtime_long.begin_batch(runtime, 123)
    runtime_long.collect_text(runtime, 123, "Use the voice note too.")
    pending_key = runtime_long.register_voice_confirmation(
        runtime,
        chat_id=123,
        prompt="[Voice message transcription] hello",
        transcript="hello",
        summary="Voice: note.ogg",
    )

    await runtime_long.cmd_end(runtime, _update(), SimpleNamespace())

    assert pending_key is not None
    assert runtime.enqueued == []
    assert runtime._long_buffer_active is True
    assert "Confirm or discard 1 pending voice transcript" in runtime.replies[-1]["text"]


@pytest.mark.asyncio
async def test_cmd_end_waits_for_reserved_media_before_finalizing():
    runtime = _runtime()
    runtime._long_batch_quiet_seconds = 0.01
    runtime_long.begin_batch(runtime, 123, "Compare the incoming image.")
    reservation_id = runtime_long.reserve_media(
        runtime,
        123,
        "photo",
        "incoming.jpg",
        transport_metadata={"message_id": 77, "media_group_id": "album-1"},
    )

    await runtime_long.cmd_end(runtime, _update(), SimpleNamespace())
    await asyncio.sleep(0.03)

    assert reservation_id is not None
    assert runtime.enqueued == []
    assert runtime._long_buffer_state == runtime_long.LONG_BATCH_CLOSING

    assert runtime_long.complete_media(
        runtime,
        reservation_id,
        "User sent a photo (saved at /tmp/incoming.jpg). View the image and respond.",
    ) is True
    await asyncio.sleep(0.03)

    assert len(runtime.enqueued) == 1
    metadata = runtime.enqueued[0]["request_metadata"]
    assert metadata["media_count"] == 1
    assert metadata["attachment_receipts"][0]["status"] == "received"
    assert metadata["attachment_receipts"][0]["media_group_id"] == "album-1"


@pytest.mark.asyncio
async def test_timeout_submits_confirmed_items_and_discards_pending_voice(monkeypatch):
    runtime = _runtime()
    runtime_long.begin_batch(runtime, 123)
    runtime_long.collect_media(
        runtime,
        123,
        'User sent a PDF document "a.pdf" (saved at /tmp/a.pdf). Read it.',
        "document",
        "a.pdf",
    )
    runtime_long.register_voice_confirmation(
        runtime,
        chat_id=123,
        prompt="[Voice message transcription] pending",
        transcript="pending",
        summary="Voice: pending.ogg",
    )

    async def no_wait(_seconds):
        return None

    monkeypatch.setattr(runtime_long.asyncio, "sleep", no_wait)

    await runtime_long.long_buffer_timeout(runtime)

    assert len(runtime.enqueued) == 1
    assert runtime.enqueued[0]["source"] == "multimodal"
    assert len(runtime.delivered) == 1
    assert "discarded 1 unconfirmed voice transcript" in runtime.delivered[0]["text"]
    assert runtime._pending_voice == {}


def test_long_batch_is_scoped_to_starting_chat():
    runtime = _runtime()
    runtime_long.begin_batch(runtime, 123)

    assert runtime_long.collect_text(runtime, 999, "other chat") is False
    assert runtime_long.collect_media(runtime, 999, "other media", "photo", "x.jpg") is False
    assert runtime._long_buffer == []


@pytest.mark.asyncio
async def test_safevoice_callback_confirms_into_batch_without_enqueuing():
    runtime = _runtime()
    runtime_long.begin_batch(runtime, 123)
    pending_key = runtime_long.register_voice_confirmation(
        runtime,
        chat_id=123,
        prompt="[Voice message transcription] hello batch",
        transcript="hello batch",
        summary="Voice: note.ogg",
    )
    edits = []
    answers = []

    class _Query:
        data = f"safevoice:yes:{pending_key}"
        from_user = SimpleNamespace(id=1)

        async def edit_message_text(self, text, **kwargs):
            edits.append({"text": text, **kwargs})

        async def answer(self, text):
            answers.append(text)

    update = SimpleNamespace(callback_query=_Query())

    await FlexibleAgentRuntime.callback_safevoice(runtime, update, SimpleNamespace())

    assert runtime.enqueued == []
    assert runtime._long_pending_voice_keys == set()
    assert runtime._long_buffer_kinds == ["voice_transcript"]
    assert "hello batch" in runtime._long_buffer[0]
    assert "added to the active /long batch" in edits[0]["text"]
    assert answers == ["Added to /long batch"]


def test_pending_voice_keeps_original_position_after_later_media_arrives():
    runtime = _runtime()
    runtime_long.begin_batch(runtime, 123)
    pending_key = runtime_long.register_voice_confirmation(
        runtime,
        chat_id=123,
        prompt="[Voice message transcription] first item",
        transcript="first item",
        summary="Voice: first.ogg",
    )
    runtime_long.collect_media(
        runtime,
        123,
        'User sent a PDF document "second.pdf" (saved at /tmp/second.pdf). Read it.',
        "document",
        "second.pdf",
    )
    pending = runtime._pending_voice.pop(pending_key)

    assert runtime_long.resolve_voice_confirmation(runtime, pending_key, pending) is True
    submission = runtime_long.consume_batch(runtime, 123)

    assert submission is not None
    assert submission.prompt.index("Voice transcript: Voice: first.ogg") < submission.prompt.index(
        "Document: second.pdf"
    )
