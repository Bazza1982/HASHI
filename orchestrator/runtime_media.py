from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import NetworkError, TimedOut

from orchestrator import runtime_long
from orchestrator.media_utils import is_image_file
from orchestrator.multimodal_contract import (
    attachment_manifest,
    canonical_request_content,
    infer_mime_type,
    modality_for_attachment,
)
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


def _is_her_backend(runtime: Any, backend: Any) -> bool:
    backend_engine = str(
        getattr(getattr(backend, "config", None), "engine", "") or ""
    ).strip().casefold()
    if not backend_engine:
        backend_engine = str(
            getattr(getattr(runtime, "config", None), "active_backend", "") or ""
        ).strip().casefold()
    return backend_engine in {"her", "her-v2"}


def _backend_accepts_media_bridge(backend: Any, media_kind: str, filename: str) -> bool:
    capabilities = getattr(backend, "capabilities", None)
    modality = modality_for_attachment(
        media_kind,
        filename=filename,
        mime_type=infer_mime_type(filename),
    )
    resolver = getattr(backend, "resolve_input_capability", None)
    if callable(resolver):
        try:
            if resolver().supports(modality):
                return True
        except (TypeError, ValueError):
            pass
    if modality in set(getattr(capabilities, "input_modalities", ()) or ()):
        return True

    ingress_resolver = getattr(backend, "accepts_media_input", None)
    if callable(ingress_resolver):
        try:
            if ingress_resolver(modality):
                return True
        except (TypeError, ValueError):
            pass

    registry = getattr(backend, "tool_registry", None)
    is_allowed = getattr(registry, "is_allowed", None)
    supports_files = bool(getattr(capabilities, "supports_files", False))

    kind = str(media_kind or "").strip().casefold()
    if kind == "photo" or (kind == "document" and is_image_file(filename)):
        return bool(
            callable(is_allowed)
            and (is_allowed("vision_inspect") or is_allowed("media_read"))
        )
    if kind in {"audio", "video", "voice"}:
        return bool(callable(is_allowed) and is_allowed("media_read"))
    if kind == "document":
        # The legacy supports_files flag is meaningful only for ordinary
        # documents.  It is never proof of image/audio/video understanding.
        return bool(
            supports_files
            or (
                callable(is_allowed)
                and (is_allowed("file_read") or is_allowed("media_read"))
            )
        )
    return False


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


def _transport_metadata(update: Any, filename: str) -> dict[str, Any]:
    message = getattr(update, "message", None)
    caption = str(getattr(message, "caption", "") or "")
    mime_type = ""
    for attribute in ("document", "audio", "video", "voice"):
        media = getattr(message, attribute, None)
        candidate = str(getattr(media, "mime_type", "") or "").strip()
        if candidate:
            mime_type = candidate
            break
    if not mime_type and getattr(message, "photo", None):
        mime_type = "image/jpeg"
    return {
        "filename": str(filename or ""),
        "caption": caption,
        "mime_type": mime_type or infer_mime_type(filename),
        "update_id": getattr(update, "update_id", None),
        "message_id": getattr(message, "message_id", None),
        "media_group_id": getattr(message, "media_group_id", None),
    }


def _single_attachment_request(
    prompt: str,
    *,
    media_kind: str,
    filename: str,
    local_path: Path,
    transport: dict[str, Any],
    attachment_id: str | None = None,
) -> tuple[dict[str, Any], tuple[dict[str, Any], ...]]:
    digest = hashlib.sha256()
    size_bytes = 0
    with local_path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            size_bytes += len(chunk)
            digest.update(chunk)
    modality = modality_for_attachment(
        media_kind,
        mime_type=str(transport.get("mime_type") or ""),
        filename=filename,
    )
    content = canonical_request_content(
        [
            {"type": "text", "item_index": 1, "text": str(prompt)},
            {
                "type": "media",
                "item_index": 2,
                "attachment_id": attachment_id or f"attachment-{uuid4().hex}",
                "modality": modality,
                "kind": str(media_kind or modality).strip().casefold(),
                "mime_type": str(
                    transport.get("mime_type")
                    or infer_mime_type(filename, modality=modality)
                ),
                "filename": filename,
                "caption": str(transport.get("caption") or ""),
                "local_ref": str(local_path),
                "size_bytes": size_bytes,
                "sha256": digest.hexdigest(),
                "transport": {
                    key: transport[key]
                    for key in ("update_id", "message_id", "media_group_id")
                    if transport.get(key) not in (None, "")
                },
            },
        ]
    )
    return content, attachment_manifest(content)


def _single_attachment_metadata(
    content: dict[str, Any], manifest: tuple[dict[str, Any], ...]
) -> dict[str, Any]:
    return {
        "canonical_content_version": content["version"],
        "attachment_manifest": [dict(item) for item in manifest],
    }


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
    if backend and not _backend_accepts_media_bridge(backend, media_kind, filename):
        await runtime._reply_text(update, f"Current backend does not support {media_kind.lower()} attachments yet.")
        return
    _print_user_message(runtime.name, summary, media_tag=media_kind)
    chat_id = update.effective_chat.id
    reservation_id = runtime_long.reserve_media(
        runtime,
        chat_id,
        media_kind.lower(),
        summary,
        transport_metadata=_transport_metadata(update, filename),
    )
    try:
        local_path = await runtime.download_media(file_id, filename)
        rendered_prompt = prompt.replace("{local_path}", str(local_path))
        if reservation_id is not None and runtime_long.complete_media(
            runtime,
            reservation_id,
            rendered_prompt,
            local_path=local_path,
        ):
            return
        transport = _transport_metadata(update, filename)
        request_content, manifest = _single_attachment_request(
            rendered_prompt,
            media_kind=media_kind,
            filename=filename,
            local_path=local_path,
            transport=transport,
            attachment_id=reservation_id,
        )
        await runtime.enqueue_request(
            chat_id,
            rendered_prompt,
            media_kind.lower(),
            summary,
            request_metadata=_single_attachment_metadata(
                request_content, manifest
            ),
            request_content=request_content,
        )
    except Exception as e:
        if reservation_id is not None:
            runtime_long.discard_media_reservation(runtime, reservation_id)
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
            if (
                backend
                and _is_her_backend(runtime, backend)
                and _backend_accepts_media_bridge(
                    backend,
                    media_kind,
                    filename,
                )
            ):
                prompt = (
                    f"User sent a voice message (saved at {local_path}). "
                    "Local direct transcription failed. Use media_read on that exact path; "
                    "it will normalize the audio before retrying transcription, then respond."
                )
                if runtime_long.collect_media(
                    runtime,
                    update.effective_chat.id,
                    prompt,
                    media_kind.lower(),
                    filename,
                    local_path=local_path,
                    transport_metadata=_transport_metadata(update, filename),
                ):
                    return
                request_content, manifest = _single_attachment_request(
                    prompt,
                    media_kind=media_kind,
                    filename=filename,
                    local_path=local_path,
                    transport=_transport_metadata(update, filename),
                )
                await runtime.enqueue_request(
                    update.effective_chat.id,
                    prompt,
                    media_kind.lower(),
                    filename,
                    request_metadata=_single_attachment_metadata(
                        request_content, manifest
                    ),
                    request_content=request_content,
                )
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
    if runtime_long.collect_media(
        runtime,
        update.effective_chat.id,
        prompt,
        "sticker",
        summary,
        transport_metadata=_transport_metadata(update, "sticker"),
    ):
        return
    await runtime.enqueue_request(update.effective_chat.id, prompt, "sticker", summary)
