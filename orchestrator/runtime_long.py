from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from orchestrator.runtime_common import _print_user_message, _safe_excerpt


MULTIMODAL_BATCH_SOURCE = "multimodal"
MULTIMODAL_BATCH_HEADER = """[Multimodal batch]
The user intentionally grouped the following messages and media into one request.
Treat every item as shared context for one task. Read or inspect every referenced file before replying, and consider relationships across the items.
Return one consolidated response. Do not answer the items separately unless the user's task explicitly asks for separate outputs.
If there is no explicit task text, analyze the grouped media together and explain the combined result.
"""

_MEDIA_RESPONSE_REWRITES = (
    (
        "Extract the text, analyze the contents thoroughly, and respond.",
        "Extract the text and analyze the contents thoroughly for the combined task.",
    ),
    ("Read the raw contents carefully and respond.", "Read the raw contents carefully for the combined task."),
    ("Attempt to read the file and respond.", "Attempt to read the file for the combined task."),
    ("View the image carefully and respond.", "View the image carefully for the combined task."),
    ("View the image and respond.", "View the image for the combined task."),
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

    @property
    def item_count(self) -> int:
        return self.text_count + self.media_count

    @property
    def line_count(self) -> int:
        return len(self.prompt.splitlines())


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

    pending = getattr(runtime, "_long_pending_voice_keys", None)
    if not isinstance(pending, set):
        runtime._long_pending_voice_keys = set()


def begin_batch(runtime: Any, chat_id: int, initial_text: str = "") -> None:
    _ensure_batch_state(runtime)
    discard_pending_voice_confirmations(runtime)
    runtime._long_buffer = []
    runtime._long_buffer_kinds = []
    runtime._long_buffer_summaries = []
    runtime._long_buffer_ids = []
    runtime._long_buffer_active = True
    runtime._long_buffer_chat_id = chat_id
    if initial_text.strip():
        collect_text(runtime, chat_id, initial_text)


def is_collecting(runtime: Any, chat_id: int | None = None) -> bool:
    if not bool(getattr(runtime, "_long_buffer_active", False)):
        return False
    if chat_id is None:
        return True
    return getattr(runtime, "_long_buffer_chat_id", None) == chat_id


def collect_text(runtime: Any, chat_id: int, text: str) -> bool:
    if not is_collecting(runtime, chat_id):
        return False
    _ensure_batch_state(runtime)
    runtime._long_buffer.append(str(text or ""))
    runtime._long_buffer_kinds.append("text")
    runtime._long_buffer_summaries.append(_safe_excerpt(str(text or "")))
    runtime._long_buffer_ids.append(None)
    return True


def collect_media(
    runtime: Any,
    chat_id: int,
    prompt: str,
    media_kind: str,
    summary: str,
) -> bool:
    if not is_collecting(runtime, chat_id):
        return False
    _ensure_batch_state(runtime)
    kind = str(media_kind or "media").strip().lower() or "media"
    batch_prompt = str(prompt or "")
    for response_instruction, batch_instruction in _MEDIA_RESPONSE_REWRITES:
        batch_prompt = batch_prompt.replace(response_instruction, batch_instruction)
    runtime._long_buffer.append(batch_prompt)
    runtime._long_buffer_kinds.append(kind)
    runtime._long_buffer_summaries.append(str(summary or "").strip())
    runtime._long_buffer_ids.append(None)
    logger = getattr(runtime, "logger", None)
    if logger is not None:
        logger.info(
            f"Collected {kind} in /long batch "
            f"(chat_id={chat_id}, items={len(runtime._long_buffer)}, summary={summary!r})"
        )
    return True


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
) -> str:
    rendered_items: list[str] = []
    for index, (content, kind, summary) in enumerate(zip(buffer, kinds, summaries), start=1):
        if kind == "text":
            label = "User text"
        else:
            label = _media_label(kind, summary)
        rendered_items.append(f"[Item {index} — {label}]\n{content.strip()}")
    return f"{MULTIMODAL_BATCH_HEADER}\n" + "\n\n".join(rendered_items) + "\n\n[End multimodal batch]"


def consume_batch(runtime: Any, fallback_chat_id: int) -> LongBatchSubmission | None:
    _ensure_batch_state(runtime)
    buffer = list(runtime._long_buffer)
    kinds = list(runtime._long_buffer_kinds)
    summaries = list(runtime._long_buffer_summaries)
    chat_id = getattr(runtime, "_long_buffer_chat_id", None)
    if chat_id is None:
        chat_id = fallback_chat_id

    runtime._long_buffer = []
    runtime._long_buffer_kinds = []
    runtime._long_buffer_summaries = []
    runtime._long_buffer_ids = []
    runtime._long_buffer_active = False
    runtime._long_buffer_chat_id = None

    nonempty = [bool(str(item or "").strip()) for item in buffer]
    if not any(nonempty):
        return None

    filtered = [
        (content, kind, summary)
        for content, kind, summary, keep in zip(buffer, kinds, summaries, nonempty)
        if keep
    ]
    buffer = [item[0] for item in filtered]
    kinds = [item[1] for item in filtered]
    summaries = [item[2] for item in filtered]
    text_count = sum(kind == "text" for kind in kinds)
    media_count = len(kinds) - text_count

    if media_count:
        prompt = _build_multimodal_prompt(buffer, kinds, summaries)
        user_text = "\n".join(content for content, kind in zip(buffer, kinds) if kind == "text").strip()
        detail = _safe_excerpt(user_text) if user_text else "grouped media"
        summary = f"Multimodal batch ({text_count} text, {media_count} media): {detail}"
        source = MULTIMODAL_BATCH_SOURCE
    else:
        prompt = "\n".join(buffer).strip()
        summary = _safe_excerpt(prompt)
        source = "text"

    return LongBatchSubmission(
        chat_id=int(chat_id),
        prompt=prompt,
        source=source,
        summary=summary,
        text_count=text_count,
        media_count=media_count,
    )


async def cmd_long(runtime: Any, update: Any, context: Any) -> None:
    if not runtime._is_authorized_user(update.effective_user.id):
        return
    chat_id = update.effective_chat.id
    if is_collecting(runtime):
        if is_collecting(runtime, chat_id):
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


async def cmd_end(runtime: Any, update: Any, context: Any) -> None:
    del context
    if not runtime._is_authorized_user(update.effective_user.id):
        return
    chat_id = update.effective_chat.id
    if not is_collecting(runtime, chat_id):
        await runtime._reply_text(update, "No /long session active in this chat.")
        return
    unconfirmed = pending_voice_count(runtime)
    if unconfirmed:
        await runtime._reply_text(
            update,
            f"⚠️ Confirm or discard {unconfirmed} pending voice transcript(s) before /end.",
        )
        return

    timeout_task = getattr(runtime, "_long_buffer_timeout_task", None)
    if timeout_task and not timeout_task.done():
        timeout_task.cancel()
    runtime._long_buffer_timeout_task = None
    submission = consume_batch(runtime, chat_id)
    if submission is None:
        await runtime._reply_text(update, "⚠️ /long buffer was empty, nothing to submit.")
        return

    if submission.media_count:
        collected = (
            f"✅ Collected {submission.text_count} text message(s) and "
            f"{submission.media_count} media item(s). Submitting as one request..."
        )
    else:
        collected = f"✅ Collected {submission.line_count} lines. Submitting..."
    await runtime._reply_text(update, collected)
    _print_user_message(runtime.name, submission.prompt)
    await runtime.enqueue_request(
        submission.chat_id,
        submission.prompt,
        submission.source,
        submission.summary,
    )


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
    submission = consume_batch(runtime, int(chat_id))
    if submission is None:
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
        return

    if submission.media_count:
        details = (
            f"{submission.text_count} text message(s), {submission.media_count} media item(s)"
        )
    else:
        details = f"{submission.line_count} lines"
    suffix = (
        f"; discarded {discarded_voice} unconfirmed voice transcript(s)"
        if discarded_voice
        else ""
    )
    await runtime.send_long_message(
        submission.chat_id,
        f"⏰ /long auto-submitted after 5min timeout ({details}{suffix}).",
        request_id=f"long-timeout-{uuid4().hex[:8]}",
        purpose="long-timeout",
    )
    _print_user_message(runtime.name, submission.prompt)
    await runtime.enqueue_request(
        submission.chat_id,
        submission.prompt,
        submission.source,
        submission.summary,
    )
