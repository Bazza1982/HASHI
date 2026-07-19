from __future__ import annotations

import asyncio
import json
import re
from typing import Any
from types import SimpleNamespace

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

_STEER_CMD_RE = re.compile(r"^/steer(?:@\w+)?\s*(.*)$", re.IGNORECASE | re.DOTALL)


def extract_steer_direction(update: Any, context: Any) -> str:
    """Return the free-text direction after /steer (preserves punctuation and newlines)."""
    message = getattr(update, "effective_message", None) or getattr(update, "message", None)
    text = str(getattr(message, "text", None) or getattr(message, "caption", None) or "").strip()
    if text:
        match = _STEER_CMD_RE.match(text)
        if match:
            return str(match.group(1) or "").strip()
    args = getattr(context, "args", None) or []
    return " ".join(str(a) for a in args if str(a).strip()).strip()


def build_steer_prompt(*, direction: str, original_prompt: str = "", backend: str = "") -> str:
    """Compose a mid-task course-correction prompt that keeps progress/artefacts."""
    direction = str(direction or "").strip()
    original = str(original_prompt or "").strip()
    backend_note = f"\nActive backend/engine at interrupt: {backend}" if backend else ""
    original_block = ""
    if original:
        # Bound size so steer stays usable on small-context models.
        clipped = original if len(original) <= 12000 else (original[:12000] + "\n…[original task truncated]")
        original_block = (
            "\n\n--- Original task context (for continuity; do not restart from zero) ---\n"
            f"{clipped}\n"
            "--- End original task context ---"
        )
    return (
        "[HASHI /steer — mid-task course correction]\n"
        "The user interrupted the previous turn to add direction. This is NOT a new blank task.\n"
        "Requirements:\n"
        "1. Stop the previous approach only where it conflicts with the new direction.\n"
        "2. KEEP all interim progress already made: workspace files, artefacts, tool results, "
        "CLI session state, partial answers, and thinking already produced.\n"
        "3. Do NOT call session-reset flows, wipe workspaces, or discard completed sub-steps "
        "unless the new direction explicitly requires it.\n"
        "4. Continue from the current state and incorporate the additional direction below.\n"
        f"{backend_note}\n\n"
        "Additional direction / requirement from the user:\n"
        f"{direction}"
        f"{original_block}"
    )


async def _shutdown_active_backend(runtime: Any) -> str:
    """Kill the active backend process/request tree. Returns a short status label."""
    # Flexible runtime path
    backend_manager = getattr(runtime, "backend_manager", None)
    if backend_manager is not None:
        current = getattr(backend_manager, "current_backend", None)
        if current is not None and hasattr(current, "shutdown"):
            await current.shutdown()
            return str(getattr(runtime.config, "active_backend", "") or "backend")
        return "none"

    # Fixed / legacy runtime path
    backend = getattr(runtime, "backend", None)
    if backend is not None and hasattr(backend, "shutdown"):
        await backend.shutdown()
        engine = getattr(getattr(runtime, "config", None), "engine", None)
        return str(engine or "backend")
    return "none"


async def _clear_request_queue(runtime: Any) -> int:
    dropped = 0
    queue = getattr(runtime, "queue", None)
    if queue is None:
        return 0
    while not queue.empty():
        try:
            queue.get_nowait()
            queue.task_done()
            dropped += 1
        except asyncio.QueueEmpty:
            break
    return dropped


async def _notify_interrupted(
    runtime: Any,
    *,
    reason: str,
    error: str,
    summary: str,
) -> None:
    meta = getattr(runtime, "current_request_meta", None)
    notify = getattr(runtime, "_notify_right_brain_interrupted", None)
    if not callable(notify):
        return
    if not isinstance(meta, dict) or not meta.get("request_id"):
        return
    try:
        item = SimpleNamespace(
            request_id=str(meta.get("request_id") or ""),
            chat_id=meta.get("chat_id"),
            prompt=str(meta.get("prompt") or ""),
            source=str(meta.get("source") or "text"),
            summary=str(meta.get("summary") or summary),
        )
        is_bridge_request = item.source.startswith("bridge:") or item.source.startswith("bridge-transfer:")
        notify(
            item,
            item.prompt,
            is_bridge_request=is_bridge_request,
            reason=reason,
            error=error,
        )
    except Exception as exc:
        runtime.logger.warning("Failed to notify interrupted turn for %s: %s", reason, exc)


def _capture_original_prompt(runtime: Any) -> str:
    meta = getattr(runtime, "current_request_meta", None)
    if isinstance(meta, dict):
        prompt = str(meta.get("prompt") or "").strip()
        if prompt:
            return prompt
    last_prompt = getattr(runtime, "last_prompt", None)
    if last_prompt is not None:
        prompt = str(getattr(last_prompt, "prompt", "") or "").strip()
        if prompt:
            return prompt
    return ""


async def cmd_stop(runtime: Any, update: Any, context: Any) -> None:
    if not runtime._is_authorized_user(update.effective_user.id):
        return

    active = getattr(runtime.config, "active_backend", None) or getattr(runtime.config, "engine", "")
    runtime.logger.warning(
        f"Manual stop requested for agent {runtime.name} "
        f"(queue_size={runtime.queue.qsize()}, backend={active})"
    )
    await _shutdown_active_backend(runtime)
    await _notify_interrupted(
        runtime,
        reason="user_stop",
        error="/stop received while right brain was running",
        summary="Manual stop",
    )

    dropped = await _clear_request_queue(runtime)

    await runtime._reply_text(
        update,
        f"Stopped execution. Cleared {dropped} queued messages and killed active backend process tree.",
    )


def _user_is_authorized(runtime: Any, update: Any) -> bool:
    checker = getattr(runtime, "_is_authorized_user", None)
    user = getattr(update, "effective_user", None)
    user_id = getattr(user, "id", None)
    if callable(checker):
        return bool(checker(user_id))
    authorized_id = getattr(getattr(runtime, "global_config", None), "authorized_id", None)
    if authorized_id is None:
        return True
    return user_id == authorized_id


async def _reply(runtime: Any, update: Any, text: str) -> None:
    if hasattr(runtime, "_reply_text"):
        await runtime._reply_text(update, text)
        return
    message = getattr(update, "effective_message", None) or getattr(update, "message", None)
    if message is not None and hasattr(message, "reply_text"):
        await message.reply_text(text)
        return
    chat = getattr(update, "effective_chat", None)
    chat_id = getattr(chat, "id", None)
    if chat_id is not None and hasattr(runtime, "send_long_message"):
        await runtime.send_long_message(chat_id, text, purpose="steer-command")


async def cmd_steer(runtime: Any, update: Any, context: Any) -> None:
    """Stop the active turn immediately, keep progress/artefacts, then queue new direction.

    Works for all agent backends/models: kills the active adapter process or HTTP
    generation, preserves workspace/session/transcript state, and enqueues a
    continuity-preserving prompt with the user's additional requirements.
    """
    if not _user_is_authorized(runtime, update):
        return

    direction = extract_steer_direction(update, context)
    if not direction:
        await _reply(
            runtime,
            update,
            "Usage: /steer <additional direction or requirement>\n"
            "Example: /steer also include unit tests for the auth module\n\n"
            "Stops the current turn immediately (like /stop), keeps interim thinking, "
            "progress, and artefacts, then continues with your new direction.",
        )
        return

    active = str(
        getattr(runtime.config, "active_backend", None)
        or getattr(runtime.config, "engine", "")
        or ""
    )
    original_prompt = _capture_original_prompt(runtime)
    was_running = bool(
        (isinstance(getattr(runtime, "current_request_meta", None), dict)
         and getattr(runtime, "current_request_meta", {}).get("request_id"))
        or getattr(runtime, "is_generating", False)
        or (getattr(runtime, "queue", None) is not None and not runtime.queue.empty())
    )

    runtime.logger.warning(
        f"Manual steer requested for agent {runtime.name} "
        f"(queue_size={runtime.queue.qsize()}, backend={active}, "
        f"direction_len={len(direction)}, had_original={bool(original_prompt)})"
    )

    await _shutdown_active_backend(runtime)
    await _notify_interrupted(
        runtime,
        reason="user_steer",
        error="/steer received while right brain was running",
        summary=f"Steer: {direction[:120]}",
    )
    dropped = await _clear_request_queue(runtime)

    # Best-effort re-init so the steered turn can start on all backends.
    backend_manager = getattr(runtime, "backend_manager", None)
    if backend_manager is not None and hasattr(backend_manager, "initialize_active_backend"):
        try:
            await backend_manager.initialize_active_backend()
        except Exception as exc:
            runtime.logger.warning("Steer re-init of active backend failed: %s", exc)

    steer_prompt = build_steer_prompt(
        direction=direction,
        original_prompt=original_prompt,
        backend=active,
    )
    chat = getattr(update, "effective_chat", None)
    chat_id = getattr(chat, "id", None)
    if chat_id is None:
        message = getattr(update, "effective_message", None) or getattr(update, "message", None)
        chat_id = getattr(getattr(message, "chat", None), "id", None)
    if chat_id is None:
        await _reply(runtime, update, "Steer aborted: could not resolve chat id.")
        return

    summary = f"Steer: {direction[:80]}"
    request_id = None
    if hasattr(runtime, "enqueue_request"):
        request_id = await runtime.enqueue_request(
            int(chat_id),
            steer_prompt,
            "steer",
            summary,
        )
    else:
        await _reply(runtime, update, "Steer aborted: runtime has no enqueue_request path.")
        return

    kept = "Kept interim progress, thinking, and workspace artefacts."
    state = "Interrupted active work" if was_running else "No active generation"
    await _reply(
        runtime,
        update,
        f"🧭 Steered.\n"
        f"{state}; cleared {dropped} queued message(s).\n"
        f"{kept}\n"
        f"Queued continuation with your new direction"
        f"{f' (request {request_id})' if request_id else ''}.",
    )


async def cmd_retry(runtime: Any, update: Any, context: Any) -> None:
    if not runtime._is_authorized_user(update.effective_user.id):
        return
    args = [a.strip().lower() for a in (context.args or []) if a.strip()]
    mode = args[0] if args else "response"
    chat_id = update.effective_chat.id
    if mode in {"response", "resp"}:
        await retry_response(runtime, update, chat_id)
        return
    if mode in {"prompt", "req", "request"}:
        await retry_prompt(runtime, update, chat_id)
        return
    markup = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("重发回复", callback_data="tgl:retry:response"),
                InlineKeyboardButton("重跑 Prompt", callback_data="tgl:retry:prompt"),
            ]
        ]
    )
    await runtime._reply_text(update, "Retry — choose action:", reply_markup=markup)


async def callback_retry_toggle(runtime: Any, query: Any, value: str) -> None:
    chat_id = query.message.chat_id
    await query.answer(f"Retrying {value}...")
    if value == "response":
        if runtime.last_response:
            await runtime.send_long_message(
                chat_id=runtime.last_response["chat_id"],
                text=runtime.last_response["text"],
                request_id=runtime.last_response.get("request_id"),
                purpose="retry-response",
            )
            return
        transcript_text = load_last_text_from_transcript(runtime, "assistant")
        if transcript_text:
            await runtime.send_long_message(chat_id=chat_id, text=transcript_text, purpose="retry-response")
        elif runtime.last_prompt:
            await runtime.enqueue_request(runtime.last_prompt.chat_id, runtime.last_prompt.prompt, "retry", "Retry request")
        else:
            await query.answer("Nothing to retry.", show_alert=True)
        return

    if runtime.last_prompt:
        await runtime.enqueue_request(runtime.last_prompt.chat_id, runtime.last_prompt.prompt, "retry", "Retry request")
        return
    transcript_text = load_last_text_from_transcript(runtime, "user")
    if transcript_text:
        await runtime.enqueue_request(chat_id, transcript_text, "retry", "Retry request")
    else:
        await query.answer("No previous prompt.", show_alert=True)


async def retry_response(runtime: Any, update: Any, chat_id: int) -> None:
    if not runtime.last_response:
        transcript_text = load_last_text_from_transcript(runtime, "assistant")
        if transcript_text:
            await runtime._reply_text(update, "Restoring last response from transcript...")
            await runtime.send_long_message(
                chat_id=chat_id,
                text=transcript_text,
                purpose="retry-response",
            )
            return
        if runtime.last_prompt:
            await runtime._reply_text(update, "No cached response — retrying last prompt...")
            await runtime.enqueue_request(
                runtime.last_prompt.chat_id,
                runtime.last_prompt.prompt,
                "retry",
                "Retry request",
            )
        else:
            await runtime._reply_text(update, "Nothing to retry — no previous response or prompt.")
        return

    await runtime._reply_text(update, "Resending last response...")
    await runtime.send_long_message(
        chat_id=runtime.last_response["chat_id"],
        text=runtime.last_response["text"],
        request_id=runtime.last_response.get("request_id"),
        purpose="retry-response",
    )


async def retry_prompt(runtime: Any, update: Any, chat_id: int) -> None:
    if not runtime.last_prompt:
        transcript_text = load_last_text_from_transcript(runtime, "user")
        if transcript_text:
            await runtime._reply_text(update, "Restoring last prompt from transcript...")
            await runtime.enqueue_request(chat_id, transcript_text, "retry", "Retry request")
        else:
            await runtime._reply_text(update, "No previous prompt to rerun.")
        return

    await runtime._reply_text(update, "Retrying last prompt...")
    await runtime.enqueue_request(
        runtime.last_prompt.chat_id,
        runtime.last_prompt.prompt,
        "retry",
        "Retry request",
    )


def load_last_text_from_transcript(runtime: Any, role: str) -> str | None:
    try:
        if not runtime.transcript_log_path.exists():
            return None
        last_text = None
        with open(runtime.transcript_log_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    if entry.get("role") == role and entry.get("text"):
                        last_text = entry["text"]
                except Exception:
                    pass
        return last_text
    except Exception:
        return None
