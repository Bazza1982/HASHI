from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import NetworkError, TimedOut

from orchestrator import runtime_long
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


def _log_warning(runtime: Any, message: str) -> None:
    logger = getattr(runtime, "logger", None)
    warning = getattr(logger, "warning", None)
    if callable(warning):
        warning(message)
    else:
        info = getattr(logger, "info", None)
        if callable(info):
            info(message)


def _available_media_path(media_dir: Path, filename: str) -> Path:
    # Telegram filenames are user-controlled. Keep every download inside the
    # agent media directory and avoid overwriting same-named batch items.
    normalized = str(filename or "").replace("\\", "/")
    safe_name = Path(normalized).name.strip() or f"media_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}"
    candidate = media_dir / safe_name
    if not candidate.exists():
        return candidate
    suffix = candidate.suffix
    stem = candidate.name[: -len(suffix)] if suffix else candidate.name
    for index in range(2, 10_000):
        alternative = media_dir / f"{stem}_{index}{suffix}"
        if not alternative.exists():
            return alternative
    raise RuntimeError(f"could not allocate a unique media filename for {safe_name!r}")


async def download_media(runtime: Any, file_id: str, filename: str) -> Path:
    local_path = _available_media_path(runtime.media_dir, filename)
    retryable_errors = (TimedOut, NetworkError, TimeoutError, OSError)
    max_attempts = 3
    last_error: Exception | None = None

    for attempt in range(1, max_attempts + 1):
        try:
            tg_file = await runtime.app.bot.get_file(file_id)
            await tg_file.download_to_drive(local_path)
            runtime.logger.info(f"Downloaded media: {local_path}")
            return local_path
        except retryable_errors as e:
            last_error = e
            if attempt >= max_attempts:
                break
            delay = 0.75 * attempt
            _log_warning(
                runtime,
                f"Media download attempt {attempt}/{max_attempts} failed for {filename}: {e}; retrying in {delay:.2f}s",
            )
            await asyncio.sleep(delay)

    if last_error is not None:
        raise last_error
    raise RuntimeError(f"Media download failed for {filename}")


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
        if runtime_long.collect_media(
            runtime,
            update.effective_chat.id,
            rendered_prompt,
            media_kind.lower(),
            summary,
        ):
            return
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
    runtime._record_active_chat(update)
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
                if runtime_long.collect_media(
                    runtime,
                    update.effective_chat.id,
                    prompt,
                    media_kind.lower(),
                    filename,
                ):
                    return
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

        chat_id = update.effective_chat.id
        batch_pending_key = None
        if runtime._safevoice_enabled and runtime_long.is_collecting(runtime, chat_id):
            batch_pending_key = runtime_long.register_voice_confirmation(
                runtime,
                chat_id=chat_id,
                prompt=prompt,
                transcript=transcript,
                summary=f"{media_kind}: {filename}",
            )

        if runtime._safevoice_enabled:
            chat_key = batch_pending_key or str(chat_id)
            runtime._pending_voice[chat_key] = {
                "prompt": prompt,
                "transcript": transcript,
                "summary": f"{media_kind}: {filename}",
                "chat_id": chat_id,
                "timestamp": datetime.now().isoformat(),
                "long_batch": bool(batch_pending_key),
            }
            max_preview = 3500
            if len(transcript) > max_preview:
                preview = transcript[:max_preview] + f"\n\n…(共 {len(transcript)} 字，已截断)"
            else:
                preview = transcript
            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("Send transcript", callback_data=f"safevoice:yes:{chat_key}"),
                    InlineKeyboardButton("Discard transcript", callback_data=f"safevoice:no:{chat_key}"),
                ]
            ])
            safevoice_title = (
                "🛡️ *Safe Voice — Confirm transcription for /long batch:*"
                if batch_pending_key
                else "🛡️ *Safe Voice — Confirm transcription:*"
            )
            await runtime._reply_text(
                update,
                f"{safevoice_title}\n\n_{preview}_",
                reply_markup=keyboard,
                parse_mode="Markdown",
            )
        else:
            if runtime_long.collect_media(
                runtime,
                chat_id,
                prompt,
                "voice_transcript",
                f"{media_kind}: {filename}",
            ):
                return
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
    if runtime_long.collect_media(runtime, update.effective_chat.id, prompt, "sticker", summary):
        return
    await runtime.enqueue_request(update.effective_chat.id, prompt, "sticker", summary)
