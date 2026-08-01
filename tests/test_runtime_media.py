from pathlib import Path
from types import SimpleNamespace

import pytest
from telegram.error import TimedOut

from orchestrator import runtime_media


class _Logger:
    def __init__(self):
        self.messages = []

    def info(self, message):
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


def _enable_long_buffer(runtime, target_session_id="long-1"):
    collected = []
    runtime._active_long_session_id = (
        lambda chat_id: target_session_id if chat_id == 123 else None
    )

    def _buffer_long_chunk(chat_id, text, *, session_id=None):
        if chat_id != 123 or session_id != target_session_id:
            return False
        collected.append(text)
        return True

    runtime._buffer_long_chunk = _buffer_long_chunk
    runtime.collected = collected
    return collected


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
async def test_handle_document_downloads_into_long_buffer_without_enqueuing(tmp_path):
    runtime = _runtime(tmp_path)
    collected = _enable_long_buffer(runtime)
    update = _update(
        document=SimpleNamespace(file_name="notes.txt", file_id="file-1"),
        caption="please read",
    )

    await runtime_media.handle_document(runtime, update, SimpleNamespace())

    assert runtime.enqueued == []
    assert len(collected) == 1
    assert collected[0].startswith("[Document]\n")
    assert str(tmp_path / "media" / "notes.txt") in collected[0]
    assert "please read" in collected[0]
    assert runtime.replies[-1]["text"].startswith("📝 Added document to /long")


@pytest.mark.asyncio
async def test_handle_photo_downloads_into_long_buffer_without_enqueuing(tmp_path):
    runtime = _runtime(tmp_path)
    collected = _enable_long_buffer(runtime)
    update = _update(
        photo=[SimpleNamespace(file_id="photo-1")],
        caption="compare this",
    )

    await runtime_media.handle_photo(runtime, update, SimpleNamespace())

    assert runtime.enqueued == []
    assert len(collected) == 1
    assert collected[0].startswith("[Photo]\n")
    assert "compare this" in collected[0]
    assert "View the image" in collected[0]


@pytest.mark.asyncio
async def test_handle_voice_transcript_goes_into_long_buffer(tmp_path, monkeypatch):
    from orchestrator import voice_transcriber

    class _Transcriber:
        async def transcribe(self, _local_path):
            return "Please compare the attached items."

    monkeypatch.setattr(voice_transcriber, "get_transcriber", lambda: _Transcriber())
    runtime = _runtime(tmp_path)
    runtime._safevoice_enabled = False
    collected = _enable_long_buffer(runtime)
    update = _update(voice=SimpleNamespace(file_id="voice-1"))

    await runtime_media.handle_voice(runtime, update, SimpleNamespace())

    assert runtime.enqueued == []
    assert collected == [
        "[Voice]\n[Voice message transcription] Please compare the attached items."
    ]
    assert runtime.replies[-1]["text"].startswith("📝 Added voice to /long")


@pytest.mark.asyncio
async def test_safe_voice_remembers_target_long_session(tmp_path, monkeypatch):
    from orchestrator import voice_transcriber

    class _Transcriber:
        async def transcribe(self, _local_path):
            return "Please inspect the photo."

    monkeypatch.setattr(voice_transcriber, "get_transcriber", lambda: _Transcriber())
    runtime = _runtime(tmp_path)
    _enable_long_buffer(runtime)
    update = _update(voice=SimpleNamespace(file_id="voice-1"))

    await runtime_media.handle_voice(runtime, update, SimpleNamespace())

    assert runtime.enqueued == []
    assert runtime.collected == []
    assert runtime._pending_voice["123"]["long_session_id"] == "long-1"
    assert "Confirm to add to /long before /end" in runtime.replies[-1]["text"]


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
