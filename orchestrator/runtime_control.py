from __future__ import annotations

import asyncio
import re
import time
from typing import Any
from types import SimpleNamespace

from orchestrator import runtime_retry, runtime_session
_STEER_CMD_RE = re.compile(r"^/steer(?:@\w+)?\s*(.*)$", re.IGNORECASE | re.DOTALL)

# Intentional user-driven interrupts that kill the active backend process.
# Exit codes such as -9 (SIGKILL) are expected and must not surface as Backend errors.
_USER_INTERRUPT_REASONS = frozenset(
    {"user_stop", "user_steer", "user_focus", "user_retry"}
)

_FOCUS_DIRECTION = (
    "Continue working on the original user task. /focus is a scope correction, not a "
    "stop, pause, cancellation, completion signal, or request for a status-only reply. "
    "Apply this reminder once and keep working:\n"
    "1. Treat the original user task shown below as the complete source of authority.\n"
    "2. Preserve its exact requested outcome and stated boundaries. Do not reinterpret "
    "/focus as a replacement task or permission to reduce the requested outcome.\n"
    "3. Resume immediately from the progress already made. Take the next concrete, "
    "in-scope action; do not merely explain a plan, summarize progress, or wrap up unless "
    "the original task itself requested that.\n"
    "4. Discontinue only actions that are outside the original scope. If the previous "
    "approach was too broad, choose a narrower in-scope path and continue the task.\n"
    "5. Do not treat earlier plans, recommendations, memories, open items, or model "
    "preferences as user authorization.\n"
    "6. Preserve all in-scope progress, files, artefacts, tool results, and session state. "
    "Do not reset, revert, delete, or clean up anything unless the user requested it.\n"
    "7. If unrequested work has already started, do not extend it. Mention it briefly; "
    "do not repair or remove it without permission.\n"
    "8. Continue autonomously through the smallest sufficient set of actions and "
    "proportionate verification until the original requested outcome is genuinely complete.\n"
    "9. Do not create extra branches, documents, refactors, audits, deployments, "
    "synchronisations, or follow-up improvements unless explicitly requested or "
    "technically unavoidable.\n"
    "10. Do not stop merely because the task is difficult, slow, uncertain, or partially "
    "complete. Stop only when the original outcome is complete or a genuine blocker "
    "requires new user authority or an external state change.\n"
    "11. If additional authority is genuinely required, ask the user before expanding scope. "
    "Otherwise, continue working without asking for confirmation.\n"
    "12. Once the original requested outcome is complete, report it concisely."
)


def mark_user_interrupt(runtime: Any, reason: str) -> None:
    """Record that the active turn is being intentionally stopped by the user."""
    reason = str(reason or "").strip()
    if reason not in _USER_INTERRUPT_REASONS:
        return
    meta = getattr(runtime, "current_request_meta", None)
    request_id = None
    if isinstance(meta, dict):
        rid = meta.get("request_id")
        if rid:
            request_id = str(rid)
    runtime._user_interrupt = {
        "reason": reason,
        "request_id": request_id,
        "at": time.time(),
    }


def peek_user_interrupt(runtime: Any, request_id: str | None = None) -> str | None:
    """Return interrupt reason if one is pending for this turn, without consuming it."""
    data = getattr(runtime, "_user_interrupt", None)
    if not isinstance(data, dict):
        return None
    reason = str(data.get("reason") or "").strip()
    if reason not in _USER_INTERRUPT_REASONS:
        return None
    marked_id = data.get("request_id")
    if request_id and marked_id and str(request_id) != str(marked_id):
        return None
    return reason


def consume_user_interrupt(runtime: Any, request_id: str | None = None) -> str | None:
    """Consume a matching intentional user interrupt; return reason or None."""
    reason = peek_user_interrupt(runtime, request_id)
    if reason is None:
        return None
    runtime._user_interrupt = None
    return reason


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


def _unwrap_focus_original(prompt: str) -> str:
    """Recover the original task when /focus is invoked more than once."""
    text = str(prompt or "").strip()
    marker_start = "--- Original user task"
    marker_end = "\n--- End original user task ---"
    start = text.rfind(marker_start)
    if start < 0:
        return text
    content_start = text.find("\n", start)
    if content_start < 0:
        return text
    end = text.find(marker_end, content_start)
    if end < 0:
        return text
    return text[content_start + 1 : end].strip()


def build_focus_prompt(*, original_prompt: str, backend: str = "") -> str:
    """Compose a one-off scope correction around the active or most recent task."""
    original = _unwrap_focus_original(original_prompt)
    backend_note = f"\nActive backend/engine at interrupt: {backend}" if backend else ""
    clipped = original if len(original) <= 12000 else (original[:12000] + "\n…[original task truncated]")
    return (
        "[HASHI /focus — one-off scope correction]\n"
        "The user invoked /focus to narrow execution without stopping the original task. "
        "This is not a new task and not a request to pause, cancel, wrap up, or only report "
        "status. Continue the original task after applying the scope correction."
        f"{backend_note}\n\n"
        f"{_FOCUS_DIRECTION}\n\n"
        "--- Original user task (continue this task; preserve progress; do not restart) ---\n"
        f"{clipped}\n"
        "--- End original user task ---"
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


async def _recall_request_queue(runtime: Any, count: int | None = None) -> int:
    """Remove all or the newest N waiting requests while preserving FIFO order."""
    if count is None:
        return await _clear_request_queue(runtime)

    queue = getattr(runtime, "queue", None)
    if queue is None or count <= 0:
        return 0

    waiting: list[Any] = []
    while not queue.empty():
        try:
            waiting.append(queue.get_nowait())
            queue.task_done()
        except asyncio.QueueEmpty:
            break

    dropped = min(count, len(waiting))
    keep_count = len(waiting) - dropped
    for item in waiting[:keep_count]:
        queue.put_nowait(item)
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
    # Mark before kill so the pipeline can suppress the expected non-zero exit
    # (e.g. Grok CLI code -9 / SIGKILL) instead of showing ❌ Backend error.
    busy = _agent_is_busy(runtime)
    interrupted_task = None
    if busy:
        interrupted_task = runtime_retry.remember_interrupted_task(
            runtime,
            getattr(runtime, "current_request_meta", None),
            backend=str(active or ""),
            reason="user_stop",
        )
        mark_user_interrupt(runtime, "user_stop")
    await _shutdown_active_backend(runtime)
    await _notify_interrupted(
        runtime,
        reason="user_stop",
        error="/stop received while right brain was running",
        summary="Manual stop",
    )

    dropped = await _clear_request_queue(runtime)

    continuation_note = (
        " The unfinished task was saved; send “continue” or “继续” to resume it."
        if interrupted_task is not None
        else ""
    )
    await runtime._reply_text(
        update,
        f"Stopped execution. Cleared {dropped} queued messages and killed active backend process tree."
        f"{continuation_note}",
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


def _agent_is_busy(runtime: Any) -> bool:
    """True when a generation is active or work is already queued."""
    meta = getattr(runtime, "current_request_meta", None)
    if isinstance(meta, dict) and meta.get("request_id"):
        return True
    if getattr(runtime, "is_generating", False):
        return True
    queue = getattr(runtime, "queue", None)
    if queue is not None and not queue.empty():
        return True
    return False


async def cmd_steer(
    runtime: Any,
    update: Any,
    context: Any,
    *,
    command_name: str = "steer",
) -> None:
    """Course-correct mid-task, or send a plain new request when idle.

    Busy: stop immediately, keep progress/artefacts, enqueue the steer wrapper.
    Idle: do not wrap — enqueue the direction text only as a new request.
    """
    if not _user_is_authorized(runtime, update):
        return

    focus_mode = command_name == "focus"
    direction = _FOCUS_DIRECTION if focus_mode else extract_steer_direction(update, context)
    if not direction:
        await _reply(
            runtime,
            update,
            "Usage: /steer <additional direction or requirement>\n"
            "Example: /steer also include unit tests for the auth module\n\n"
            "When busy: stops the current turn (like /stop), keeps interim thinking, "
            "progress, and artefacts, then continues with your new direction.\n"
            "When idle: sends your text as a new request without the mid-task wrapper.",
        )
        return

    active = str(
        getattr(runtime.config, "active_backend", None)
        or getattr(runtime.config, "engine", "")
        or ""
    )
    busy = _agent_is_busy(runtime)
    original_prompt = _capture_original_prompt(runtime) if (busy or focus_mode) else ""

    runtime.logger.warning(
        f"Manual {command_name} requested for agent {runtime.name} "
        f"(busy={busy}, queue_size={runtime.queue.qsize()}, backend={active}, "
        f"direction_len={len(direction)}, had_original={bool(original_prompt)})"
    )

    chat = getattr(update, "effective_chat", None)
    chat_id = getattr(chat, "id", None)
    if chat_id is None:
        message = getattr(update, "effective_message", None) or getattr(update, "message", None)
        chat_id = getattr(getattr(message, "chat", None), "id", None)
    if chat_id is None:
        await _reply(runtime, update, "Steer aborted: could not resolve chat id.")
        return

    if not busy and not focus_mode:
        # Idle: plain new direction — no mid-task wrapper, no interrupt path.
        if not hasattr(runtime, "enqueue_request"):
            await _reply(runtime, update, "Steer aborted: runtime has no enqueue_request path.")
            return
        request_id = await runtime.enqueue_request(
            int(chat_id),
            direction,
            "text",
            direction[:80],
        )
        await _reply(
            runtime,
            update,
            f"🧭 Agent was idle — queued your text as a new request"
            f"{f' ({request_id})' if request_id else ''} (no steer wrapper).",
        )
        return

    if not busy:
        if not original_prompt:
            await _reply(
                runtime,
                update,
                "🎯 Agent is already idle and no recent task was found. Nothing was queued.",
            )
            return
        request_id = await runtime.enqueue_request(
            int(chat_id),
            build_focus_prompt(original_prompt=original_prompt, backend=active),
            "focus",
            "Focus: narrow the most recent task to user intent",
        )
        await _reply(
            runtime,
            update,
            "🎯 Focus applied to the most recent task.\n"
            "Queued a one-off continuation that must keep working on the original outcome "
            "within its requested scope"
            f"{f' ({request_id})' if request_id else ''}.",
        )
        return

    # Mark before kill so exit -9 / SIGKILL is not reported as ❌ Backend error.
    interrupt_reason = "user_focus" if focus_mode else "user_steer"
    mark_user_interrupt(runtime, interrupt_reason)
    await _shutdown_active_backend(runtime)
    await _notify_interrupted(
        runtime,
        reason=interrupt_reason,
        error=f"/{command_name} received while right brain was running",
        summary=(
            "Focus: narrow current task to user intent"
            if focus_mode
            else f"Steer: {direction[:120]}"
        ),
    )
    dropped = await _clear_request_queue(runtime)

    # Best-effort re-init so the steered turn can start on all backends.
    backend_manager = getattr(runtime, "backend_manager", None)
    if backend_manager is not None and hasattr(backend_manager, "initialize_active_backend"):
        try:
            await backend_manager.initialize_active_backend()
        except Exception as exc:
            runtime.logger.warning("Steer re-init of active backend failed: %s", exc)

    continuation_prompt = (
        build_focus_prompt(original_prompt=original_prompt, backend=active)
        if focus_mode
        else build_steer_prompt(
            direction=direction,
            original_prompt=original_prompt,
            backend=active,
        )
    )
    summary = (
        "Focus: narrow current task to user intent"
        if focus_mode
        else f"Steer: {direction[:80]}"
    )
    if not hasattr(runtime, "enqueue_request"):
        await _reply(runtime, update, "Steer aborted: runtime has no enqueue_request path.")
        return
    request_id = await runtime.enqueue_request(
        int(chat_id),
        continuation_prompt,
        "focus" if focus_mode else "steer",
        summary,
    )

    if focus_mode:
        await _reply(
            runtime,
            update,
            "🎯 Focus applied.\n"
            f"Re-focused the active task; cleared {dropped} queued message(s).\n"
            "Preserved existing progress and queued an immediate continuation restricted "
            "to the original user-requested scope. The task must continue until its "
            "requested outcome is complete or genuinely blocked"
            f"{f' (request {request_id})' if request_id else ''}.",
        )
    else:
        await _reply(
            runtime,
            update,
            f"🧭 Steered.\n"
            f"Interrupted active work; cleared {dropped} queued message(s).\n"
            f"Kept interim progress, thinking, and workspace artefacts.\n"
            f"Queued continuation with your new direction"
            f"{f' (request {request_id})' if request_id else ''}.",
        )


async def cmd_focus(runtime: Any, update: Any, context: Any) -> None:
    """Apply a predefined one-off scope correction using the /steer control path."""
    await cmd_steer(runtime, update, context, command_name="focus")


async def cmd_recall(runtime: Any, update: Any, context: Any) -> None:
    """Remove waiting requests without interrupting the active task."""
    if not _user_is_authorized(runtime, update):
        return

    args = [str(arg).strip() for arg in (getattr(context, "args", None) or []) if str(arg).strip()]
    count: int | None = None
    if args:
        if len(args) != 1 or not re.fullmatch(r"[1-9]\d*", args[0]):
            await _reply(
                runtime,
                update,
                "Usage: /recall [count]\n"
                "/recall — remove every waiting request\n"
                "/recall 1 — remove the newest waiting request\n"
                "/recall 2 — remove the newest two waiting requests\n\n"
                "The count must be a positive whole number. Nothing was recalled.",
            )
            return
        try:
            count = int(args[0])
        except ValueError:
            # Python limits extremely long decimal-to-int conversions. A syntactically
            # valid value beyond that limit necessarily exceeds any realizable queue,
            # so capping it to the current queue size preserves `/recall n` semantics.
            queue = getattr(runtime, "queue", None)
            count = max(1, queue.qsize() if queue is not None else 0)

    dropped = await _recall_request_queue(runtime, count)
    runtime.logger.warning(
        "Manual recall requested for agent %s (requested=%s, dropped=%s, active_task_continues=%s)",
        runtime.name,
        count if count is not None else "all",
        dropped,
        bool(getattr(runtime, "current_request_meta", None) or getattr(runtime, "is_generating", False)),
    )

    if dropped:
        scope = f"all {dropped}" if count is None else f"the newest {dropped}"
        await _reply(
            runtime,
            update,
            f"↩️ Recalled {scope} queued request(s).\n"
            "The current active task was not interrupted and will continue.",
        )
        return

    await _reply(
        runtime,
        update,
        "↩️ No queued requests to recall.\n"
        "The current active task was not interrupted and will continue if one is running.",
    )


async def cmd_retry(runtime: Any, update: Any, context: Any) -> None:
    if not _user_is_authorized(runtime, update):
        return
    args = [a.strip().lower() for a in (context.args or []) if a.strip()]
    if args and args[0] in {"response", "resp"}:
        await _reply(
            runtime,
            update,
            "Response replay has moved to /resend. Nothing was retried.",
        )
        return
    if args and (len(args) != 1 or args[0] not in {"prompt", "req", "request"}):
        await _reply(
            runtime,
            update,
            "Usage: /retry\n"
            "/retry stops the current execution, creates a clean session, restores recent "
            "handoff context, and reruns the last prompt.\n"
            "Use /resend to replay the previous output without model work.",
        )
        return

    chat_id = update.effective_chat.id
    prompt_snapshot = runtime_retry.capture_retryable_prompt(
        runtime,
        fallback_chat_id=chat_id,
    )
    if prompt_snapshot is None:
        await _reply(
            runtime,
            update,
            "Nothing to retry — no previous user request was found. No session state was changed.",
        )
        return

    # Save before shutdown/reset so a stuck or failed turn remains retryable even
    # if the recovery sequence itself is interrupted.
    runtime_retry.remember_retryable_prompt(runtime, prompt_snapshot)

    retry_lock = getattr(runtime, "_retry_command_lock", None)
    if retry_lock is None:
        retry_lock = asyncio.Lock()
        runtime._retry_command_lock = retry_lock
    if retry_lock.locked():
        await _reply(runtime, update, "A retry recovery is already being prepared.")
        return

    async with retry_lock:
        active_meta = getattr(runtime, "current_request_meta", None)
        has_active_request = bool(
            getattr(runtime, "is_generating", False)
            and isinstance(active_meta, dict)
            and active_meta.get("request_id")
        )
        if has_active_request:
            mark_user_interrupt(runtime, "user_retry")
        runtime.logger.warning(
            "Recovery retry requested for agent %s "
            "(request_id=%s, source=%s, queue_size=%s)",
            runtime.name,
            prompt_snapshot.request_id or "unknown",
            prompt_snapshot.source,
            runtime.queue.qsize(),
        )

        await _shutdown_active_backend(runtime)
        if has_active_request:
            await _notify_interrupted(
                runtime,
                reason="user_retry",
                error="/retry reset a stuck or stale model context",
                summary="Recovery retry",
            )
        dropped = await _clear_request_queue(runtime)

        try:
            reset_mode = await runtime_session.reset_for_retry(runtime)
        except Exception as exc:
            runtime.logger.exception("Could not reset context for /retry: %s", exc)
            await _reply(
                runtime,
                update,
                f"Retry recovery stopped because the clean session could not be created: {exc}",
            )
            return

        handoff = runtime_retry.build_retry_handoff(runtime)
        handoff_queued = False
        if handoff is not None:
            try:
                arm_primer = getattr(runtime, "_arm_session_primer", None)
                if callable(arm_primer):
                    arm_primer(
                        "This is an automatic /retry recovery. Restore only the recent bridge "
                        "history needed for continuity, then process the retried user request."
                    )
                enqueue_bootstrap = getattr(runtime, "enqueue_startup_bootstrap", None)
                backend_manager = getattr(runtime, "backend_manager", None)
                backend = (
                    getattr(backend_manager, "current_backend", None)
                    if backend_manager is not None
                    else getattr(runtime, "backend", None)
                )
                supports_sessions = bool(
                    getattr(getattr(backend, "capabilities", None), "supports_sessions", False)
                )
                if supports_sessions and callable(enqueue_bootstrap):
                    await enqueue_bootstrap(chat_id)
                handoff_request_id = await runtime.enqueue_request(
                    chat_id,
                    handoff.prompt,
                    runtime_retry.RETRY_HANDOFF_SOURCE,
                    f"Retry handoff restore [{handoff.exchange_count} exchanges]",
                    is_retry=True,
                    deliver_to_telegram=False,
                    skip_memory_injection=True,
                )
                handoff_queued = bool(handoff_request_id)
            except Exception as exc:
                runtime.logger.exception(
                    "Could not queue handoff context for /retry: %s",
                    exc,
                )

        try:
            request_id = await runtime.enqueue_request(
                chat_id,
                prompt_snapshot.prompt,
                "retry",
                f"Recovery retry: {prompt_snapshot.summary}",
                is_retry=True,
            )
        except Exception as exc:
            runtime.logger.exception("Could not requeue the prompt for /retry: %s", exc)
            request_id = None
        if not request_id:
            await _reply(
                runtime,
                update,
                "The context was reset, but the previous request could not be queued again.",
            )
            return

        continuity = (
            f"{handoff.exchange_count} recent exchange(s)"
            if handoff_queued and handoff is not None
            else (
                "handoff restore could not be queued"
                if handoff is not None
                else "no recent handoff history"
            )
        )
        await _reply(
            runtime,
            update,
            "↻ Recovery retry started.\n"
            f"Stopped stale execution and cleared {dropped} waiting request(s).\n"
            f"Clean context: /{reset_mode} semantics.\n"
            f"Continuity: {continuity}.\n"
            f"Requeued the last prompt as {request_id}.",
        )


async def cmd_resend(runtime: Any, update: Any, context: Any) -> None:
    if not _user_is_authorized(runtime, update):
        return
    args = [str(arg).strip() for arg in (getattr(context, "args", None) or []) if str(arg).strip()]
    if args:
        await _reply(
            runtime,
            update,
            "Usage: /resend\n"
            "/resend replays the previous model or Bridge output exactly, without model work.",
        )
        return
    snapshot = runtime_retry.capture_resend_output(runtime)
    if snapshot is None:
        await _reply(runtime, update, "Nothing to resend — no previous model or Bridge output was found.")
        return
    await runtime.send_long_message(
        chat_id=update.effective_chat.id,
        text=snapshot.text,
        request_id=snapshot.request_id,
        purpose="resend-output",
    )


async def callback_retry_toggle(runtime: Any, query: Any, value: str) -> None:
    chat_id = query.message.chat_id
    if value == "response":
        snapshot = runtime_retry.capture_resend_output(runtime)
        if snapshot is None:
            await query.answer("Nothing to resend.", show_alert=True)
            return
        await query.answer("Resending previous output...")
        await runtime.send_long_message(
            chat_id=chat_id,
            text=snapshot.text,
            request_id=snapshot.request_id,
            purpose="resend-output",
        )
        return

    if value not in {"prompt", "req", "request"}:
        await query.answer("This old retry menu is no longer supported.", show_alert=True)
        return
    await query.answer("Starting recovery retry...")
    update = SimpleNamespace(
        effective_user=query.from_user,
        effective_chat=SimpleNamespace(id=chat_id),
        effective_message=query.message,
        message=query.message,
    )
    await cmd_retry(runtime, update, SimpleNamespace(args=[]))
