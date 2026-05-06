from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestrator import runtime_media


class _Logger:
    def __init__(self):
        self.messages = []

    def info(self, message):
        self.messages.append(("info", message))

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

    async def get_file(self, file_id):
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
