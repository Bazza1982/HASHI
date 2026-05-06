from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from orchestrator.media_utils import is_image_file
from orchestrator.runtime_common import _print_user_message


def build_media_prompt(
    media_kind: str,
    filename: str,
    caption: str = "",
    emoji: str = "",
) -> tuple[str, str]:
    kind = media_kind.lower()
    ext = Path(filename).suffix.lower()

    if kind == "document":
        if is_image_file(filename):
            prompt = f'User sent an image file "{filename}" (saved at {{local_path}}). View the image carefully and respond.'
            if caption:
                prompt += f' Caption: "{caption}"'
            return prompt, caption or filename
        if ext == ".pdf":
            prompt = f'User sent a PDF document "{filename}" (saved at {{local_path}}). Extract the text, analyze the contents thoroughly, and respond.'
        elif ext in [".txt", ".md", ".csv", ".json", ".py", ".js", ".html"]:
            prompt = f'User sent a text/code file "{filename}" (saved at {{local_path}}). Read the raw contents carefully and respond.'
        else:
            prompt = f'User sent a document "{filename}" (saved at {{local_path}}). Attempt to read the file and respond.'
        if caption:
            prompt += f' Caption: "{caption}"'
        return prompt, filename

    if kind == "photo":
        prompt = "User sent a photo (saved at {local_path})."
        if caption:
            prompt += f' Caption: "{caption}"'
        prompt += " View the image and respond."
        return prompt, caption or filename

    if kind == "voice":
        return (
            "User sent a voice message (saved at {local_path}). Listen to the audio, transcribe it, and respond.",
            filename,
        )

    if kind == "audio":
        prompt = f'User sent an audio file "{filename}" (saved at {{local_path}}).'
        if caption:
            prompt += f' Caption: "{caption}"'
        prompt += " Listen to the audio and respond."
        return prompt, filename

    if kind == "video":
        prompt = f'User sent a video "{filename}" (saved at {{local_path}}).'
        if caption:
            prompt += f' Caption: "{caption}"'
        prompt += " Watch the video and respond."
        return prompt, filename

    if kind == "sticker":
        prompt = f"User sent a sticker (emoji: {emoji or ''}). React warmly."
        if caption:
            prompt += f' Caption: "{caption}"'
        return prompt, emoji or filename or "sticker"

    return f'User sent a file "{filename}" (saved at {{local_path}}). Read it if possible and respond.', filename


async def download_media(runtime: Any, file_id: str, filename: str) -> Path:
    tg_file = await runtime.app.bot.get_file(file_id)
    local_path = runtime.media_dir / filename
    await tg_file.download_to_drive(local_path)
    runtime.logger.info(f"Downloaded media: {local_path}")
    return local_path


async def handle_media_message(
    runtime: Any,
    update: Any,
    media_kind: str,
    filename: str,
    file_id: str,
    prompt: str,
    summary: str,
):
    runtime._record_active_chat(update)
    if runtime._should_redirect_after_transfer():
        await runtime._reply_text(update, runtime._transfer_redirect_text())
        return
    backend = getattr(runtime.backend_manager, "current_backend", None)
    if backend and not backend.capabilities.supports_files:
        await runtime._reply_text(update, f"Current backend does not support {media_kind.lower()} attachments yet.")
        return
    _print_user_message(runtime.name, summary, media_tag=media_kind)
    try:
        local_path = await runtime.download_media(file_id, filename)
        rendered_prompt = prompt.replace("{local_path}", str(local_path))
        await runtime.enqueue_request(update.effective_chat.id, rendered_prompt, media_kind.lower(), summary)
    except Exception as e:
        runtime.error_logger.exception(f"{media_kind} handler failed for '{filename}': {e}")
        try:
            await runtime._reply_text(update, f"Failed to process {media_kind.lower()} message.")
        except Exception:
            pass


async def handle_document(runtime: Any, update: Any, context: Any):
    if not runtime._is_authorized_user(update.effective_user.id):
        return
    doc = update.message.document
    original_name = doc.file_name or f"file_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    caption = update.message.caption or ""
    prompt, summary = build_media_prompt("document", original_name, caption=caption)
    await runtime._handle_media_message(update, "Document", original_name, doc.file_id, prompt, summary)


async def handle_photo(runtime: Any, update: Any, context: Any):
    if not runtime._is_authorized_user(update.effective_user.id):
        return
    photo = update.message.photo[-1]
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"photo_{ts}.jpg"
    caption = update.message.caption or ""
    prompt, summary = build_media_prompt("photo", filename, caption=caption)
    await runtime._handle_media_message(update, "Photo", filename, photo.file_id, prompt, summary)


async def handle_voice(runtime: Any, update: Any, context: Any):
    if not runtime._is_authorized_user(update.effective_user.id):
        return
    voice = update.message.voice
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"voice_{ts}.ogg"
    await runtime._handle_voice_or_audio(update, "Voice", filename, voice.file_id)


async def handle_audio(runtime: Any, update: Any, context: Any):
    if not runtime._is_authorized_user(update.effective_user.id):
        return
    audio = update.message.audio
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    original_name = audio.file_name or f"audio_{ts}"
    caption = update.message.caption or ""
    await runtime._handle_voice_or_audio(update, "Audio", original_name, audio.file_id, caption=caption)


async def handle_voice_or_audio(
    runtime: Any,
    update: Any,
    media_kind: str,
    filename: str,
    file_id: str,
    caption: str = "",
):
    """Download voice/audio, transcribe locally, and dispatch as text."""
    if runtime._should_redirect_after_transfer():
        await runtime._reply_text(update, runtime._transfer_redirect_text())
        return
    from orchestrator.voice_transcriber import get_transcriber

    _print_user_message(runtime.name, f"Transcribing {filename}...", media_tag=media_kind)
    try:
        local_path = await runtime.download_media(file_id, filename)
        transcriber = get_transcriber()
        transcript = await transcriber.transcribe(local_path)

        if transcript.startswith("[Transcription error]"):
            runtime.error_logger.error(f"Voice transcription failed for {filename}: {transcript}")
            backend = getattr(runtime.backend_manager, "current_backend", None)
            if backend and backend.capabilities.supports_files:
                prompt = f"User sent a voice message (saved at {local_path}). Listen to the audio, transcribe it, and respond."
                await runtime.enqueue_request(update.effective_chat.id, prompt, media_kind.lower(), filename)
            else:
                await runtime._reply_text(update, f"Failed to transcribe {media_kind.lower()} message.")
            return

        _print_user_message(runtime.name, transcript, media_tag="Transcription")
        prompt = f"[Voice message transcription] {transcript}"
        if caption:
            prompt += f'\nCaption: "{caption}"'

        runtime.telegram_logger.info(
            f"Transcribed {media_kind.lower()} ({filename}): {len(transcript)} chars"
        )

        if runtime._safevoice_enabled:
            chat_id = update.effective_chat.id
            chat_key = str(chat_id)
            runtime._pending_voice[chat_key] = {
                "prompt": prompt,
                "transcript": transcript,
                "summary": f"{media_kind}: {filename}",
                "timestamp": datetime.now().isoformat(),
            }
            max_preview = 3500
            if len(transcript) > max_preview:
                preview = transcript[:max_preview] + f"\n\n…(共 {len(transcript)} 字，已截断)"
            else:
                preview = transcript
            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("✅ Send", callback_data=f"safevoice:yes:{chat_key}"),
                    InlineKeyboardButton("❌ Discard", callback_data=f"safevoice:no:{chat_key}"),
                ]
            ])
            await runtime._reply_text(
                update,
                f"🛡️ *Safe Voice — Confirm transcription:*\n\n_{preview}_",
                reply_markup=keyboard,
                parse_mode="Markdown",
            )
        else:
            await runtime.enqueue_request(update.effective_chat.id, prompt, "voice_transcript", f"{media_kind}: {filename}")
    except Exception as e:
        runtime.error_logger.exception(f"{media_kind} voice handler failed for '{filename}': {e}")
        try:
            await runtime._reply_text(update, f"Failed to process {media_kind.lower()} message.")
        except Exception:
            pass


async def handle_video(runtime: Any, update: Any, context: Any):
    if not runtime._is_authorized_user(update.effective_user.id):
        return
    video = update.message.video
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    original_name = video.file_name or f"video_{ts}.mp4"
    caption = update.message.caption or ""
    prompt, summary = build_media_prompt("video", original_name, caption=caption)
    await runtime._handle_media_message(update, "Video", original_name, video.file_id, prompt, summary)


async def handle_sticker(runtime: Any, update: Any, context: Any):
    if not runtime._is_authorized_user(update.effective_user.id):
        return
    if runtime._should_redirect_after_transfer():
        await runtime._reply_text(update, runtime._transfer_redirect_text())
        return
    sticker = update.message.sticker
    emoji = sticker.emoji or ""
    prompt, summary = build_media_prompt("sticker", "sticker", emoji=emoji)
    _print_user_message(runtime.name, emoji or "sticker", media_tag="Sticker")
    await runtime.enqueue_request(update.effective_chat.id, prompt, "sticker", summary)
