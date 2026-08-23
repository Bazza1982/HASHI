from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from orchestrator.multimodal_contract import (
    MultimodalContractError,
    attachment_manifest,
    canonical_request_content,
    infer_mime_type,
    modality_for_attachment,
)
from orchestrator.runtime_common import _print_user_message, _safe_excerpt


MULTIMODAL_BATCH_SOURCE = "multimodal"
LONG_BATCH_IDLE = "idle"
LONG_BATCH_OPEN = "open"
LONG_BATCH_CLOSING = "closing"
DEFAULT_MEDIA_QUIET_SECONDS = 2.0
MULTIMODAL_BATCH_HEADER = """[Multimodal batch]
The user intentionally grouped the following messages and media into one request.
Treat every item as shared context for one task, but do not inspect an attachment merely because it exists. Decide whether the user's task depends on its contents.
When content understanding is needed, use the least costly suitable capability: OCR for visible text only, native image input when available, or vision_inspect for non-text visual meaning on a text-only backend.
If the task compares or checks all attached images, inspect every relevant image separately and keep the per-image findings identifiable. Never infer image contents from filenames, captions, or transport receipts.
Each Transport receipt below proves intake only; it is not semantic evidence about the attachment's contents.
Return one consolidated response. Do not answer the items separately unless the user's task explicitly asks for separate outputs.
If there is no explicit task text, analyze the grouped media together and explain the combined result.
"""

_MEDIA_RESPONSE_REWRITES = (
    (
        "Extract the text, analyze the contents thoroughly, and respond.",
        "Use this document as attachment context for the combined task.",
    ),
    ("Read the raw contents carefully and respond.", "Use this file as attachment context for the combined task."),
    ("Attempt to read the file and respond.", "Use this file as attachment context for the combined task."),
    ("View the image carefully and respond.", "Use this image as attachment context for the combined task."),
    ("View the image and respond.", "Use this image as attachment context for the combined task."),
    (
        "Listen to the audio, transcribe it, and respond.",
        "Listen to the audio and transcribe it for the combined task.",
    ),
    ("Watch the video and respond.", "Watch the video for the combined task."),
    ("Read it if possible and respond.", "Read it if possible for the combined task."),
    ("React warmly.", "Interpret the sticker as part of the combined task."),
)


@dataclass(frozen=True)
class LongBatchSubmission:
    chat_id: int
    prompt: str
    source: str
    summary: str
    text_count: int
    media_count: int
    batch_id: str
    request_content: dict[str, Any]
    attachment_manifest: tuple[dict[str, Any], ...] = ()
    attachment_receipts: tuple[dict[str, Any], ...] = ()

    @property
    def item_count(self) -> int:
        return self.text_count + self.media_count

    @property
    def line_count(self) -> int:
        return len(self.prompt.splitlines())

    @property
    def request_metadata(self) -> dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "text_count": self.text_count,
            "media_count": self.media_count,
            "attachment_receipts": [dict(receipt) for receipt in self.attachment_receipts],
            "canonical_content_version": self.request_content["version"],
            "attachment_manifest": [
                dict(attachment) for attachment in self.attachment_manifest
            ],
            "response_contract": "one_consolidated_report",
        }


def _ensure_batch_state(runtime: Any) -> None:
    buffer = getattr(runtime, "_long_buffer", None)
    if not isinstance(buffer, list):
        buffer = []
        runtime._long_buffer = buffer

    kinds = getattr(runtime, "_long_buffer_kinds", None)
    if not isinstance(kinds, list) or len(kinds) != len(buffer):
        # Older runtimes only had _long_buffer, whose entries were all text.
        kinds = ["text"] * len(buffer)
        runtime._long_buffer_kinds = kinds

    summaries = getattr(runtime, "_long_buffer_summaries", None)
    if not isinstance(summaries, list) or len(summaries) != len(buffer):
        summaries = [""] * len(buffer)
        runtime._long_buffer_summaries = summaries

    item_ids = getattr(runtime, "_long_buffer_ids", None)
    if not isinstance(item_ids, list) or len(item_ids) != len(buffer):
        runtime._long_buffer_ids = [None] * len(buffer)

    metadata = getattr(runtime, "_long_buffer_metadata", None)
    if not isinstance(metadata, list) or len(metadata) != len(buffer):
        runtime._long_buffer_metadata = [None] * len(buffer)

    pending = getattr(runtime, "_long_pending_voice_keys", None)
    if not isinstance(pending, set):
        runtime._long_pending_voice_keys = set()

    pending_media = getattr(runtime, "_long_pending_media_ids", None)
    if not isinstance(pending_media, set):
        runtime._long_pending_media_ids = set()

    state = str(getattr(runtime, "_long_buffer_state", "") or "").strip().lower()
    if state not in {LONG_BATCH_IDLE, LONG_BATCH_OPEN, LONG_BATCH_CLOSING}:
        state = LONG_BATCH_OPEN if bool(getattr(runtime, "_long_buffer_active", False)) else LONG_BATCH_IDLE
        runtime._long_buffer_state = state

    if not hasattr(runtime, "_long_batch_id"):
        runtime._long_batch_id = None
    if not hasattr(runtime, "_long_finalize_task"):
        runtime._long_finalize_task = None
    if not hasattr(runtime, "_long_finalize_update"):
        runtime._long_finalize_update = None
    if not hasattr(runtime, "_long_finalize_reason"):
        runtime._long_finalize_reason = None
    if not hasattr(runtime, "_long_finalize_discarded_voice"):
        runtime._long_finalize_discarded_voice = 0


def _cancel_task(runtime: Any, attribute: str) -> None:
    task = getattr(runtime, attribute, None)
    try:
        current_task = asyncio.current_task()
    except RuntimeError:
        current_task = None
    if task is not None and task is not current_task and not task.done():
        task.cancel()
    if task is not current_task:
        setattr(runtime, attribute, None)


def begin_batch(runtime: Any, chat_id: int, initial_text: str = "") -> None:
    _ensure_batch_state(runtime)
    _cancel_task(runtime, "_long_finalize_task")
    _cancel_task(runtime, "_long_buffer_timeout_task")
    discard_pending_voice_confirmations(runtime)
    runtime._long_buffer = []
    runtime._long_buffer_kinds = []
    runtime._long_buffer_summaries = []
    runtime._long_buffer_ids = []
    runtime._long_buffer_metadata = []
    runtime._long_pending_media_ids = set()
    runtime._long_buffer_active = True
    runtime._long_buffer_state = LONG_BATCH_OPEN
    runtime._long_buffer_chat_id = chat_id
    runtime._long_batch_id = f"long-{uuid4().hex}"
    runtime._long_finalize_update = None
    runtime._long_finalize_reason = None
    runtime._long_finalize_discarded_voice = 0
    if initial_text.strip():
        collect_text(runtime, chat_id, initial_text)


def is_collecting(runtime: Any, chat_id: int | None = None) -> bool:
    _ensure_batch_state(runtime)
    if getattr(runtime, "_long_buffer_state", LONG_BATCH_IDLE) != LONG_BATCH_OPEN:
        return False
    if chat_id is None:
        return True
    return getattr(runtime, "_long_buffer_chat_id", None) == chat_id


def is_batch_active(runtime: Any, chat_id: int | None = None) -> bool:
    _ensure_batch_state(runtime)
    if getattr(runtime, "_long_buffer_state", LONG_BATCH_IDLE) not in {
        LONG_BATCH_OPEN,
        LONG_BATCH_CLOSING,
    }:
        return False
    if chat_id is None:
        return True
    return getattr(runtime, "_long_buffer_chat_id", None) == chat_id


def accepts_media(runtime: Any, chat_id: int) -> bool:
    return is_batch_active(runtime, chat_id)


def collect_text(runtime: Any, chat_id: int, text: str) -> bool:
    if not is_collecting(runtime, chat_id):
        return False
    _ensure_batch_state(runtime)
    runtime._long_buffer.append(str(text or ""))
    runtime._long_buffer_kinds.append("text")
    runtime._long_buffer_summaries.append(_safe_excerpt(str(text or "")))
    runtime._long_buffer_ids.append(None)
    runtime._long_buffer_metadata.append(None)
    return True


def _batch_media_prompt(prompt: str) -> str:
    batch_prompt = str(prompt or "")
    for response_instruction, batch_instruction in _MEDIA_RESPONSE_REWRITES:
        batch_prompt = batch_prompt.replace(response_instruction, batch_instruction)
    return batch_prompt


def _receipt_transport_fields(transport_metadata: dict[str, Any] | None) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    for key, value in dict(transport_metadata or {}).items():
        if value is None or value == "":
            continue
        if isinstance(value, (str, int, float, bool)):
            fields[str(key)] = value
    return fields


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _schedule_closing_finalize(runtime: Any) -> None:
    if getattr(runtime, "_long_buffer_state", LONG_BATCH_IDLE) != LONG_BATCH_CLOSING:
        return
    _cancel_task(runtime, "_long_finalize_task")
    batch_id = str(getattr(runtime, "_long_batch_id", "") or "")
    runtime._long_finalize_task = asyncio.create_task(
        _finalize_after_quiet(runtime, batch_id)
    )


def reserve_media(
    runtime: Any,
    chat_id: int,
    media_kind: str,
    summary: str,
    *,
    transport_metadata: dict[str, Any] | None = None,
) -> str | None:
    if not accepts_media(runtime, chat_id):
        return None
    _ensure_batch_state(runtime)
    kind = str(media_kind or "media").strip().lower() or "media"
    item_id = f"attachment-{uuid4().hex}"
    receipt = {
        "receipt_id": item_id,
        "attachment_id": item_id,
        "status": "pending",
        "kind": kind,
        "summary": str(summary or "").strip(),
        **_receipt_transport_fields(transport_metadata),
    }
    runtime._long_buffer.append("")
    runtime._long_buffer_kinds.append(kind)
    runtime._long_buffer_summaries.append(str(summary or "").strip())
    runtime._long_buffer_ids.append(item_id)
    runtime._long_buffer_metadata.append(receipt)
    runtime._long_pending_media_ids.add(item_id)
    # A media handler may begin after /end because Telegram delivered an album
    # as several updates. Cancel the quiet timer while this download is pending.
    if getattr(runtime, "_long_buffer_state", LONG_BATCH_IDLE) == LONG_BATCH_CLOSING:
        _cancel_task(runtime, "_long_finalize_task")
    return item_id


def complete_media(
    runtime: Any,
    reservation_id: str,
    prompt: str,
    *,
    local_path: str | Path | None = None,
) -> bool:
    _ensure_batch_state(runtime)
    try:
        index = runtime._long_buffer_ids.index(reservation_id)
    except ValueError:
        runtime._long_pending_media_ids.discard(reservation_id)
        return False

    runtime._long_buffer[index] = _batch_media_prompt(prompt)
    receipt = dict(runtime._long_buffer_metadata[index] or {})
    receipt["status"] = "received"
    if local_path is not None:
        path = Path(local_path)
        receipt["local_ref"] = str(path)
        # Compatibility for request metadata consumers during rolling upgrade.
        receipt["local_path"] = str(path)
        try:
            receipt["size_bytes"] = path.stat().st_size
            receipt["sha256"] = _sha256_file(path)
            modality = modality_for_attachment(
                str(receipt.get("kind") or ""),
                mime_type=str(receipt.get("mime_type") or ""),
                filename=str(receipt.get("filename") or path.name),
            )
            receipt["modality"] = modality
            receipt["mime_type"] = str(
                receipt.get("mime_type")
                or infer_mime_type(path.name, modality=modality)
            ).casefold()
        except OSError:
            # The path remains useful evidence even if metadata collection races
            # with an external cleanup. Content processing will fail explicitly.
            receipt["integrity"] = "unavailable"
    runtime._long_buffer_metadata[index] = receipt
    runtime._long_pending_media_ids.discard(reservation_id)
    logger = getattr(runtime, "logger", None)
    if logger is not None:
        logger.info(
            f"Collected {runtime._long_buffer_kinds[index]} in /long batch "
            f"(chat_id={runtime._long_buffer_chat_id}, items={len(runtime._long_buffer)}, "
            f"receipt_id={reservation_id})"
        )
    _schedule_closing_finalize(runtime)
    return True


def discard_media_reservation(runtime: Any, reservation_id: str) -> None:
    _ensure_batch_state(runtime)
    runtime._long_pending_media_ids.discard(reservation_id)
    _remove_pending_placeholder(runtime, reservation_id)
    _schedule_closing_finalize(runtime)


def collect_media(
    runtime: Any,
    chat_id: int,
    prompt: str,
    media_kind: str,
    summary: str,
    *,
    local_path: str | Path | None = None,
    transport_metadata: dict[str, Any] | None = None,
) -> bool:
    reservation_id = reserve_media(
        runtime,
        chat_id,
        media_kind,
        summary,
        transport_metadata=transport_metadata,
    )
    if reservation_id is None:
        return False
    return complete_media(
        runtime,
        reservation_id,
        prompt,
        local_path=local_path,
    )


def register_voice_confirmation(
    runtime: Any,
    *,
    chat_id: int,
    prompt: str,
    transcript: str,
    summary: str,
) -> str | None:
    if not is_collecting(runtime, chat_id):
        return None
    _ensure_batch_state(runtime)
    pending_voice = getattr(runtime, "_pending_voice", None)
    if not isinstance(pending_voice, dict):
        pending_voice = {}
        runtime._pending_voice = pending_voice
    pending_key = f"long-{uuid4().hex[:12]}"
    # Reserve the original Telegram ordering position without exposing the
    # transcript to the eventual model request before SafeVoice approval.
    runtime._long_buffer.append("")
    runtime._long_buffer_kinds.append("pending_voice")
    runtime._long_buffer_summaries.append(str(summary or "").strip())
    runtime._long_buffer_ids.append(pending_key)
    runtime._long_buffer_metadata.append(None)
    pending_voice[pending_key] = {
        "prompt": prompt,
        "transcript": transcript,
        "summary": summary,
        "chat_id": chat_id,
        "long_batch": True,
    }
    runtime._long_pending_voice_keys.add(pending_key)
    return pending_key


def resolve_voice_confirmation(runtime: Any, pending_key: str, pending: dict[str, Any]) -> bool:
    _ensure_batch_state(runtime)
    runtime._long_pending_voice_keys.discard(pending_key)
    try:
        chat_id = int(pending.get("chat_id"))
    except (TypeError, ValueError):
        _remove_pending_placeholder(runtime, pending_key)
        return False
    if not is_collecting(runtime, chat_id):
        _remove_pending_placeholder(runtime, pending_key)
        return False
    try:
        index = runtime._long_buffer_ids.index(pending_key)
    except ValueError:
        return collect_media(
            runtime,
            chat_id,
            str(pending.get("prompt") or ""),
            "voice_transcript",
            str(pending.get("summary") or "Voice message"),
        )
    runtime._long_buffer[index] = str(pending.get("prompt") or "")
    runtime._long_buffer_kinds[index] = "voice_transcript"
    runtime._long_buffer_summaries[index] = str(pending.get("summary") or "Voice message")
    runtime._long_buffer_ids[index] = None
    runtime._long_buffer_metadata[index] = None
    return True


def _remove_pending_placeholder(runtime: Any, pending_key: str) -> None:
    _ensure_batch_state(runtime)
    try:
        index = runtime._long_buffer_ids.index(pending_key)
    except ValueError:
        return
    del runtime._long_buffer[index]
    del runtime._long_buffer_kinds[index]
    del runtime._long_buffer_summaries[index]
    del runtime._long_buffer_ids[index]
    del runtime._long_buffer_metadata[index]


def discard_voice_confirmation(runtime: Any, pending_key: str) -> None:
    _ensure_batch_state(runtime)
    runtime._long_pending_voice_keys.discard(pending_key)
    _remove_pending_placeholder(runtime, pending_key)


def pending_voice_count(runtime: Any) -> int:
    _ensure_batch_state(runtime)
    return len(runtime._long_pending_voice_keys)


def discard_pending_voice_confirmations(runtime: Any) -> int:
    _ensure_batch_state(runtime)
    keys = set(runtime._long_pending_voice_keys)
    pending_voice = getattr(runtime, "_pending_voice", None)
    for key in keys:
        if isinstance(pending_voice, dict):
            pending_voice.pop(key, None)
        _remove_pending_placeholder(runtime, key)
    runtime._long_pending_voice_keys.clear()
    return len(keys)


def _media_label(kind: str, summary: str) -> str:
    labels = {
        "audio": "Audio",
        "document": "Document",
        "photo": "Photo",
        "sticker": "Sticker",
        "video": "Video",
        "voice": "Voice message",
        "voice_transcript": "Voice transcript",
    }
    label = labels.get(kind, kind.replace("_", " ").title() or "Media")
    return f"{label}: {summary}" if summary else label


def _build_multimodal_prompt(
    buffer: list[str],
    kinds: list[str],
    summaries: list[str],
    metadata: list[dict[str, Any] | None],
) -> str:
    rendered_items: list[str] = []
    for index, (content, kind, summary, receipt) in enumerate(
        zip(buffer, kinds, summaries, metadata),
        start=1,
    ):
        if kind == "text":
            label = "User text"
        else:
            label = _media_label(kind, summary)
        parts = [f"[Item {index} — {label}]", content.strip()]
        if isinstance(receipt, dict):
            transport_receipt = dict(receipt)
            transport_receipt["item_index"] = index
            parts.append(
                "[Transport receipt]\n"
                + json.dumps(transport_receipt, ensure_ascii=False, sort_keys=True)
            )
        rendered_items.append("\n".join(part for part in parts if part))
    return f"{MULTIMODAL_BATCH_HEADER}\n" + "\n\n".join(rendered_items) + "\n\n[End multimodal batch]"


def _build_canonical_batch_content(
    buffer: list[str],
    kinds: list[str],
    metadata: list[dict[str, Any] | None],
) -> dict[str, Any]:
    parts: list[dict[str, Any]] = []
    for index, (content, kind, receipt) in enumerate(
        zip(buffer, kinds, metadata), start=1
    ):
        if kind == "text" or not isinstance(receipt, dict) or not receipt.get(
            "local_ref"
        ):
            if str(content or ""):
                parts.append(
                    {"type": "text", "item_index": index, "text": str(content)}
                )
            continue
        modality = str(
            receipt.get("modality")
            or modality_for_attachment(
                kind,
                mime_type=str(receipt.get("mime_type") or ""),
                filename=str(receipt.get("filename") or ""),
            )
        )
        parts.append(
            {
                "type": "media",
                "item_index": index,
                "attachment_id": str(
                    receipt.get("attachment_id") or receipt.get("receipt_id") or ""
                ),
                "modality": modality,
                "kind": kind,
                "mime_type": str(
                    receipt.get("mime_type")
                    or infer_mime_type(
                        str(receipt.get("filename") or receipt.get("local_ref") or ""),
                        modality=modality,
                    )
                ),
                "filename": str(receipt.get("filename") or ""),
                "caption": str(receipt.get("caption") or ""),
                "local_ref": str(receipt["local_ref"]),
                "size_bytes": int(receipt.get("size_bytes") or 0),
                "sha256": str(receipt.get("sha256") or ""),
                "transport": {
                    key: receipt[key]
                    for key in ("update_id", "message_id", "media_group_id")
                    if receipt.get(key) not in (None, "")
                },
            }
        )
    return canonical_request_content(parts)


def consume_batch(runtime: Any, fallback_chat_id: int) -> LongBatchSubmission | None:
    _ensure_batch_state(runtime)
    _cancel_task(runtime, "_long_buffer_timeout_task")
    _cancel_task(runtime, "_long_finalize_task")
    buffer = list(runtime._long_buffer)
    kinds = list(runtime._long_buffer_kinds)
    summaries = list(runtime._long_buffer_summaries)
    metadata = list(runtime._long_buffer_metadata)
    batch_id = str(getattr(runtime, "_long_batch_id", "") or f"long-{uuid4().hex}")
    chat_id = getattr(runtime, "_long_buffer_chat_id", None)
    if chat_id is None:
        chat_id = fallback_chat_id

    runtime._long_buffer = []
    runtime._long_buffer_kinds = []
    runtime._long_buffer_summaries = []
    runtime._long_buffer_ids = []
    runtime._long_buffer_metadata = []
    runtime._long_pending_media_ids = set()
    runtime._long_buffer_active = False
    runtime._long_buffer_state = LONG_BATCH_IDLE
    runtime._long_buffer_chat_id = None
    runtime._long_batch_id = None
    runtime._long_finalize_update = None
    runtime._long_finalize_reason = None

    nonempty = [bool(str(item or "").strip()) for item in buffer]
    if not any(nonempty):
        return None

    filtered = [
        (content, kind, summary, receipt)
        for content, kind, summary, receipt, keep in zip(
            buffer,
            kinds,
            summaries,
            metadata,
            nonempty,
        )
        if keep
    ]
    buffer = [item[0] for item in filtered]
    kinds = [item[1] for item in filtered]
    summaries = [item[2] for item in filtered]
    metadata = [item[3] for item in filtered]
    text_count = sum(kind == "text" for kind in kinds)
    media_count = len(kinds) - text_count

    if media_count:
        prompt = _build_multimodal_prompt(buffer, kinds, summaries, metadata)
        user_text = "\n".join(content for content, kind in zip(buffer, kinds) if kind == "text").strip()
        detail = _safe_excerpt(user_text) if user_text else "grouped media"
        summary = f"Multimodal batch ({text_count} text, {media_count} media): {detail}"
        source = MULTIMODAL_BATCH_SOURCE
    else:
        prompt = "\n".join(buffer).strip()
        summary = _safe_excerpt(prompt)
        source = "text"

    request_content = _build_canonical_batch_content(buffer, kinds, metadata)
    manifest = attachment_manifest(request_content)

    return LongBatchSubmission(
        chat_id=int(chat_id),
        prompt=prompt,
        source=source,
        summary=summary,
        text_count=text_count,
        media_count=media_count,
        batch_id=batch_id,
        request_content=request_content,
        attachment_manifest=manifest,
        attachment_receipts=tuple(
            {**dict(receipt), "item_index": index}
            for index, (kind, receipt) in enumerate(zip(kinds, metadata), start=1)
            if kind != "text" and isinstance(receipt, dict)
        ),
    )


async def cmd_long(runtime: Any, update: Any, context: Any) -> None:
    if not runtime._is_authorized_user(update.effective_user.id):
        return
    chat_id = update.effective_chat.id
    if is_batch_active(runtime):
        if is_batch_active(runtime, chat_id):
            if getattr(runtime, "_long_buffer_state", LONG_BATCH_IDLE) == LONG_BATCH_CLOSING:
                await runtime._reply_text(
                    update,
                    "⏳ The previous /long batch is still finalizing its media intake.",
                )
            else:
                await runtime._reply_text(update, "⏳ Already in /long mode. Send /end to finish.")
        else:
            await runtime._reply_text(update, "⏳ A /long batch is already active in another chat.")
        return

    args_text = " ".join(context.args).strip() if context.args else ""
    begin_batch(runtime, chat_id, args_text)
    timeout_task = getattr(runtime, "_long_buffer_timeout_task", None)
    if timeout_task and not timeout_task.done():
        timeout_task.cancel()
    runtime._long_buffer_timeout_task = asyncio.create_task(long_buffer_timeout(runtime))
    await runtime._reply_text(
        update,
        "📝 /long batch started. Send text, documents, photos, audio, video, or stickers, then send /end to submit everything as one request.",
    )


def _quiet_seconds(runtime: Any) -> float:
    try:
        return max(
            0.0,
            float(getattr(runtime, "_long_batch_quiet_seconds", DEFAULT_MEDIA_QUIET_SECONDS)),
        )
    except (TypeError, ValueError):
        return DEFAULT_MEDIA_QUIET_SECONDS


async def _finish_batch(runtime: Any, batch_id: str) -> None:
    _ensure_batch_state(runtime)
    if str(getattr(runtime, "_long_batch_id", "") or "") != batch_id:
        return
    if getattr(runtime, "_long_buffer_state", LONG_BATCH_IDLE) != LONG_BATCH_CLOSING:
        return
    if runtime._long_pending_media_ids:
        return

    chat_id = getattr(runtime, "_long_buffer_chat_id", None)
    if chat_id is None:
        return
    reason = str(getattr(runtime, "_long_finalize_reason", "user") or "user")
    update = getattr(runtime, "_long_finalize_update", None)
    discarded_voice = int(getattr(runtime, "_long_finalize_discarded_voice", 0) or 0)
    try:
        submission = consume_batch(runtime, int(chat_id))
    except MultimodalContractError as exc:
        attachment = (
            f" for attachment {exc.attachment_id}" if exc.attachment_id else ""
        )
        message = (
            "⚠️ /long media validation failed"
            f"{attachment} ({exc.code}). Nothing was submitted."
        )
        logger = getattr(runtime, "logger", None)
        if logger is not None:
            logger.warning(f"{message} {exc}")
        if update is not None:
            await runtime._reply_text(update, message)
        else:
            await runtime.send_long_message(
                int(chat_id),
                message,
                request_id=f"long-invalid-{uuid4().hex[:8]}",
                purpose="long-invalid-media",
            )
        return
    if submission is None:
        if reason == "timeout":
            suffix = (
                f" {discarded_voice} unconfirmed voice transcript(s) were discarded."
                if discarded_voice
                else ""
            )
            await runtime.send_long_message(
                int(chat_id),
                f"⏰ /long timed out with empty buffer. Cancelled.{suffix}",
                request_id=f"long-timeout-{uuid4().hex[:8]}",
                purpose="long-timeout",
            )
        elif update is not None:
            await runtime._reply_text(update, "⚠️ /long buffer was empty, nothing to submit.")
        return

    if submission.media_count:
        details = (
            f"✅ Collected {submission.text_count} text message(s) and "
            f"{submission.media_count} media item(s). Submitting as one request..."
        )
    else:
        details = f"✅ Collected {submission.line_count} lines. Submitting..."

    if reason == "timeout":
        timeout_details = (
            f"{submission.text_count} text message(s), {submission.media_count} media item(s)"
            if submission.media_count
            else f"{submission.line_count} lines"
        )
        suffix = (
            f"; discarded {discarded_voice} unconfirmed voice transcript(s)"
            if discarded_voice
            else ""
        )
        await runtime.send_long_message(
            submission.chat_id,
            f"⏰ /long auto-submitted after 5min timeout ({timeout_details}{suffix}).",
            request_id=f"long-timeout-{uuid4().hex[:8]}",
            purpose="long-timeout",
        )
    elif update is not None:
        await runtime._reply_text(update, details)

    _print_user_message(runtime.name, submission.prompt)
    logger = getattr(runtime, "logger", None)
    if logger is not None:
        logger.info(
            f"Submitting /long batch {submission.batch_id} "
            f"(text_count={submission.text_count}, media_count={submission.media_count}, "
            f"receipt_count={len(submission.attachment_receipts)})"
        )
    enqueue_kwargs = (
        {"request_metadata": submission.request_metadata}
        if submission.media_count
        else {}
    )
    await runtime.enqueue_request(
        submission.chat_id,
        submission.prompt,
        submission.source,
        submission.summary,
        request_content=submission.request_content,
        **enqueue_kwargs,
    )


async def _finalize_after_quiet(runtime: Any, batch_id: str) -> None:
    current_task = asyncio.current_task()
    try:
        await asyncio.sleep(_quiet_seconds(runtime))
        await _finish_batch(runtime, batch_id)
    except asyncio.CancelledError:
        return
    finally:
        if getattr(runtime, "_long_finalize_task", None) is current_task:
            runtime._long_finalize_task = None


async def cmd_end(runtime: Any, update: Any, context: Any) -> None:
    del context
    if not runtime._is_authorized_user(update.effective_user.id):
        return
    chat_id = update.effective_chat.id
    if not is_batch_active(runtime, chat_id):
        await runtime._reply_text(update, "No /long session active in this chat.")
        return
    if getattr(runtime, "_long_buffer_state", LONG_BATCH_IDLE) == LONG_BATCH_CLOSING:
        await runtime._reply_text(update, "⏳ /long is already finalizing media intake.")
        return
    unconfirmed = pending_voice_count(runtime)
    if unconfirmed:
        await runtime._reply_text(
            update,
            f"⚠️ Confirm or discard {unconfirmed} pending voice transcript(s) before /end.",
        )
        return

    _cancel_task(runtime, "_long_buffer_timeout_task")
    runtime._long_buffer_state = LONG_BATCH_CLOSING
    runtime._long_finalize_update = update
    runtime._long_finalize_reason = "user"
    runtime._long_finalize_discarded_voice = 0
    batch_id = str(runtime._long_batch_id or "")
    await runtime._reply_text(
        update,
        "⏳ Finishing /long media intake; late items from the same album will stay in this batch...",
    )
    if _quiet_seconds(runtime) <= 0 and not runtime._long_pending_media_ids:
        await _finish_batch(runtime, batch_id)
    else:
        _schedule_closing_finalize(runtime)


async def long_buffer_timeout(runtime: Any) -> None:
    try:
        await asyncio.sleep(300)
    except asyncio.CancelledError:
        return
    if not is_collecting(runtime):
        return

    chat_id = getattr(runtime, "_long_buffer_chat_id", None)
    discarded_voice = discard_pending_voice_confirmations(runtime)
    runtime._long_buffer_timeout_task = None
    if chat_id is None:
        return
    runtime._long_buffer_state = LONG_BATCH_CLOSING
    runtime._long_finalize_update = None
    runtime._long_finalize_reason = "timeout"
    runtime._long_finalize_discarded_voice = discarded_voice
    batch_id = str(runtime._long_batch_id or "")
    if runtime._long_pending_media_ids:
        _schedule_closing_finalize(runtime)
        return
    await _finish_batch(runtime, batch_id)
