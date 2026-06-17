from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from typing import Any
import hashlib as _hashlib

from telegram.error import RetryAfter

from orchestrator.runtime_common import _print_final_response, _safe_excerpt
from orchestrator import telegram_delivery_failover
from orchestrator.telegram_notifications import disable_notification

EMPTY_SUCCESS_TOOL_FAILURE_MESSAGE = (
    "I wasn't able to complete that — a tool I tried to use didn't return a result. "
    "Please check that all required API keys (e.g. brave_api_key for web search) are configured in secrets.json."
)


@dataclass(frozen=True)
class QueueItemStart:
    is_bridge_request: bool
    queued_at: datetime
    queue_wait_s: float


@dataclass(frozen=True)
class TurnPrompt:
    effective_prompt: str
    final_prompt: str
    extra_sections: list[tuple[str, str]]
    habit_ids: list[str]
    incremental: bool
    prompt_audit: dict[str, Any]


@dataclass(frozen=True)
class BackendGeneration:
    response: Any | None
    detached: bool
    backend_started: datetime
    detach_after_s: float
    generation_task: asyncio.Task | None = None


@dataclass(frozen=True)
class SuccessfulResponse:
    display_text: str
    visible_text: str
    wrapper_result: Any


@dataclass
class StreamedAnswerState:
    request_id: str
    chat_id: int
    placeholder: Any | None
    buffer: list[str]
    started_at: datetime
    delta_count: int = 0
    char_count: int = 0
    edit_count: int = 0
    failed: bool = False
    failure_reason: str = ""
    final_promoted: bool = False

    @property
    def has_text(self) -> bool:
        return self.delta_count > 0 and bool("".join(self.buffer))


@dataclass(frozen=True)
class StreamFinalization:
    streamed: bool
    final_delivered: bool
    continuation_chunks_sent: int = 0
    fallback_required: bool = False
    error: str = ""


@dataclass(frozen=True)
class InteractiveFeedback:
    stop_typing: asyncio.Event | None
    typing_task: asyncio.Task | None
    escalation_task: asyncio.Task | None
    answer_preview_task: asyncio.Task | None
    answer_stream_state: StreamedAnswerState | None
    placeholder: Any | None
    stream_callback: Any | None
    think_flush_task: asyncio.Task | None
    on_stream_event: Any | None


def begin_queue_item(runtime, item) -> QueueItemStart:
    if not item.silent:
        runtime.last_prompt = item
    is_bridge_request = item.source.startswith("bridge:") or item.source.startswith("bridge-transfer:")
    queued_at = datetime.fromisoformat(item.created_at)
    queue_wait_s = (datetime.now() - queued_at).total_seconds()
    runtime.logger.info(
        f"Processing {item.request_id} via {runtime.config.active_backend} "
        f"(source={item.source}, silent={item.silent}, prompt_len={len(item.prompt)}, "
        f"queue_wait_s={queue_wait_s:.2f})"
    )
    runtime.current_request_meta = {
        "request_id": item.request_id,
        "chat_id": item.chat_id,
        "prompt": item.prompt,
        "source": item.source,
        "summary": item.summary,
        "started_at": datetime.now().isoformat(),
    }
    runtime._mark_activity()
    runtime._log_maintenance(
        item,
        "processing",
        engine=runtime.config.active_backend,
        silent=item.silent,
        prompt_len=len(item.prompt),
        queue_wait_s=f"{queue_wait_s:.2f}",
    )
    runtime.is_generating = True
    return QueueItemStart(
        is_bridge_request=is_bridge_request,
        queued_at=queued_at,
        queue_wait_s=queue_wait_s,
    )


async def build_turn_prompt(runtime, item, *, is_bridge_request: bool) -> TurnPrompt:
    effective_prompt = runtime._consume_session_primer(item)
    habit_sections, habit_ids = runtime._build_habit_sections(item, effective_prompt)
    extra_sections = runtime._workzone_prompt_section() + habit_sections
    extra_sections += await runtime._build_pre_turn_context_sections(
        item,
        effective_prompt,
        is_bridge_request=is_bridge_request,
    )
    runtime.current_request_meta["habit_ids"] = habit_ids
    incremental = (
        runtime.backend_manager.agent_mode == "fixed"
        and hasattr(runtime.backend_manager.current_backend, "_session_id")
        and runtime.backend_manager.current_backend._session_id is not None
    )
    prompt_payload = runtime.context_assembler.build_prompt_payload(
        effective_prompt,
        runtime.config.active_backend,
        extra_sections=extra_sections,
        inject_memory=not item.skip_memory_injection,
        incremental=incremental,
    )
    final_prompt = prompt_payload["final_prompt"]
    prompt_audit = prompt_payload.get("audit", {})
    runtime._last_prompt_audit = prompt_audit
    runtime._thinking_chars_this_req = 0
    runtime._last_full_prompt_tokens = len(final_prompt) // 4
    return TurnPrompt(
        effective_prompt=effective_prompt,
        final_prompt=final_prompt,
        extra_sections=extra_sections,
        habit_ids=habit_ids,
        incremental=incremental,
        prompt_audit=prompt_audit,
    )


async def run_backend_generation(
    runtime,
    item,
    final_prompt: str,
    *,
    on_stream_event,
    audit_active: bool,
) -> BackendGeneration:
    extra = runtime.config.extra or {}
    background_mode = (
        extra.get("background_mode", False)
        and not item.silent
        and item.deliver_to_telegram
    )
    detach_after_s = float(
        extra.get("background_detach_after")
        or (extra.get("escalation_thresholds") or [30, 60, 90, 150])[-1]
    )

    backend_started = datetime.now()
    current_backend = getattr(runtime.backend_manager, "current_backend", None)
    if runtime.config.active_backend == "openrouter-api" and hasattr(current_backend, "set_reasoning_enabled"):
        current_backend.set_reasoning_enabled(runtime._think or audit_active)

    if background_mode:
        generation_task = asyncio.create_task(
            runtime.backend_manager.generate_response(
                final_prompt,
                item.request_id,
                is_retry=item.is_retry,
                silent=item.silent,
                on_stream_event=on_stream_event,
            )
        )
        try:
            response = await asyncio.wait_for(
                asyncio.shield(generation_task),
                timeout=detach_after_s,
            )
            detached = False
        except asyncio.TimeoutError:
            response = None
            detached = True
        except asyncio.CancelledError:
            generation_task.cancel()
            try:
                await generation_task
            except asyncio.CancelledError:
                pass
            raise
        finally:
            runtime.is_generating = False
        return BackendGeneration(
            response=response,
            detached=detached,
            backend_started=backend_started,
            detach_after_s=detach_after_s,
            generation_task=generation_task,
        )

    try:
        response = await runtime.backend_manager.generate_response(
            final_prompt,
            item.request_id,
            is_retry=item.is_retry,
            silent=item.silent,
            on_stream_event=on_stream_event,
        )
    finally:
        runtime.is_generating = False
    return BackendGeneration(
        response=response,
        detached=False,
        backend_started=backend_started,
        detach_after_s=detach_after_s,
    )


def log_backend_finished(
    runtime,
    item,
    response,
    *,
    backend_elapsed_s: float,
    final_prompt: str,
) -> None:
    runtime.logger.info(
        f"Backend finished {item.request_id} via {runtime.config.active_backend} "
        f"(success={response.is_success}, elapsed_s={backend_elapsed_s:.2f}, "
        f"text_len={len(response.text or '')}, error_len={len(response.error or '')}, "
        f"final_prompt_len={len(final_prompt)})"
    )
    runtime._log_maintenance(
        item,
        "backend_finished",
        engine=runtime.config.active_backend,
        success=response.is_success,
        elapsed_s=f"{backend_elapsed_s:.2f}",
        text_len=len(response.text or ""),
        error_len=len(response.error or ""),
        final_prompt_len=len(final_prompt),
        result_excerpt=_safe_excerpt(response.text or response.error or "", 200),
    )


async def cleanup_interactive_feedback(
    runtime,
    item,
    *,
    stop_typing,
    typing_task,
    escalation_task,
    answer_preview_task=None,
    think_flush_task,
    placeholder,
    delete_placeholder: bool = True,
) -> None:
    if stop_typing and typing_task:
        stop_typing.set()
        await typing_task
        if escalation_task is not None:
            try:
                await escalation_task
            except asyncio.CancelledError:
                pass
        if answer_preview_task is not None:
            try:
                await answer_preview_task
            except asyncio.CancelledError:
                pass

    if think_flush_task is not None:
        think_flush_task.cancel()
        try:
            await think_flush_task
        except asyncio.CancelledError:
            pass
        await runtime._flush_thinking(item.chat_id)

    if placeholder and delete_placeholder:
        try:
            delete_started = datetime.now()
            await runtime.app.bot.delete_message(chat_id=item.chat_id, message_id=placeholder.message_id)
            delete_elapsed_s = (datetime.now() - delete_started).total_seconds()
            runtime.telegram_logger.info(
                f"Deleted placeholder for {item.request_id} "
                f"(elapsed_s={delete_elapsed_s:.2f})"
            )
        except Exception:
            pass


async def answer_preview_loop(
    runtime,
    item,
    *,
    placeholder,
    stop_event: asyncio.Event,
    event_queue: asyncio.Queue,
    stream_state: StreamedAnswerState | None = None,
) -> None:
    """Edit the Telegram placeholder with assistant text deltas while generating."""
    if placeholder is None:
        return

    from adapters.stream_events import (
        KIND_ERROR,
        KIND_FILE_EDIT,
        KIND_PROGRESS,
        KIND_SHELL_EXEC,
        KIND_TEXT_DELTA,
        KIND_THINKING,
        KIND_TOOL_END,
        KIND_TOOL_START,
    )

    extra = getattr(getattr(runtime, "config", None), "extra", {}) or {}
    min_edit_interval = float(extra.get("answer_stream_edit_interval_s", 1.0))
    min_chars = int(extra.get("answer_stream_min_chars", 24))
    max_chars = int(extra.get("answer_stream_max_chars", 3400))
    started = datetime.now()
    last_edit_at = 0.0
    chunks: list[str] = []
    latest_status = "Still working..."
    dirty = False
    preview_disabled = False
    status_kinds = {
        KIND_ERROR,
        KIND_FILE_EDIT,
        KIND_PROGRESS,
        KIND_SHELL_EXEC,
        KIND_THINKING,
        KIND_TOOL_END,
        KIND_TOOL_START,
    }

    def _preview_text() -> str:
        text = "".join(chunks).strip()
        if len(text) > max_chars:
            text = "...\n" + text[-max_chars:]
        elapsed = int((datetime.now() - started).total_seconds())
        header = f"✍️ {runtime.name} is replying... ({elapsed}s)\n\n"
        if text:
            return header + text
        return header + latest_status

    async def _edit() -> None:
        nonlocal dirty, last_edit_at, preview_disabled
        if preview_disabled:
            dirty = False
            return
        if telegram_delivery_failover.is_delivery_blocked(runtime):
            preview_disabled = True
            dirty = False
            return
        text = _preview_text()
        if len(text.strip()) < min_chars:
            return
        try:
            await runtime.app.bot.edit_message_text(
                chat_id=item.chat_id,
                message_id=placeholder.message_id,
                text=text,
            )
            if stream_state is not None:
                stream_state.edit_count += 1
            last_edit_at = asyncio.get_running_loop().time()
            dirty = False
        except Exception as exc:
            retry_after = getattr(exc, "retry_after", None) if isinstance(exc, RetryAfter) else None
            if retry_after is not None or "429" in str(exc) or "RetryAfter" in str(exc):
                preview_disabled = True
                dirty = False
                if stream_state is not None:
                    stream_state.failed = True
                    stream_state.failure_reason = str(exc)
                if isinstance(exc, RetryAfter):
                    await telegram_delivery_failover.handle_retry_after(
                        runtime,
                        exc=exc,
                        chat_id=item.chat_id,
                        request_id=item.request_id,
                        purpose="answer_preview",
                    )
                runtime.telegram_logger.warning(
                    f"Answer stream preview disabled for {item.request_id}: {exc}"
                )
            elif "message is not modified" in str(exc).lower():
                dirty = False
            else:
                if stream_state is not None:
                    stream_state.failed = True
                    stream_state.failure_reason = str(exc)
                runtime.telegram_logger.warning(
                    f"Answer stream preview edit failed for {item.request_id}: {exc}"
                )

    while not stop_event.is_set():
        try:
            event = await asyncio.wait_for(event_queue.get(), timeout=min_edit_interval)
            kind = getattr(event, "kind", None)
            raw_summary = str(getattr(event, "summary", "") or "")
            summary = raw_summary.strip()
            if kind == KIND_TEXT_DELTA and raw_summary:
                chunks.append(raw_summary)
                if stream_state is not None:
                    stream_state.buffer.append(raw_summary)
                    stream_state.delta_count += 1
                    stream_state.char_count += len(raw_summary)
                dirty = True
            elif kind in status_kinds and summary and not chunks:
                latest_status = summary[:240]
                dirty = True
        except asyncio.TimeoutError:
            dirty = True

        now = asyncio.get_running_loop().time()
        if dirty and (now - last_edit_at) >= min_edit_interval:
            await _edit()

    if dirty:
        await _edit()


async def setup_interactive_feedback(
    runtime,
    item,
    *,
    audit_active: bool,
    audit_collector,
) -> InteractiveFeedback:
    stop_typing = None
    typing_task = None
    escalation_task = None
    placeholder = None
    stream_callback = None
    think_flush_task = None
    answer_preview_task = None
    answer_stream_state = None
    if not item.silent and item.deliver_to_telegram:
        placeholder_text, placeholder_parse_mode = runtime.get_typing_placeholder()
        stop_typing = asyncio.Event()
        typing_task = asyncio.create_task(runtime.typing_loop(item.chat_id, stop_typing))
        try:
            if await telegram_delivery_failover.handle_blocked_send(
                runtime,
                chat_id=item.chat_id,
                request_id=item.request_id,
                purpose="placeholder",
            ):
                runtime.telegram_logger.warning(
                    f"Skipping placeholder for {item.request_id} — delivery blocked"
                )
            else:
                placeholder_started = datetime.now()
                placeholder = await runtime.app.bot.send_message(
                    chat_id=item.chat_id,
                    text=placeholder_text,
                    parse_mode=placeholder_parse_mode,
                    disable_notification=disable_notification(runtime),
                )
                placeholder_elapsed_s = (datetime.now() - placeholder_started).total_seconds()
                runtime.telegram_logger.info(
                    f"Sent placeholder for {item.request_id} "
                    f"(elapsed_s={placeholder_elapsed_s:.2f})"
                )
        except RetryAfter as e:
            await telegram_delivery_failover.handle_retry_after(
                runtime,
                exc=e,
                chat_id=item.chat_id,
                request_id=item.request_id,
                purpose="placeholder",
            )
            runtime.telegram_logger.warning(f"Failed to send placeholder due to flood control: {e}")
        except Exception as e:
            runtime.telegram_logger.warning(f"Failed to send placeholder: {e}")

        backend = runtime.backend_manager.current_backend
        capabilities = getattr(backend, "capabilities", None)
        supports_stream_display = bool(getattr(capabilities, "supports_thinking_stream", False))
        stream_queue = None
        answer_preview_queue = None
        preview_enabled = telegram_delivery_failover.effective_preview_enabled(runtime)
        preview_enabled = preview_enabled and placeholder is not None
        final_stream_enabled = bool((getattr(runtime.config, "extra", {}) or {}).get("answer_stream_final_delivery", False))
        supports_answer_stream = bool(getattr(capabilities, "supports_answer_stream", False))
        if final_stream_enabled and supports_answer_stream and placeholder is not None:
            answer_stream_state = StreamedAnswerState(
                request_id=item.request_id,
                chat_id=item.chat_id,
                placeholder=placeholder,
                buffer=[],
                started_at=datetime.now(),
            )
        runtime.logger.info(
            f"Answer stream eligibility {item.request_id}: "
            f"preview={preview_enabled}, final={answer_stream_state is not None}, "
            f"backend={getattr(runtime.config, 'active_backend', 'unknown')}, "
            f"supports_answer_stream={supports_answer_stream}"
        )
        use_stream = runtime._verbose or runtime._think or audit_active or preview_enabled
        if use_stream:
            if runtime._verbose and supports_stream_display and not preview_enabled:
                stream_queue = asyncio.Queue(maxsize=200)
            if preview_enabled:
                answer_preview_queue = asyncio.Queue(maxsize=300)
            base_stream_callback = runtime._make_stream_callback(
                event_queue=stream_queue,
                think_buffer=runtime._think_buffer if runtime._think else None,
                audit_collector=audit_collector,
            )

            if preview_enabled:
                async def stream_callback(event):
                    if answer_preview_queue is not None:
                        try:
                            answer_preview_queue.put_nowait(event)
                        except asyncio.QueueFull:
                            pass
                    await base_stream_callback(event)
            else:
                stream_callback = base_stream_callback

        if preview_enabled and answer_preview_queue is not None:
            answer_preview_task = asyncio.create_task(
                answer_preview_loop(
                    runtime,
                    item,
                    placeholder=placeholder,
                    stop_event=stop_typing,
                    event_queue=answer_preview_queue,
                    stream_state=answer_stream_state,
                )
            )
        elif runtime._verbose and supports_stream_display:
            escalation_task = asyncio.create_task(
                runtime._streaming_display_loop(
                    item.chat_id,
                    placeholder,
                    item.request_id,
                    stop_typing,
                    stream_queue,
                    backend=backend,
                )
            )
        else:
            escalation_task = asyncio.create_task(
                runtime._escalating_placeholder_loop(
                    item.chat_id,
                    placeholder,
                    item.request_id,
                    stop_typing,
                    backend=backend,
                )
            )
        if runtime._think:
            runtime._think_buffer.clear()
            runtime._openrouter_think_chunk = ""
            runtime._last_openrouter_think_snippet = None
            think_flush_task = asyncio.create_task(
                runtime._thinking_flush_loop(item.chat_id, stop_typing)
            )

    if stream_callback is None and audit_active:
        stream_callback = runtime._make_stream_callback(audit_collector=audit_collector)

    on_stream_event = stream_callback if (not item.silent or audit_active) else None
    return InteractiveFeedback(
        stop_typing=stop_typing,
        typing_task=typing_task,
        escalation_task=escalation_task,
        answer_preview_task=answer_preview_task,
        answer_stream_state=answer_stream_state,
        placeholder=placeholder,
        stream_callback=stream_callback,
        think_flush_task=think_flush_task,
        on_stream_event=on_stream_event,
    )


async def finalize_streamed_answer(
    runtime,
    item,
    *,
    stream_state: StreamedAnswerState | None,
    final_text: str,
) -> StreamFinalization:
    if stream_state is None:
        return StreamFinalization(streamed=False, final_delivered=False, fallback_required=True)

    if not stream_state.has_text or stream_state.failed or stream_state.placeholder is None:
        if stream_state.placeholder is not None:
            try:
                await runtime.app.bot.delete_message(
                    chat_id=item.chat_id,
                    message_id=stream_state.placeholder.message_id,
                )
            except Exception:
                pass
        runtime.logger.info(
            f"Answer stream finalize fallback {item.request_id}: "
            f"deltas={stream_state.delta_count}, edits={stream_state.edit_count}, "
            f"failed={stream_state.failed}, reason={stream_state.failure_reason}"
        )
        return StreamFinalization(
            streamed=stream_state.has_text,
            final_delivered=False,
            fallback_required=True,
            error=stream_state.failure_reason,
        )

    if await telegram_delivery_failover.handle_blocked_send(
        runtime,
        chat_id=item.chat_id,
        request_id=item.request_id,
        purpose="response",
        text=final_text,
    ):
        runtime.telegram_logger.warning(
            f"Answer stream final promotion skipped for {item.request_id} — delivery blocked"
        )
        return StreamFinalization(
            streamed=True,
            final_delivered=False,
            fallback_required=False,
            error="delivery blocked",
        )

    extra = getattr(getattr(runtime, "config", None), "extra", {}) or {}
    max_chars = int(extra.get("answer_stream_max_chars", 3400))
    first_chunk = (final_text or "")[:max_chars]
    continuation = (final_text or "")[max_chars:]
    try:
        await runtime.app.bot.edit_message_text(
            chat_id=item.chat_id,
            message_id=stream_state.placeholder.message_id,
            text=first_chunk,
        )
        stream_state.final_promoted = True
        continuation_chunks = 0
        if continuation:
            _elapsed, continuation_chunks = await runtime.send_long_message(
                chat_id=item.chat_id,
                text=continuation,
                request_id=item.request_id,
                purpose="response_continuation",
            )
        runtime.logger.info(
            f"Answer stream finalized {item.request_id}: promoted=True, "
            f"deltas={stream_state.delta_count}, edits={stream_state.edit_count}, "
            f"chars={stream_state.char_count}, continuation_chunks={continuation_chunks}"
        )
        return StreamFinalization(
            streamed=True,
            final_delivered=True,
            continuation_chunks_sent=continuation_chunks,
        )
    except Exception as exc:
        stream_state.failed = True
        stream_state.failure_reason = str(exc)
        if isinstance(exc, RetryAfter):
            await telegram_delivery_failover.handle_retry_after(
                runtime,
                exc=exc,
                chat_id=item.chat_id,
                request_id=item.request_id,
                purpose="response",
                text=final_text,
            )
        runtime.telegram_logger.warning(
            f"Answer stream final promotion failed for {item.request_id}: {exc}"
        )
        return StreamFinalization(
            streamed=True,
            final_delivered=False,
            fallback_required=not isinstance(exc, RetryAfter),
            error=str(exc),
        )


async def handle_empty_success_response(runtime, item) -> None:
    err_msg = EMPTY_SUCCESS_TOOL_FAILURE_MESSAGE
    runtime.logger.warning(
        f"Backend {runtime.config.active_backend} returned success with empty text for "
        f"{item.request_id} — treating as recoverable tool failure"
    )
    runtime._mark_error(err_msg)
    runtime._record_habit_outcome(item, success=False, error_text=err_msg)
    if runtime._should_buffer_during_transfer(item.request_id):
        runtime._record_suppressed_transfer_result(item, success=False, error=err_msg)
    if not item.silent and not runtime._should_buffer_during_transfer(item.request_id):
        await runtime.send_long_message(
            chat_id=item.chat_id,
            text=err_msg,
            request_id=item.request_id,
            purpose="error",
        )
    await runtime._notify_request_listeners(
        item.request_id,
        {
            "request_id": item.request_id,
            "success": False,
            "text": None,
            "error": err_msg,
            "source": item.source,
            "summary": item.summary,
        },
    )


async def prepare_successful_response(runtime, item, response, *, completion_path: str) -> SuccessfulResponse:
    if item.source == "bridge:hchat-draft" and hasattr(runtime, "_prepare_hchat_draft_success"):
        return await runtime._prepare_hchat_draft_success(
            item,
            core_raw=response.text,
            completion_path=completion_path,
        )
    display_text = runtime._strip_transfer_accept_prefix(item, response.text)
    runtime._mark_success()
    runtime._record_habit_outcome(item, success=True, response_text=response.text)
    visible_text, wrapper_result = await runtime._apply_wrapper_to_visible_text(
        item,
        display_text or response.text,
    )
    runtime._append_core_transcript(
        item,
        core_raw=response.text,
        visible_text=visible_text,
        completion_path=completion_path,
        wrapper_result=wrapper_result,
    )
    await runtime._notify_request_listeners(
        item.request_id,
        {
            "request_id": item.request_id,
            "success": True,
            "text": visible_text,
            "error": None,
            "source": item.source,
            "summary": item.summary,
            **runtime._wrapper_listener_fields(response.text, visible_text, wrapper_result),
        },
    )
    return SuccessfulResponse(
        display_text=display_text,
        visible_text=visible_text,
        wrapper_result=wrapper_result,
    )


def record_foreground_usage_audit(
    runtime,
    item,
    response,
    *,
    visible_text: str,
    wrapper_result,
    final_prompt: str,
    effective_prompt: str,
    incremental: bool,
) -> None:
    try:
        from tools.token_tracker import estimate_tokens, record_audit_event, record_usage

        if response.usage:
            input_tokens = response.usage.input_tokens
            output_tokens = response.usage.output_tokens
            thinking_tokens = response.usage.thinking_tokens
            token_source = "api"
            record_usage(
                runtime.workspace_dir,
                model=runtime.get_current_model(),
                backend=runtime.config.active_backend,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                thinking_tokens=thinking_tokens,
                session_id=runtime.session_id_dt,
                cost_usd=getattr(response, "cost_usd", None),
            )
        else:
            input_tokens = estimate_tokens(final_prompt)
            output_tokens = estimate_tokens(visible_text)
            thinking_tokens = runtime._thinking_chars_this_req // 4
            token_source = "estimated"
            record_usage(
                runtime.workspace_dir,
                model=runtime.get_current_model(),
                backend=runtime.config.active_backend,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                thinking_tokens=thinking_tokens,
                session_id=runtime.session_id_dt,
            )
        prompt_audit = runtime._last_prompt_audit
        section_chars = {s["key"]: s["chars"] for s in prompt_audit.get("sections", [])}
        section_tokens = {
            s["key"]: s.get("tokens_est") or max(1, s["chars"] // 4)
            for s in prompt_audit.get("sections", [])
        }
        section_counts = {s["key"]: s.get("item_count", 0) for s in prompt_audit.get("sections", [])}
        stream_metadata = getattr(response, "stream_metadata", None) or {}
        claw_thinking = stream_metadata.get("claw_thinking") or {}
        record_audit_event(
            runtime.workspace_dir,
            {
                "request_id": item.request_id,
                "agent": runtime.name,
                "runtime": "flex",
                "completion_path": "foreground",
                "backend": runtime.config.active_backend,
                "model": runtime.get_current_model(),
                "source": item.source,
                "summary": item.summary,
                "silent": item.silent,
                "is_retry": item.is_retry,
                "success": response.is_success,
                "incremental_mode": incremental,
                "token_source": token_source,
                "raw_prompt_chars": len(item.prompt),
                "effective_prompt_chars": len(effective_prompt),
                "final_prompt_chars": len(final_prompt),
                "response_chars": len(visible_text or ""),
                "core_raw_chars": len(response.text or ""),
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "thinking_tokens": thinking_tokens,
                "thinking_chars": int(claw_thinking.get("thinking_chars") or 0),
                "thinking_event_count": int(claw_thinking.get("thinking_event_count") or 0),
                "thinking_redacted_count": int(claw_thinking.get("thinking_redacted_count") or 0),
                "thinking_sources": list(claw_thinking.get("thinking_sources") or []),
                "tool_call_count": int(getattr(response, "tool_call_count", 0) or 0),
                "tool_loop_count": int(getattr(response, "tool_loop_count", 0) or 0),
                "tool_catalog_count": 0,
                "tool_schema_chars": 0,
                "tool_schema_tokens_est": 0,
                "tool_schema_fingerprint": "",
                "tool_max_loops": 0,
                "budget_applied": bool(prompt_audit.get("budget_applied")),
                "budget_limit_chars": prompt_audit.get("budget_limit_chars"),
                "context_chars_before_budget": prompt_audit.get("context_chars_before_budget", 0),
                "time_fyi_chars": prompt_audit.get("time_fyi_chars", 0),
                "context_expansion_ratio": round(len(final_prompt) / max(len(item.prompt), 1), 3),
                "context_fingerprint": prompt_audit.get("context_fingerprint", ""),
                "request_fingerprint": _hashlib.sha1((item.prompt or "").encode("utf-8")).hexdigest()[:16],
                "section_chars": section_chars,
                "section_tokens_est": section_tokens,
                "section_counts": section_counts,
                **runtime._wrapper_audit_fields(wrapper_result),
            },
        )
    except Exception:
        pass


def persist_success_memory(
    runtime,
    item,
    response,
    *,
    visible_text: str,
    wrapper_result,
    is_bridge_request: bool,
    session_reset_source: str,
) -> None:
    memory_user_text = item.prompt
    if item.source.lower() in {"document", "photo", "voice", "audio", "video", "sticker"}:
        memory_user_text = f"[{item.source}] {item.summary}"
    if item.source not in {"startup", "system", session_reset_source} and not is_bridge_request:
        memory_assistant_text = runtime._core_memory_assistant_text(
            response.text,
            visible_text,
            wrapper_result,
        )
        runtime.memory_store.record_turn("user", item.source, memory_user_text)
        runtime.memory_store.record_turn("assistant", runtime.config.active_backend, memory_assistant_text)
        runtime.memory_store.record_exchange(memory_user_text, memory_assistant_text, item.source)
        runtime._schedule_post_turn_observers(
            item,
            memory_user_text,
            memory_assistant_text,
            is_bridge_request=is_bridge_request,
        )
    if not is_bridge_request:
        runtime.handoff_builder.append_transcript("user", item.prompt, item.source)
        runtime.handoff_builder.append_transcript("assistant", visible_text)
        runtime.handoff_builder.refresh_recent_context()
        runtime.project_chat_logger.log_exchange(item.prompt, visible_text, item.source)


async def handle_backend_error(
    runtime,
    item,
    response,
    *,
    queued_at: datetime,
    queue_wait_s: float,
    backend_elapsed_s: float,
) -> None:
    err_msg = response.error or "Unknown error"
    runtime._mark_error(err_msg)
    runtime._record_habit_outcome(item, success=False, error_text=err_msg)
    if runtime._should_buffer_during_transfer(item.request_id):
        runtime._record_suppressed_transfer_result(item, success=False, error=err_msg)
    await runtime._notify_request_listeners(
        item.request_id,
        {
            "request_id": item.request_id,
            "success": False,
            "text": None,
            "error": err_msg,
            "source": item.source,
            "summary": item.summary,
        },
    )
    if item.silent:
        return
    runtime.error_logger.error(
        f"Flex Backend error for {item.request_id} "
        f"({runtime.config.active_backend}, source={item.source}): {err_msg}"
    )
    if runtime._should_retry_codex_scheduler_failure(item, err_msg):
        runtime._schedule_codex_scheduler_retry(item)
    if not item.deliver_to_telegram:
        return
    if runtime._should_buffer_during_transfer(item.request_id):
        return
    send_elapsed_s, chunk_count = await runtime.send_long_message(
        chat_id=item.chat_id,
        text=f"Flex Backend Error ({runtime.config.active_backend}): {err_msg}",
        request_id=item.request_id,
        purpose="error",
    )
    total_elapsed_s = (datetime.now() - queued_at).total_seconds()
    runtime.logger.info(
        f"Completed {item.request_id} error delivery via {runtime.config.active_backend} "
        f"(queue_wait_s={queue_wait_s:.2f}, backend_s={backend_elapsed_s:.2f}, "
        f"telegram_send_s={send_elapsed_s:.2f}, total_s={total_elapsed_s:.2f}, "
        f"chunks={chunk_count})"
    )
    runtime._log_maintenance(item, "send_error", error_excerpt=_safe_excerpt(err_msg, 200))


async def handle_success_delivery(
    runtime,
    item,
    response,
    *,
    visible_text: str,
    wrapper_result,
    is_bridge_request: bool,
    session_reset_source: str,
    queued_at: datetime,
    queue_wait_s: float,
    backend_elapsed_s: float,
    audit_collector,
    answer_stream_state: StreamedAnswerState | None = None,
) -> None:
    if runtime._should_buffer_during_transfer(item.request_id):
        if answer_stream_state is not None and answer_stream_state.placeholder is not None:
            try:
                await runtime.app.bot.delete_message(
                    chat_id=item.chat_id,
                    message_id=answer_stream_state.placeholder.message_id,
                )
            except Exception:
                pass
        runtime._record_suppressed_transfer_result(item, success=True, text=visible_text)
        return
    runtime.last_response = {
        "chat_id": item.chat_id,
        "text": visible_text,
        "request_id": item.request_id,
        "responded_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    persist_success_memory(
        runtime,
        item,
        response,
        visible_text=visible_text,
        wrapper_result=wrapper_result,
        is_bridge_request=is_bridge_request,
        session_reset_source=session_reset_source,
    )
    if not item.deliver_to_telegram:
        return

    response_text = visible_text
    cos_handled = False
    await runtime._send_wrapper_verbose_trace(item, response.text, visible_text, wrapper_result)
    if (
        runtime._cos_enabled
        and runtime.name != "lily"
        and not item.source.startswith("cos-query:")
        and response_text
        and response_text.rstrip().endswith(("?", "？"))
    ):
        cos_result = await runtime.cos_query(response_text)
        if cos_result.get("answered") and cos_result.get("response"):
            response_text = cos_result["response"]
        else:
            cos_handled = True
    _print_final_response(runtime.name, response_text)
    stream_finalization = await finalize_streamed_answer(
        runtime,
        item,
        stream_state=answer_stream_state,
        final_text=response_text,
    )
    if stream_finalization.final_delivered:
        send_elapsed_s = 0.0
        chunk_count = 1 + stream_finalization.continuation_chunks_sent
    elif stream_finalization.fallback_required:
        send_elapsed_s, chunk_count = await runtime.send_long_message(
            chat_id=item.chat_id,
            text=response_text,
            request_id=item.request_id,
            purpose="response",
        )
    else:
        send_elapsed_s, chunk_count = 0.0, 0
    await runtime._send_voice_reply(item.chat_id, response_text, item.request_id)
    runtime._schedule_audit_followup(
        item,
        core_raw=response.text,
        visible_text=visible_text,
        response=response,
        audit_collector=audit_collector,
        completion_path="foreground",
    )
    total_elapsed_s = (datetime.now() - queued_at).total_seconds()
    runtime.logger.info(
        f"Completed {item.request_id} delivery via {runtime.config.active_backend} "
        f"(queue_wait_s={queue_wait_s:.2f}, backend_s={backend_elapsed_s:.2f}, "
        f"telegram_send_s={send_elapsed_s:.2f}, total_s={total_elapsed_s:.2f}, "
        f"chunks={chunk_count})"
    )
    runtime._log_maintenance(item, "send_success", text_len=len(response_text or ""))
    if not cos_handled:
        await runtime._hchat_route_reply(item, response_text)
