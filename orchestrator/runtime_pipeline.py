from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from typing import Any
import hashlib as _hashlib

from orchestrator.runtime_common import _safe_excerpt


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
    think_flush_task,
    placeholder,
) -> None:
    if stop_typing and typing_task:
        stop_typing.set()
        await typing_task
        if escalation_task is not None:
            try:
                await escalation_task
            except asyncio.CancelledError:
                pass

    if think_flush_task is not None:
        think_flush_task.cancel()
        try:
            await think_flush_task
        except asyncio.CancelledError:
            pass
        await runtime._flush_thinking(item.chat_id)

    if placeholder:
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


async def prepare_successful_response(runtime, item, response, *, completion_path: str) -> SuccessfulResponse:
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
