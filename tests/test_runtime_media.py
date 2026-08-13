from pathlib import Path
from types import SimpleNamespace

import pytest
from telegram.error import TimedOut

from orchestrator import runtime_long, runtime_media


class _Logger:
    def __init__(self):
        self.messages = []

    def info(self, message, *args):
        if args:
            message = message % args
        self.messages.append(("info", message))

    def warning(self, message):
        self.messages.append(("warning", message))

    def error(self, message):
        self.messages.append(("error", message))

    def exception(self, message):
        self.messages.append(("exception", message))


class _TelegramFile:
    def __init__(self):
        self.downloaded_to = None

    async def download_to_drive(self, local_path):
        self.downloaded_to = local_path
        local_path.parent.mkdir(parents=True, exist_ok=True)
        local_path.write_text("media", encoding="utf-8")


class _Bot:
    def __init__(self):
        self.file = _TelegramFile()
        self.get_file_calls = 0

    async def get_file(self, file_id):
        self.get_file_calls += 1
        self.file_id = file_id
        return self.file


class _FlakyBot(_Bot):
    async def get_file(self, file_id):
        self.get_file_calls += 1
        if self.get_file_calls == 1:
            raise TimedOut("temporary timeout")
        self.file_id = file_id
        return self.file


def _update(**message_fields):
    message = SimpleNamespace(**message_fields)
    return SimpleNamespace(
        effective_chat=SimpleNamespace(id=123),
        effective_user=SimpleNamespace(id=1),
        message=message,
    )


def _runtime(tmp_path: Path):
    enqueued = []
    replies = []
    runtime = SimpleNamespace(
        app=SimpleNamespace(bot=_Bot()),
        backend_manager=SimpleNamespace(current_backend=SimpleNamespace(capabilities=SimpleNamespace(supports_files=True))),
        error_logger=_Logger(),
        logger=_Logger(),
        media_dir=tmp_path / "media",
        name="zelda",
        telegram_logger=_Logger(),
        _pending_voice={},
        _safevoice_enabled=True,
        _long_buffer=[],
        _long_buffer_kinds=[],
        _long_buffer_summaries=[],
        _long_buffer_ids=[],
        _long_buffer_active=False,
        _long_buffer_chat_id=None,
        _long_pending_voice_keys=set(),
        _long_buffer_timeout_task=None,
    )
    runtime._is_authorized_user = lambda user_id: user_id == 1
    runtime._record_active_chat = lambda update: None
    runtime._should_redirect_after_transfer = lambda: False
    runtime._transfer_redirect_text = lambda: "redirect"

    async def _reply_text(update, text, **kwargs):
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

    runtime._reply_text = _reply_text
    runtime.enqueue_request = enqueue_request
    runtime.download_media = lambda file_id, filename: runtime_media.download_media(runtime, file_id, filename)
    runtime._handle_media_message = (
        lambda update, media_kind, filename, file_id, prompt, summary:
        runtime_media.handle_media_message(runtime, update, media_kind, filename, file_id, prompt, summary)
    )
    runtime._handle_voice_or_audio = (
        lambda update, media_kind, filename, file_id, caption="":
        runtime_media.handle_voice_or_audio(
            runtime,
            update,
            media_kind,
            filename,
            file_id,
            caption=caption,
        )
    )
    runtime.enqueued = enqueued
    runtime.replies = replies
    return runtime


def test_build_media_prompt_for_image_document():
    prompt, summary = runtime_media.build_media_prompt("document", "scan.png", caption="receipt")

    assert "image file" in prompt
    assert "receipt" in prompt
    assert summary == "receipt"


@pytest.mark.asyncio
async def test_handle_document_downloads_and_enqueues(tmp_path):
    runtime = _runtime(tmp_path)
    update = _update(
        document=SimpleNamespace(file_name="notes.txt", file_id="file-1"),
        caption="please read",
    )

    await runtime_media.handle_document(runtime, update, SimpleNamespace())

    assert runtime.app.bot.file_id == "file-1"
    assert runtime.app.bot.file.downloaded_to == tmp_path / "media" / "notes.txt"
    assert runtime.enqueued[0]["source"] == "document"
    assert "notes.txt" in runtime.enqueued[0]["prompt"]
    assert "please read" in runtime.enqueued[0]["prompt"]


@pytest.mark.asyncio
async def test_download_media_retries_transient_telegram_timeout(tmp_path, monkeypatch):
    runtime = _runtime(tmp_path)
    runtime.app.bot = _FlakyBot()
    sleep_calls = []

    async def fake_sleep(delay):
        sleep_calls.append(delay)

    monkeypatch.setattr(runtime_media.asyncio, "sleep", fake_sleep)

    local_path = await runtime_media.download_media(runtime, "file-1", "photo.jpg")

    assert local_path == tmp_path / "media" / "photo.jpg"
    assert runtime.app.bot.get_file_calls == 2
    assert runtime.app.bot.file.downloaded_to == local_path
    assert sleep_calls == [0.75]
    assert any(level == "warning" for level, _message in runtime.logger.messages)


@pytest.mark.asyncio
async def test_download_media_keeps_duplicate_names_distinct_and_inside_media_dir(tmp_path):
    runtime = _runtime(tmp_path)

    first = await runtime_media.download_media(runtime, "file-1", "../same.pdf")
    second = await runtime_media.download_media(runtime, "file-2", "same.pdf")

    assert first == tmp_path / "media" / "same.pdf"
    assert second == tmp_path / "media" / "same_2.pdf"
    assert first.read_text(encoding="utf-8") == "media"
    assert second.read_text(encoding="utf-8") == "media"


@pytest.mark.asyncio
async def test_handle_sticker_enqueues_reaction(tmp_path):
    runtime = _runtime(tmp_path)
    update = _update(sticker=SimpleNamespace(emoji="✨"))

    await runtime_media.handle_sticker(runtime, update, SimpleNamespace())

    assert runtime.enqueued == [
        {
            "chat_id": 123,
            "prompt": "User sent a sticker (emoji: ✨). React warmly.",
            "source": "sticker",
            "summary": "✨",
        }
    ]


@pytest.mark.asyncio
async def test_document_is_collected_without_enqueue_during_long_batch(tmp_path):
    runtime = _runtime(tmp_path)
    runtime_long.begin_batch(runtime, 123)
    update = _update(
        document=SimpleNamespace(file_name="contract.pdf", file_id="file-1"),
        caption="compare this with the other files",
    )

    await runtime_media.handle_document(runtime, update, SimpleNamespace())

    assert runtime.enqueued == []
    submission = runtime_long.consume_batch(runtime, 123)
    assert submission is not None
    assert submission.source == "multimodal"
    assert submission.media_count == 1
    assert "contract.pdf" in submission.prompt
    assert str(tmp_path / "media" / "contract.pdf") in submission.prompt
    assert "compare this with the other files" in submission.prompt


@pytest.mark.asyncio
async def test_multiple_documents_and_task_enqueue_as_one_request_on_end(tmp_path):
    runtime = _runtime(tmp_path)
    runtime_long.begin_batch(runtime, 123)
    runtime_long.collect_text(runtime, 123, "Review all files together and report once.")
    first = _update(
        document=SimpleNamespace(file_name="authority.pdf", file_id="file-1"),
        caption="",
    )
    second = _update(
        document=SimpleNamespace(file_name="contract.pdf", file_id="file-2"),
        caption="",
    )

    await runtime_media.handle_document(runtime, first, SimpleNamespace())
    await runtime_media.handle_document(runtime, second, SimpleNamespace())
    await runtime_long.cmd_end(runtime, second, SimpleNamespace())

    assert len(runtime.enqueued) == 1
    request = runtime.enqueued[0]
    assert request["source"] == "multimodal"
    assert "Review all files together and report once." in request["prompt"]
    assert "authority.pdf" in request["prompt"]
    assert "contract.pdf" in request["prompt"]
    assert request["prompt"].index("authority.pdf") < request["prompt"].index("contract.pdf")
    assert "Return one consolidated response" in request["prompt"]


@pytest.mark.asyncio
async def test_photo_and_sticker_join_same_long_batch(tmp_path):
    runtime = _runtime(tmp_path)
    runtime_long.begin_batch(runtime, 123)
    photo_update = _update(
        photo=[SimpleNamespace(file_id="photo-1")],
        caption="front page",
    )
    sticker_update = _update(sticker=SimpleNamespace(emoji="✨"))

    await runtime_media.handle_photo(runtime, photo_update, SimpleNamespace())
    await runtime_media.handle_sticker(runtime, sticker_update, SimpleNamespace())

    assert runtime.enqueued == []
    submission = runtime_long.consume_batch(runtime, 123)
    assert submission is not None
    assert submission.media_count == 2
    assert "front page" in submission.prompt
    assert "✨" in submission.prompt
    assert submission.prompt.index("Photo: front page") < submission.prompt.index("Sticker: ✨")


@pytest.mark.asyncio
async def test_safevoice_confirmation_adds_transcript_to_long_batch(tmp_path, monkeypatch):
    runtime = _runtime(tmp_path)
    runtime_long.begin_batch(runtime, 123)

    class _Transcriber:
        async def transcribe(self, local_path):
            assert local_path.suffix == ".ogg"
            return "please compare the totals"

    monkeypatch.setattr(
        "orchestrator.voice_transcriber.get_transcriber",
        lambda: _Transcriber(),
    )
    update = _update(voice=SimpleNamespace(file_id="voice-1"))

    await runtime_media.handle_voice(runtime, update, SimpleNamespace())

    assert runtime.enqueued == []
    assert len(runtime._long_pending_voice_keys) == 1
    pending_key = next(iter(runtime._long_pending_voice_keys))
    assert pending_key.startswith("long-")
    pending = runtime._pending_voice.pop(pending_key)
    assert pending["long_batch"] is True
    assert runtime_long.resolve_voice_confirmation(runtime, pending_key, pending) is True

    submission = runtime_long.consume_batch(runtime, 123)
    assert submission is not None
    assert submission.media_count == 1
    assert "please compare the totals" in submission.prompt
    assert "Voice transcript" in submission.prompt


@pytest.mark.asyncio
async def test_failed_direct_voice_transcription_routes_her_to_media_read(tmp_path, monkeypatch):
    runtime = _runtime(tmp_path)
    runtime._safevoice_enabled = False
    runtime.config = SimpleNamespace(active_backend="her")

    class _Transcriber:
        async def transcribe(self, _local_path):
            return "[Transcription error] decoder rejected input"

    monkeypatch.setattr(
        "orchestrator.voice_transcriber.get_transcriber",
        lambda: _Transcriber(),
    )
    update = _update(voice=SimpleNamespace(file_id="voice-1"))

    await runtime_media.handle_voice(runtime, update, SimpleNamespace())

    assert len(runtime.enqueued) == 1
    assert "Use media_read" in runtime.enqueued[0]["prompt"]
    assert "normalize the audio" in runtime.enqueued[0]["prompt"]


@pytest.mark.asyncio
async def test_failed_direct_voice_transcription_does_not_offer_media_read_to_non_her(
    tmp_path,
    monkeypatch,
):
    runtime = _runtime(tmp_path)
    runtime._safevoice_enabled = False
    runtime.config = SimpleNamespace(active_backend="codex-cli")

    class _Transcriber:
        async def transcribe(self, _local_path):
            return "[Transcription error] decoder rejected input"

    monkeypatch.setattr(
        "orchestrator.voice_transcriber.get_transcriber",
        lambda: _Transcriber(),
    )
    update = _update(voice=SimpleNamespace(file_id="voice-1"))

    await runtime_media.handle_voice(runtime, update, SimpleNamespace())

    assert runtime.enqueued == []
    assert runtime.replies == [{"text": "Failed to transcribe voice message."}]
