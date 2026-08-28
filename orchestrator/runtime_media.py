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
    request_content_is_voice_origin,
)
from orchestrator.runtime_common import _print_user_message
from orchestrator.voice_transcript_gate import (
    complete_native_audio_response,
    release_deferred_transcript,
    state_after_transcription,
)


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
    duration_ms = None
    for attribute in ("document", "audio", "video", "voice"):
        media = getattr(message, attribute, None)
        candidate = str(getattr(media, "mime_type", "") or "").strip()
        if candidate:
            mime_type = candidate
        raw_duration = getattr(media, "duration", None)
        if raw_duration is not None:
            try:
                duration_ms = max(0, int(float(raw_duration) * 1000))
            except (TypeError, ValueError):
                duration_ms = None
        if candidate or duration_ms is not None:
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
        "duration_ms": duration_ms,
    }


def _single_attachment_request(
    prompt: str,
    *,
    media_kind: str,
    filename: str,
    local_path: Path,
    transport: dict[str, Any],
    attachment_id: str | None = None,
    semantic_role: str | None = None,
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
    parts: list[dict[str, Any]] = []
    next_index = 1
    if str(prompt or ""):
        parts.append({"type": "text", "item_index": next_index, "text": str(prompt)})
        next_index += 1
    media_part = {
                "type": "media",
                "item_index": next_index,
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
            }
    if modality == "audio":
        media_part["semantic_role"] = str(
            semantic_role
            or ("voice_message" if str(media_kind).casefold() == "voice" else "audio_attachment")
        )
        if transport.get("duration_ms") is not None:
            media_part["duration_ms"] = int(transport["duration_ms"])
    parts.append(media_part)
    content = canonical_request_content(parts)
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


def _backend_supports_native_audio_chat(
    runtime: Any,
    backend: Any,
    *,
    terminal: str | None = None,
) -> bool:
    if backend is None:
        return False
    global_config = getattr(runtime, "global_config", None)
    if (
        global_config is not None
        and hasattr(global_config, "native_audio_chat_v1")
        and not bool(getattr(global_config, "native_audio_chat_v1"))
    ):
        return False
    manager = getattr(runtime, "voice_manager", None)
    native_enabled = getattr(manager, "native_audio_enabled", None)
    if callable(native_enabled):
        try:
            enabled = bool(native_enabled(terminal))
        except TypeError:
            enabled = bool(native_enabled())
        if not enabled:
            return False
    capability = None
    resolver = getattr(backend, "resolve_input_capability", None)
    if callable(resolver):
        try:
            capability = resolver()
        except (TypeError, ValueError):
            capability = None
    accepts_audio = bool(capability and capability.supports("audio"))
    if not accepts_audio:
        accepts_audio = "audio" in set(
            getattr(getattr(backend, "capabilities", None), "input_modalities", ())
            or ()
        )
    if not accepts_audio:
        ingress_resolver = getattr(backend, "accepts_media_input", None)
        if callable(ingress_resolver):
            try:
                accepts_audio = bool(ingress_resolver("audio"))
            except (TypeError, ValueError):
                accepts_audio = False
    output_modalities = set(
        getattr(getattr(backend, "capabilities", None), "output_modalities", ())
        or ()
    )
    outputs_audio = "audio" in output_modalities
    if outputs_audio:
        output_capability = getattr(backend, "capabilities", None)
        formats = getattr(output_capability, "output_formats", {}) or {}
        streams = str(
            getattr(output_capability, "output_streaming", "none") or "none"
        ).strip().casefold()
        surface = str(
            getattr(output_capability, "api_surface", "unknown") or "unknown"
        ).strip().casefold()
        outputs_audio = bool(formats.get("audio")) and streams not in {
            "",
            "none",
            "unknown",
        } and surface not in {"", "none", "unknown"}
    if not outputs_audio:
        output_resolver = getattr(backend, "supports_media_output", None)
        if callable(output_resolver):
            try:
                outputs_audio = bool(output_resolver("audio"))
            except (TypeError, ValueError):
                outputs_audio = False
    return accepts_audio and outputs_audio


def _native_audio_retention(
    runtime: Any, *, terminal: str | None = None
) -> tuple[int, bool]:
    manager = getattr(runtime, "voice_manager", None)
    policy_resolver = getattr(manager, "native_policy_for_terminal", None)
    policy = (
        policy_resolver(terminal)
        if callable(policy_resolver)
        else getattr(manager, "native_policy", None)
    )
    if isinstance(policy, dict):
        raw = policy.get("retention_seconds", 3600)
    else:
        raw = getattr(
            getattr(runtime, "global_config", None),
            "native_audio_retention_seconds",
            3600,
        )
    if str(raw).strip().casefold() in {"indefinite", "forever"}:
        return 3600, True
    return max(60, int(raw or 3600)), False


def _admit_native_telegram_audio(
    runtime: Any,
    update: Any,
    *,
    local_path: Path,
    filename: str,
    media_kind: str,
) -> tuple[dict[str, Any], tuple[dict[str, Any], ...], dict[str, Any]]:
    """Use Session attachment admission when available; keep tests lightweight."""

    transport = _transport_metadata(update, filename)
    semantic_role = (
        "voice_message" if media_kind.casefold() == "voice" else "audio_attachment"
    )
    store = getattr(runtime, "session_store", None)
    if store is None:
        content, manifest = _single_attachment_request(
            "",
            media_kind=media_kind,
            filename=filename,
            local_path=local_path,
            transport=transport,
            semantic_role=semantic_role,
        )
        return content, manifest, _single_attachment_metadata(content, manifest)

    from orchestrator import runtime_session

    _chat_id, route_metadata, _deliver = runtime_session.request_route_for_update(
        runtime, update
    )
    payload = local_path.read_bytes()
    retention_seconds, retention_indefinite = _native_audio_retention(
        runtime, terminal="telegram"
    )
    staged = store.stage_attachment(
        session_id=route_metadata["session_id"],
        owner_id=route_metadata["owner_id"],
        filename=filename,
        media_type=str(transport.get("mime_type") or infer_mime_type(filename)),
        size_bytes=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
        semantic_role=semantic_role,
        duration_ms=transport.get("duration_ms"),
        retention_seconds=retention_seconds,
        retention_indefinite=retention_indefinite,
    )
    store.upload_attachment_bytes(
        session_id=route_metadata["session_id"],
        owner_id=route_metadata["owner_id"],
        attachment_id=staged["attachment_id"],
        payload=payload,
    )
    committed = store.commit_attachment(
        session_id=route_metadata["session_id"],
        owner_id=route_metadata["owner_id"],
        attachment_id=staged["attachment_id"],
    )
    part = store.attachment_canonical_part(
        session_id=route_metadata["session_id"],
        owner_id=route_metadata["owner_id"],
        attachment_id=committed["attachment_id"],
        item_index=1,
        semantic_role=semantic_role,
    )
    content = canonical_request_content([part])
    manifest = attachment_manifest(content)
    metadata = {
        **route_metadata,
        **_single_attachment_metadata(content, manifest),
        "session_message_text": "",
        "session_message_content": [
            {
                "type": "audio",
                "attachment_id": committed["attachment_id"],
                "semantic_role": semantic_role,
                "mime_type": committed["media_type"],
            }
        ],
        "voice_origin": request_content_is_voice_origin(content),
    }
    return content, manifest, metadata


async def _present_safe_voice_transcript(
    runtime: Any,
    update: Any,
    *,
    transcript: str,
    prompt: str,
    summary: str,
    request_id: str | None = None,
    attachment_id: str | None = None,
    native_audio: bool = False,
) -> None:
    chat_id = update.effective_chat.id
    chat_key = str(chat_id)
    runtime._pending_voice[chat_key] = {
        "prompt": prompt,
        "transcript": transcript,
        "summary": summary,
        "chat_id": chat_id,
        "timestamp": datetime.now().isoformat(),
        "long_batch": False,
        "native_audio": native_audio,
        "request_id": request_id,
        "attachment_id": attachment_id,
    }
    preview = (
        transcript[:3500] + f"\n\n…(共 {len(transcript)} 字，已截断)"
        if len(transcript) > 3500
        else transcript
    )
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "Send transcript", callback_data=f"safevoice:yes:{chat_key}"
                ),
                InlineKeyboardButton(
                    "Discard transcript", callback_data=f"safevoice:no:{chat_key}"
                ),
            ]
        ]
    )
    await runtime._reply_text(
        update,
        f"🛡️ *Safe Voice — Confirm transcription:*\n\n_{preview}_",
        reply_markup=keyboard,
        parse_mode="Markdown",
    )


def disable_safe_voice(runtime: Any) -> None:
    """Resolve native transcript waiters when Safe Voice is switched off.

    A transcript that has already reached the confirmation boundary keeps the
    legacy Safe Voice meaning: clearing the pending challenge discards that
    transcript-dependent path.  An STT task that is still running, or a ready
    transcript that no consumer has challenged yet, becomes an automatic
    (Safe Voice off) transcript.  In every case no HER stage is left waiting
    on an event whose UI control has disappeared.
    """

    registry = getattr(runtime, "_native_voice_transcripts", None)
    if not isinstance(registry, dict):
        return
    seen: set[int] = set()
    for candidate in registry.values():
        if not isinstance(candidate, dict) or id(candidate) in seen:
            continue
        seen.add(id(candidate))
        status = str(candidate.get("status") or "pending").strip().casefold()
        if status == "pending":
            candidate["safe_voice"] = False
            continue
        if status == "ready":
            candidate["safe_voice"] = False
            task = asyncio.create_task(
                release_deferred_transcript(candidate),
                name=(
                    "native-voice-safevoice-off:"
                    f"{candidate.get('request_id') or candidate.get('attachment_id') or 'unknown'}"
                ),
            )
            tasks = getattr(runtime, "_audio_transcript_tasks", None)
            if isinstance(tasks, set):
                tasks.add(task)
                task.add_done_callback(tasks.discard)
            continue
        if status != "pending_confirmation":
            continue
        request_id = str(candidate.get("request_id") or "").strip()
        decider = getattr(
            getattr(runtime, "session_store", None),
            "decide_voice_transcript",
            None,
        )
        if request_id and callable(decider):
            try:
                decider(request_id=request_id, confirmed=False)
            except Exception as exc:
                logger = getattr(runtime, "error_logger", None)
                warning = getattr(logger, "warning", None)
                if callable(warning):
                    warning(
                        "Unable to persist discarded native voice transcript %s: %s",
                        request_id,
                        exc,
                    )
        candidate["status"] = "discarded"
        release_event = candidate.get("release_event")
        if isinstance(release_event, asyncio.Event):
            release_event.set()


async def finish_native_voice_transcript_path(
    runtime: Any,
    request_id: str,
    payload: dict[str, Any],
) -> bool:
    """Resolve deferred STT after a no-tool native audio reply completes."""

    registry = getattr(runtime, "_native_voice_transcripts", None)
    state = registry.get(str(request_id)) if isinstance(registry, dict) else None
    return await complete_native_audio_response(state, payload)


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
        backend = getattr(runtime.backend_manager, "current_backend", None)
        native_audio = _backend_supports_native_audio_chat(
            runtime, backend, terminal="telegram"
        ) and not runtime_long.is_collecting(runtime, update.effective_chat.id)
        if native_audio:
            transcript_task = asyncio.create_task(transcriber.transcribe(local_path))
            request_content, manifest, request_metadata = (
                _admit_native_telegram_audio(
                    runtime,
                    update,
                    local_path=local_path,
                    filename=filename,
                    media_kind=media_kind,
                )
            )
            attachment_id = str(manifest[0]["attachment_id"])
            registry = getattr(runtime, "_native_voice_transcripts", None)
            if not isinstance(registry, dict):
                registry = {}
                runtime._native_voice_transcripts = registry
            request_id: str | None = None
            transcript_state = {
                "task": transcript_task,
                "ready_event": asyncio.Event(),
                "release_event": asyncio.Event(),
                "gate_lock": asyncio.Lock(),
                "status": "pending",
                "attachment_id": attachment_id,
                "safe_voice": bool(runtime._safevoice_enabled),
                "confirmation_requested": False,
                "confirmation_presented": False,
                "native_audio_completed": False,
                "text": "",
            }

            async def _request_confirmation() -> None:
                current_request_id = str(
                    transcript_state.get("request_id") or ""
                ).strip()
                store = getattr(runtime, "session_store", None)
                require_confirmation = getattr(
                    store, "require_voice_transcript_confirmation", None
                )
                if current_request_id and callable(require_confirmation):
                    require_confirmation(request_id=current_request_id)
                transcript_state["status"] = "pending_confirmation"
                transcript = str(transcript_state.get("text") or "").strip()
                prompt = f"[Voice message transcription] {transcript}"
                if caption:
                    prompt += f'\nCaption: "{caption}"'
                await _present_safe_voice_transcript(
                    runtime,
                    update,
                    transcript=transcript,
                    prompt=prompt,
                    summary=f"{media_kind}: {filename}",
                    request_id=current_request_id or None,
                    attachment_id=attachment_id,
                    native_audio=True,
                )

            async def _auto_release() -> None:
                current_request_id = str(
                    transcript_state.get("request_id") or ""
                ).strip()
                store = getattr(runtime, "session_store", None)
                release_ready = getattr(
                    store, "release_ready_voice_transcript", None
                )
                if current_request_id and callable(release_ready):
                    release_ready(request_id=current_request_id)

            transcript_state["request_confirmation"] = _request_confirmation
            transcript_state["auto_release"] = _auto_release
            registry[attachment_id] = transcript_state
            request_metadata["voice_transcript_key"] = attachment_id
            request_metadata["safe_voice"] = bool(runtime._safevoice_enabled)
            request_id = await runtime.enqueue_request(
                update.effective_chat.id,
                caption,
                media_kind.casefold(),
                f"{media_kind}: {filename}",
                request_metadata=request_metadata,
                request_content=request_content,
            )
            if request_id:
                transcript_state["request_id"] = str(request_id)
                registry[str(request_id)] = transcript_state

            transcript = await transcript_task
            if transcript.startswith("[Transcription error]"):
                transcript_state["status"] = "unavailable"
                transcript_state["ready_event"].set()
                transcript_state["release_event"].set()
                runtime.error_logger.error(
                    f"Voice transcription failed for {filename}: {transcript}"
                )
                await runtime._reply_text(
                    update,
                    "Local voice transcription is unavailable; the native audio response will continue.",
                )
                store = getattr(runtime, "session_store", None)
                recorder = getattr(store, "record_voice_transcript", None)
                if callable(recorder) and request_id:
                    recorder(
                        request_id=str(request_id),
                        attachment_id=attachment_id,
                        text="",
                        provenance="local_stt",
                        safe_voice_state="unavailable",
                    )
                if store is not None:
                    local_path.unlink(missing_ok=True)
                return

            async with transcript_state["gate_lock"]:
                transcript_state["text"] = transcript
                transcript_state["status"] = state_after_transcription(
                    transcript_state
                )
            _print_user_message(runtime.name, transcript, media_tag="Transcription")
            runtime.telegram_logger.info(
                f"Transcribed {media_kind.lower()} ({filename}): {len(transcript)} chars"
            )
            store = getattr(runtime, "session_store", None)
            recorder = getattr(store, "record_voice_transcript", None)
            if callable(recorder) and request_id:
                recorder(
                    request_id=str(request_id),
                    attachment_id=attachment_id,
                    text=transcript,
                    provenance="local_stt",
                    safe_voice_state=str(transcript_state["status"]),
                )
            transcript_state["ready_event"].set()
            if transcript_state["status"] in {
                "released",
                "discarded",
                "unavailable",
            }:
                transcript_state["release_event"].set()
            if store is not None:
                local_path.unlink(missing_ok=True)
            return

        transcript = await transcriber.transcribe(local_path)

        if transcript.startswith("[Transcription error]"):
            runtime.error_logger.error(f"Voice transcription failed for {filename}: {transcript}")
            await runtime._reply_text(
                update, f"Failed to transcribe {media_kind.lower()} message."
            )
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
