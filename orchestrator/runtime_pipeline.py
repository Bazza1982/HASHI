from __future__ import annotations

import asyncio
import hashlib as _hashlib
import inspect
import json
import os
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from telegram.error import RetryAfter

from orchestrator import (
    runtime_cross_session,
    runtime_delivery_order,
    runtime_retry,
    runtime_turn_context,
    telegram_delivery_failover,
    telegram_stream_policy,
)
from orchestrator.memory_plus_mode import (
    extract_memory_plus_update_details,
    is_memory_plus_enabled,
)
from orchestrator.runtime_common import _md_to_html, _print_final_response, _safe_excerpt
from orchestrator.telegram_notifications import disable_notification

EMPTY_SUCCESS_TOOL_FAILURE_MESSAGE = (
    "I wasn't able to complete that — a tool I tried to use didn't return a result. "
    "Please check that all required API keys (e.g. brave_api_key for web search) are configured in secrets.json."
)

HER_SESSION_SCOPE_PERSISTENT = "persistent"
HER_SESSION_SCOPE_ISOLATED = "isolated_per_run"
HER_SESSION_SCOPE_ISOLATED_RESUME = "isolated_resume"

_DANGLING_TOOL_MARKERS = (
    "<｜dsml｜tool_calls",
    "<｜｜dsml｜｜tool_calls",
    "<｜dsml｜invoke",
    "<｜｜dsml｜｜invoke",
    "<|dsml|tool_calls",
    "<||dsml||tool_calls",
    "<|dsml|invoke",
    "<||dsml||invoke",
    "<tool_call>",
)


def _contains_dangling_tool_markup(text: Any) -> bool:
    normalized = str(text or "").lower()
    visible = "\n".join(normalized.split("```")[::2])
    return any(marker in visible for marker in _DANGLING_TOOL_MARKERS)


def _safe_blocked_tool_markup_final(item) -> str:
    prompt = str(getattr(item, "prompt", "") or "")
    if any("\u4e00" <= char <= "\u9fff" for char in prompt):
        return (
            "本轮回复包含未完成的工具控制标记，HASHI 已阻止其发送。"
            "相关操作不视为已执行或已完成；任务状态为未完成，请从已保存的任务检查点继续。"
        )
    return (
        "HASHI blocked an unfinished tool-control envelope from the final response. "
        "No related action is considered executed or complete; the task remains incomplete "
        "and should resume from its preserved checkpoint."
    )


def queued_elapsed_s(item) -> float:
    """Return request age from a process-local clock when available."""

    queued_monotonic = getattr(item, "queued_monotonic", None)
    if queued_monotonic is not None:
        return max(0.0, time.monotonic() - float(queued_monotonic))
    return max(
        0.0,
        (datetime.now() - datetime.fromisoformat(item.created_at)).total_seconds(),
    )


def _append_her_message_audit(
    runtime,
    item,
    event,
    *,
    status: str,
    include_text: bool = False,
    reason: str = "",
    error_type: str = "",
) -> None:
    """Persist one low-volume, user-visible HER message audit record."""

    from adapters.stream_events import DELIVERY_CONTROL, DELIVERY_USER_COMMENTARY

    delivery_class = str(getattr(event, "delivery_class", "") or "")
    if delivery_class == DELIVERY_CONTROL and not bool(
        getattr(event, "required", False)
    ):
        return
    if delivery_class not in {DELIVERY_CONTROL, DELIVERY_USER_COMMENTARY}:
        return
    text = str(getattr(event, "summary", "") or "").strip()
    detail = str(getattr(event, "detail", "") or "").strip()
    if not text:
        return
    text_sha256 = _sha256_text(text)
    record = {
        "format": "her-message-audit-v1",
        "recorded_at": time.time(),
        "agent": str(getattr(runtime, "name", "") or ""),
        "request_id": str(getattr(item, "request_id", "") or ""),
        "event_id": str(getattr(event, "event_id", "") or ""),
        "kind": str(getattr(event, "kind", "") or ""),
        "delivery_class": delivery_class,
        "origin": str(getattr(event, "origin", "") or ""),
        "phase": str(getattr(event, "phase", "") or ""),
        "revision": getattr(event, "revision", None),
        "required": bool(getattr(event, "required", False)),
        "provenance": str(getattr(event, "provenance", "") or ""),
        "status": str(status or "unknown"),
        "text_sha256": text_sha256,
        **({"text": text} if include_text else {}),
        **({"detail": detail} if include_text and detail else {}),
        **({"reason": str(reason)} if reason else {}),
        **({"error_type": str(error_type)} if error_type else {}),
    }
    try:
        path = runtime.workspace_dir / "backend_state" / "her_message_audit.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as audit_log:
            audit_log.write(
                json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
            )
            audit_log.flush()
            os.fsync(audit_log.fileno())
        path.chmod(0o600)
    except Exception as exc:  # noqa: BLE001 - audit must not block presentation
        runtime.logger.warning(
            f"HER message audit write failed safely: request={record['request_id']} "
            f"event_id={record['event_id']} error_type={type(exc).__name__}"
        )


def _sha256_text(text: str) -> str:
    return "sha256:" + _hashlib.sha256(text.encode("utf-8")).hexdigest()


def request_meta_for(runtime, request_id: str) -> dict[str, Any]:
    registry = getattr(runtime, "_request_meta_by_id", None)
    if isinstance(registry, dict):
        meta = registry.get(str(request_id or ""))
        if isinstance(meta, dict):
            return dict(meta)
    current = getattr(runtime, "current_request_meta", None)
    if isinstance(current, dict) and str(current.get("request_id") or "") == str(request_id or ""):
        return dict(current)
    return {}


def _resolve_her_session_scope(runtime, item) -> str:
    explicit = str(getattr(item, "session_scope", None) or "").strip().lower()
    if explicit in {
        HER_SESSION_SCOPE_PERSISTENT,
        HER_SESSION_SCOPE_ISOLATED,
        HER_SESSION_SCOPE_ISOLATED_RESUME,
    }:
        return explicit
    if str(getattr(runtime.config, "active_backend", "") or "") != "her":
        return HER_SESSION_SCOPE_PERSISTENT
    if str(item.source or "").strip().lower().startswith("scheduler"):
        return HER_SESSION_SCOPE_ISOLATED
    return HER_SESSION_SCOPE_PERSISTENT


@dataclass(frozen=True)
class QueueItemStart:
    is_bridge_request: bool
    queued_at: datetime
    queued_monotonic: float
    queue_wait_s: float


@dataclass(frozen=True)
class TurnPrompt:
    effective_prompt: str
    final_prompt: str
    extra_sections: list[tuple[str, str]]
    incremental: bool
    prompt_audit: dict[str, Any]


@dataclass(frozen=True)
class BackendGeneration:
    response: Any | None
    detached: bool
    backend_started_monotonic: float
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
    her_message_router: Any | None


def begin_queue_item(runtime, item) -> QueueItemStart:
    if not item.silent:
        runtime.last_prompt = item
        runtime_retry.remember_retryable_prompt(runtime, item)
    is_bridge_request = item.source.startswith("bridge:") or item.source.startswith("bridge-transfer:")
    queued_at = datetime.fromisoformat(item.created_at)
    now_monotonic = time.monotonic()
    queued_monotonic = float(
        getattr(item, "queued_monotonic", now_monotonic) or now_monotonic
    )
    queue_wait_s = max(0.0, now_monotonic - queued_monotonic)
    runtime.logger.info(
        f"Processing {item.request_id} via {runtime.config.active_backend} "
        f"(source={item.source}, silent={item.silent}, prompt_len={len(item.prompt)}, "
        f"queue_wait_s={queue_wait_s:.2f})"
    )
    request_meta = {
        "request_id": item.request_id,
        "chat_id": item.chat_id,
        "prompt": item.prompt,
        "source": item.source,
        "summary": item.summary,
        "started_at": datetime.now().isoformat(),
        # Habit Meditation finishes asynchronously. Capture presentation
        # eligibility now so a later /verbose toggle cannot rewrite the
        # notification policy for this already-started task.
        "verbose_at_start": bool(getattr(runtime, "_verbose", False)),
        "silent": bool(item.silent),
        "deliver_to_telegram": bool(item.deliver_to_telegram),
        "habit_learning_eligible": bool(
            getattr(item, "habit_learning_eligible", True)
        ),
        "session_scope": _resolve_her_session_scope(runtime, item),
    }
    runtime.current_request_meta = request_meta
    registry = getattr(runtime, "_request_meta_by_id", None)
    if not isinstance(registry, dict):
        registry = {}
        runtime._request_meta_by_id = registry
    registry[item.request_id] = request_meta
    runtime._mark_activity()
    activity_store = getattr(runtime, "request_activity", None)
    if activity_store is not None:
        activity_store.mark_running(item.request_id)
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
        queued_monotonic=queued_monotonic,
        queue_wait_s=queue_wait_s,
    )


async def build_turn_prompt(runtime, item, *, is_bridge_request: bool) -> TurnPrompt:
    effective_prompt = runtime._consume_session_primer(item)
    backend = runtime.backend_manager.current_backend
    effective_prompt = runtime_cross_session.prepare_reply_binding(
        runtime, item, effective_prompt
    )
    request_meta = request_meta_for(runtime, item.request_id)
    if not request_meta.get("cross_session_receipt"):
        effective_prompt = runtime_retry.prepare_interrupted_task_continuation(
            runtime,
            item,
            effective_prompt,
            backend=str(getattr(runtime.config, "active_backend", "") or ""),
        )
        request_meta = request_meta_for(runtime, item.request_id)
    supports_sessions = bool(
        getattr(getattr(backend, "capabilities", None), "supports_sessions", False)
    )
    resume_session_id = str(request_meta.get("resume_session_id") or "").strip()
    session_id = resume_session_id or getattr(backend, "_session_id", None)
    session_scope = str(request_meta.get("session_scope") or HER_SESSION_SCOPE_PERSISTENT)
    incremental = (
        supports_sessions
        and session_id is not None
        and (
            bool(resume_session_id)
            or (
                runtime.backend_manager.agent_mode == "fixed"
                and session_scope == HER_SESSION_SCOPE_PERSISTENT
            )
        )
    )
    continuity_enabled = is_memory_plus_enabled(runtime.workspace_dir)
    extra_sections = runtime._workzone_prompt_section()
    extra_sections += runtime_turn_context.context_section(runtime, item)
    pre_turn_builder = runtime._build_pre_turn_context_sections
    pre_turn_kwargs = {"is_bridge_request": is_bridge_request}
    if "metadata" in inspect.signature(pre_turn_builder).parameters:
        pre_turn_kwargs["metadata"] = {
            "incremental": incremental,
            "continuity_enabled": continuity_enabled,
            "supports_sessions": supports_sessions,
            "session_id": session_id or "",
            "engine": runtime.config.active_backend,
        }
    extra_sections += await pre_turn_builder(item, effective_prompt, **pre_turn_kwargs)
    context_profile = None
    if continuity_enabled:
        context_profile = "memory_plus_session" if supports_sessions and runtime.backend_manager.agent_mode == "fixed" else "memory_plus_stateless"
    prompt_builder = runtime.context_assembler.build_prompt_payload
    prompt_kwargs = {
        "extra_sections": extra_sections,
        "inject_memory": not item.skip_memory_injection,
        "incremental": incremental,
    }
    if "context_profile" in inspect.signature(prompt_builder).parameters:
        prompt_kwargs["context_profile"] = context_profile
    prompt_payload = prompt_builder(
        effective_prompt,
        runtime.config.active_backend,
        **prompt_kwargs,
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

    backend_started_monotonic = time.monotonic()
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
            backend_started_monotonic=backend_started_monotonic,
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
        backend_started_monotonic=backend_started_monotonic,
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
    if stop_typing:
        stop_typing.set()
    if typing_task:
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
            delete_started = time.monotonic()
            await runtime.app.bot.delete_message(chat_id=item.chat_id, message_id=placeholder.message_id)
            delete_elapsed_s = max(0.0, time.monotonic() - delete_started)
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
        KIND_REVIEW,
        KIND_SHELL_EXEC,
        KIND_TESTING,
        KIND_TEXT_DELTA,
        KIND_THINKING,
        KIND_TOOL_END,
        KIND_TOOL_START,
        KIND_VALIDATION,
    )

    policy = telegram_stream_policy.get_policy(runtime)
    extra = getattr(getattr(runtime, "config", None), "extra", {}) or {}
    min_edit_interval = policy.edit_interval_s
    heartbeat_interval = policy.heartbeat_interval_s
    max_edits = policy.max_edits_per_request
    min_chars = int(extra.get("answer_stream_min_chars", 24))
    max_chars = int(extra.get("answer_stream_max_chars", 3400))
    loop = asyncio.get_running_loop()
    started = loop.time()
    last_edit_at = started
    last_rendered_text = ""
    edit_attempts = 0
    chunks: list[str] = []
    latest_status = "Still working..."
    latest_status_visible_with_text = False
    dirty = False
    preview_disabled = False
    status_kinds = {
        KIND_ERROR,
        KIND_FILE_EDIT,
        KIND_PROGRESS,
        KIND_REVIEW,
        KIND_SHELL_EXEC,
        KIND_TESTING,
        KIND_THINKING,
        KIND_TOOL_END,
        KIND_TOOL_START,
        KIND_VALIDATION,
    }
    assurance_status_kinds = {KIND_REVIEW, KIND_TESTING, KIND_VALIDATION}

    def _preview_text() -> str:
        text = "".join(chunks).strip()
        if len(text) > max_chars:
            text = "...\n" + text[-max_chars:]
        elapsed = max(0, int(loop.time() - started))
        header = f"✍️ {runtime.name} is replying... ({elapsed}s)\n\n"
        if text:
            if latest_status_visible_with_text:
                return header + f"📍 {latest_status}\n\n" + text
            return header + text
        return header + latest_status

    async def _edit() -> None:
        nonlocal dirty, last_edit_at, last_rendered_text, edit_attempts, preview_disabled
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
        if text == last_rendered_text:
            dirty = False
            return
        if max_edits <= 0 or edit_attempts >= max_edits:
            preview_disabled = True
            dirty = False
            runtime.telegram_logger.info(
                f"Answer stream preview budget exhausted for {item.request_id} "
                f"(attempts={edit_attempts}, max_edits={max_edits})"
            )
            return
        edit_attempts += 1
        try:
            await runtime.app.bot.edit_message_text(
                chat_id=item.chat_id,
                message_id=placeholder.message_id,
                text=_md_to_html(text),
                parse_mode="HTML",
            )
            if stream_state is not None:
                stream_state.edit_count += 1
            last_edit_at = asyncio.get_running_loop().time()
            last_rendered_text = text
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
                last_edit_at = asyncio.get_running_loop().time()
                last_rendered_text = text
                dirty = False
            else:
                last_edit_at = asyncio.get_running_loop().time()
                dirty = False
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
            elif kind in status_kinds and summary and (
                not chunks or kind in assurance_status_kinds
            ):
                latest_status = summary[:240]
                latest_status_visible_with_text = kind in assurance_status_kinds
                dirty = True
        except asyncio.TimeoutError:
            now = asyncio.get_running_loop().time()
            if policy.progress_enabled and not chunks and (now - last_edit_at) >= heartbeat_interval:
                dirty = True

        now = asyncio.get_running_loop().time()
        if dirty and (now - last_edit_at) >= min_edit_interval:
            await _edit()

    if dirty:
        await _edit()


def wrap_her_persona_stream(
    runtime,
    item,
    presentation_callback,
    *,
    backend_name: str,
    backend,
    delivery_requested: bool,
    delivery_blocked: bool = False,
    audit_collector=None,
    activity_store=None,
):
    """Create the sole HER presentation router after audit/activity persistence."""
    from adapters.stream_events import KIND_ACKNOWLEDGEMENT
    from orchestrator.her_message_router import HERMessageRouter

    normalized_backend = str(backend_name or "").strip().lower()
    if normalized_backend not in {"her", "claw-cli"}:
        return presentation_callback

    effort = str(getattr(backend, "effort", "low") or "low").strip().lower()
    runtime.logger.info(
        f"HER acknowledgement policy and message router: request={item.request_id} "
        f"commentary={bool(getattr(runtime, '_commentary', True))} "
        f"verbose={bool(getattr(runtime, '_verbose', False))} "
        f"think={bool(getattr(runtime, '_think', False))} effort={effort} "
        f"delivery_requested={delivery_requested} blocked={delivery_blocked}"
    )

    async def _send_event(event, *, purpose: str):
        await runtime_delivery_order.wait_for_turn(runtime, item.request_id)
        text = str(getattr(event, "summary", "") or "").strip()
        if not text:
            return False
        runtime.logger.info(
            f"HER message delivery started: request={item.request_id} "
            f"purpose={purpose} text_len={len(text)}"
        )
        try:
            if hasattr(runtime, "_send_text") and len(text) <= 3_500:
                result = await runtime._send_text(
                    item.chat_id,
                    text,
                    _request_id=item.request_id,
                    _purpose=purpose,
                )
                transport_accepted = result is not None
            else:
                result = await runtime.send_long_message(
                    item.chat_id,
                    text,
                    request_id=item.request_id,
                    purpose=purpose,
                )
                transport_accepted = bool(
                    isinstance(result, tuple)
                    and len(result) >= 2
                    and int(result[1] or 0) > 0
                )
        except Exception as exc:
            _append_her_message_audit(
                runtime,
                item,
                event,
                status="failed",
                reason="sender_exception",
                error_type=type(exc).__name__,
            )
            raise
        if not transport_accepted:
            _append_her_message_audit(
                runtime,
                item,
                event,
                status="not_sent",
                reason="transport_returned_no_receipt",
            )
            runtime.logger.warning(
                f"HER message was not accepted by transport: request={item.request_id} "
                f"purpose={purpose} text_len={len(text)}"
            )
            return False
        label = "acknowledgement" if purpose == "task_acknowledgement" else purpose
        _append_her_message_audit(
            runtime,
            item,
            event,
            status="transport_accepted",
        )
        runtime.logger.info(
            f"HER {label} accepted by transport: request={item.request_id} "
            f"purpose={purpose} text_len={len(text)}"
        )
        return True

    async def _commentary_presenter(event):
        purpose = (
            "task_acknowledgement"
            if getattr(event, "kind", None) == KIND_ACKNOWLEDGEMENT
            else "task_commentary"
        )
        return await _send_event(event, purpose=purpose)

    async def _control_presenter(event):
        return await _send_event(event, purpose="her_control")

    async def _persist_event(event):
        _append_her_message_audit(
            runtime,
            item,
            event,
            status="generated",
            include_text=True,
        )
        delivery_class = str(getattr(event, "delivery_class", "") or "")
        suppression_reason = ""
        if delivery_class == "user_commentary":
            if not delivery_requested:
                suppression_reason = "delivery_not_requested"
            elif delivery_blocked:
                suppression_reason = "delivery_blocked"
            elif not bool(getattr(runtime, "_commentary", True)):
                suppression_reason = "commentary_disabled"
        elif (
            delivery_class == "control"
            and bool(getattr(event, "required", False))
            and not delivery_requested
        ):
            suppression_reason = "delivery_not_requested"
        if suppression_reason:
            _append_her_message_audit(
                runtime,
                item,
                event,
                status="suppressed",
                reason=suppression_reason,
            )
        if audit_collector is not None:
            try:
                await audit_collector.record(event)
            except Exception as exc:
                runtime.logger.warning(
                    f"HER audit stream event dropped: request={item.request_id} "
                    f"error_type={type(exc).__name__}"
                )
        if activity_store is not None:
            activity_store.publish_stream(item.request_id, event)

    router = HERMessageRouter(
        request_id=item.request_id,
        logger=runtime.logger,
        technical_presenter=presentation_callback,
        reasoning_presenter=presentation_callback,
        commentary_presenter=_commentary_presenter,
        control_presenter=_control_presenter,
        verbose_enabled=lambda: bool(getattr(runtime, "_verbose", False)),
        think_enabled=lambda: bool(getattr(runtime, "_think", False)),
        commentary_enabled=lambda: bool(getattr(runtime, "_commentary", True)),
        persist_event=_persist_event,
        delivery_requested=delivery_requested,
        delivery_blocked=delivery_blocked,
    )

    async def callback(event):
        await router.route(event)

    setattr(callback, "her_message_router", router)
    return callback


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
    delivery_requested = not item.silent and item.deliver_to_telegram
    display_policy = telegram_stream_policy.get_display_policy(runtime)
    delivery_blocked = telegram_delivery_failover.is_delivery_blocked(runtime)
    typing_delivery_enabled = (
        delivery_requested and display_policy.typing_enabled and not delivery_blocked
    )
    verbose_delivery_enabled = delivery_requested and runtime._verbose and not delivery_blocked
    think_delivery_enabled = delivery_requested and runtime._think and not delivery_blocked
    backend = runtime.backend_manager.current_backend
    normalized_backend = str(
        getattr(runtime.config, "active_backend", "") or ""
    ).strip().lower()
    is_her_backend = normalized_backend in {"her", "claw-cli"}

    if delivery_requested:
        runtime.logger.info(
            f"Telegram display policy {item.request_id}: "
            f"typing={typing_delivery_enabled}, verbose={verbose_delivery_enabled}, "
            f"think={think_delivery_enabled}, source={display_policy.source}, "
            f"blocked={delivery_blocked}"
        )

    if typing_delivery_enabled or verbose_delivery_enabled or think_delivery_enabled:
        stop_typing = asyncio.Event()

    if typing_delivery_enabled or verbose_delivery_enabled:
        if typing_delivery_enabled:
            placeholder_text, placeholder_parse_mode = runtime.get_typing_placeholder()
        else:
            placeholder_text, placeholder_parse_mode = runtime.get_progress_placeholder()
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
                placeholder_started = time.monotonic()
                placeholder = await runtime.app.bot.send_message(
                    chat_id=item.chat_id,
                    text=placeholder_text,
                    parse_mode=placeholder_parse_mode,
                    disable_notification=disable_notification(runtime),
                )
                placeholder_elapsed_s = max(
                    0.0, time.monotonic() - placeholder_started
                )
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

        delivery_blocked = telegram_delivery_failover.is_delivery_blocked(runtime)
        typing_delivery_enabled = typing_delivery_enabled and not delivery_blocked
        verbose_delivery_enabled = verbose_delivery_enabled and not delivery_blocked
        think_delivery_enabled = think_delivery_enabled and not delivery_blocked
        if typing_delivery_enabled and stop_typing is not None:
            typing_task = asyncio.create_task(runtime.typing_loop(item.chat_id, stop_typing))

        capabilities = getattr(backend, "capabilities", None)
        runtime.logger.info(
            f"Verbose event eligibility {item.request_id}: enabled={verbose_delivery_enabled}, "
            f"backend={getattr(runtime.config, 'active_backend', 'unknown')}, "
            f"progress={bool(getattr(capabilities, 'supports_progress_stream', False))}, "
            f"tools={bool(getattr(capabilities, 'supports_tool_stream', False))}"
        )
        if verbose_delivery_enabled and placeholder is not None and stop_typing is not None:
            stream_queue = asyncio.Queue(maxsize=200)
            stream_callback = runtime._make_stream_callback(
                event_queue=stream_queue,
                think_buffer=runtime._think_buffer if think_delivery_enabled else None,
                audit_collector=None if is_her_backend else audit_collector,
            )
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

    if think_delivery_enabled and stop_typing is not None:
        runtime._think_buffer.clear()
        runtime._openrouter_think_chunk = ""
        runtime._last_openrouter_think_snippet = None
        if stream_callback is None:
            stream_callback = runtime._make_stream_callback(
                think_buffer=runtime._think_buffer,
                audit_collector=None if is_her_backend else audit_collector,
            )
        think_flush_task = asyncio.create_task(
            runtime._thinking_flush_loop(item.chat_id, stop_typing)
        )

    if stream_callback is None and audit_active and not is_her_backend:
        stream_callback = runtime._make_stream_callback(audit_collector=audit_collector)

    activity_store = getattr(runtime, "request_activity", None)

    stream_callback = wrap_her_persona_stream(
        runtime,
        item,
        stream_callback,
        backend_name=str(getattr(runtime.config, "active_backend", "")),
        backend=backend,
        delivery_requested=delivery_requested,
        delivery_blocked=delivery_blocked,
        audit_collector=audit_collector if is_her_backend else None,
        activity_store=activity_store if is_her_backend else None,
    )
    her_message_router = getattr(stream_callback, "her_message_router", None)

    # Local clients can observe the exact backend events and their presentation
    # ownership even when Telegram presentation is disabled.  The activity
    # store is bounded and credential-redacted by construction.
    if activity_store is not None and not is_her_backend:
        presentation_callback = stream_callback

        async def _activity_callback(event):
            activity_store.publish_stream(item.request_id, event)
            if presentation_callback is not None:
                result = presentation_callback(event)
                if inspect.isawaitable(result):
                    await result

        stream_callback = _activity_callback

    on_stream_event = stream_callback if (not item.silent or audit_active or activity_store is not None) else None
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
        her_message_router=her_message_router,
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
    await runtime_delivery_order.wait_for_turn(runtime, item.request_id)
    err_msg = EMPTY_SUCCESS_TOOL_FAILURE_MESSAGE
    runtime.logger.warning(
        f"Backend {runtime.config.active_backend} returned success with empty text for "
        f"{item.request_id} — treating as recoverable tool failure"
    )
    runtime._mark_error(err_msg)
    if runtime._should_buffer_during_transfer(item.request_id):
        runtime._record_suppressed_transfer_result(item, success=False, error=err_msg)
    delivered = False
    if not item.silent and not runtime._should_buffer_during_transfer(item.request_id):
        _elapsed, chunk_count = await runtime.send_long_message(
            chat_id=item.chat_id,
            text=err_msg,
            request_id=item.request_id,
            purpose="error",
        )
        delivered = chunk_count > 0
    runtime_cross_session.record_turn_result(
        runtime,
        item,
        error=err_msg,
        delivered=delivered,
        completion_path="foreground",
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
    visible_text, wrapper_result = await runtime._apply_wrapper_to_visible_text(
        item,
        display_text or response.text,
    )
    if _contains_dangling_tool_markup(response.text) or _contains_dangling_tool_markup(
        visible_text
    ):
        fallback = _safe_blocked_tool_markup_final(item)
        runtime.logger.error(
            f"Blocked dangling tool markup at the final delivery boundary: request={item.request_id}"
        )
        response.text = fallback
        response.stop_reason = "no_final_text"
        metadata = getattr(response, "stream_metadata", None)
        metadata = dict(metadata) if isinstance(metadata, dict) else {}
        metadata.update(
            {
                "claw_completion_status": "incomplete",
                "claw_stop_reason": "no_final_text",
                "dangling_tool_markup_blocked": True,
            }
        )
        response.stream_metadata = metadata
        display_text = fallback
        visible_text = fallback
    if not visible_text.strip():
        return SuccessfulResponse(
            display_text=display_text,
            visible_text=visible_text,
            wrapper_result=wrapper_result,
        )
    runtime._mark_success()
    safe_core_raw = extract_memory_plus_update_details(response.text).visible_text
    runtime._append_core_transcript(
        item,
        core_raw=safe_core_raw,
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
            **runtime._wrapper_listener_fields(safe_core_raw, visible_text, wrapper_result),
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
    if item.source.lower() in {"document", "photo", "voice", "audio", "video", "sticker", "multimodal"}:
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
        runtime.handoff_builder.append_transcript("assistant", visible_text, item.source)
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
    user_interrupt_reason: str | None = None,
    queued_monotonic: float | None = None,
) -> None:
    await runtime_delivery_order.wait_for_turn(runtime, item.request_id)
    err_msg = response.error or "Unknown error"
    # /stop, /steer, and /retry intentionally kill the backend process
    # (e.g. exit -9 / SIGKILL).
    # That is expected course-correction, not a backend failure — never show ❌ Backend error.
    if not user_interrupt_reason:
        from orchestrator.runtime_control import consume_user_interrupt

        user_interrupt_reason = consume_user_interrupt(runtime, getattr(item, "request_id", None))

    if user_interrupt_reason:
        soft_msg = f"Interrupted by {user_interrupt_reason}"
        runtime.logger.info(
            f"Suppressed backend exit for {item.request_id} "
            f"(reason={user_interrupt_reason}, backend={runtime.config.active_backend}, "
            f"source={item.source}): {err_msg}"
        )
        if runtime._should_buffer_during_transfer(item.request_id):
            runtime._record_suppressed_transfer_result(item, success=False, error=soft_msg)
        await runtime._notify_request_listeners(
            item.request_id,
            {
                "request_id": item.request_id,
                "success": False,
                "text": None,
                "error": soft_msg,
                "source": item.source,
                "summary": item.summary,
                "interrupted": True,
                "interrupt_reason": user_interrupt_reason,
            },
        )
        runtime._log_maintenance(
            item,
            "user_interrupt",
            reason=user_interrupt_reason,
            error_excerpt=_safe_excerpt(err_msg, 200),
            queue_wait_s=queue_wait_s,
            backend_elapsed_s=backend_elapsed_s,
        )
        runtime_cross_session.record_turn_result(
            runtime,
            item,
            response=response,
            error=soft_msg,
            delivered=False,
            completion_path="foreground",
        )
        return

    runtime._mark_error(err_msg)
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
        runtime_cross_session.record_turn_result(
            runtime,
            item,
            response=response,
            error=err_msg,
            delivered=False,
            completion_path="foreground",
        )
        return
    runtime.error_logger.error(
        f"Flex Backend error for {item.request_id} "
        f"({runtime.config.active_backend}, source={item.source}): {err_msg}"
    )
    if runtime._should_retry_codex_scheduler_failure(item, err_msg):
        runtime._schedule_codex_scheduler_retry(item)
    if not item.deliver_to_telegram:
        runtime_cross_session.record_turn_result(
            runtime,
            item,
            response=response,
            error=err_msg,
            delivered=False,
            completion_path="foreground",
        )
        return
    if runtime._should_buffer_during_transfer(item.request_id):
        runtime_cross_session.record_turn_result(
            runtime,
            item,
            response=response,
            error=err_msg,
            delivered=False,
            completion_path="foreground",
        )
        return
    send_elapsed_s, chunk_count = await runtime.send_long_message(
        chat_id=item.chat_id,
        text=err_msg,
        request_id=item.request_id,
        purpose="error",
    )
    total_elapsed_s = (
        max(0.0, time.monotonic() - queued_monotonic)
        if queued_monotonic is not None
        else max(0.0, (datetime.now() - queued_at).total_seconds())
    )
    runtime.logger.info(
        f"Completed {item.request_id} error delivery via {runtime.config.active_backend} "
        f"(queue_wait_s={queue_wait_s:.2f}, backend_s={backend_elapsed_s:.2f}, "
        f"telegram_send_s={send_elapsed_s:.2f}, total_s={total_elapsed_s:.2f}, "
        f"chunks={chunk_count})"
    )
    runtime._log_maintenance(item, "send_error", error_excerpt=_safe_excerpt(err_msg, 200))
    runtime_cross_session.record_turn_result(
        runtime,
        item,
        response=response,
        error=err_msg,
        delivered=chunk_count > 0,
        completion_path="foreground",
    )


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
    her_message_router=None,
    queued_monotonic: float | None = None,
) -> None:
    await runtime_delivery_order.wait_for_turn(runtime, item.request_id)
    runtime_retry.clear_completed_interrupted_task(runtime, item)
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
        runtime_cross_session.record_turn_result(
            runtime,
            item,
            assistant_text=visible_text,
            response=response,
            delivered=False,
            completion_path="foreground",
        )
        return
    runtime_retry.remember_output(runtime, item, visible_text)
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
        runtime_cross_session.record_turn_result(
            runtime,
            item,
            assistant_text=visible_text,
            response=response,
            delivered=False,
            completion_path="foreground",
        )
        return

    response_text = visible_text
    cos_handled = False
    safe_core_raw = extract_memory_plus_update_details(response.text).visible_text
    await runtime._send_wrapper_verbose_trace(item, safe_core_raw, visible_text, wrapper_result)
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
    runtime_cross_session.record_turn_result(
        runtime,
        item,
        assistant_text=response_text,
        response=response,
        delivered=bool(
            stream_finalization.final_delivered
            or (stream_finalization.fallback_required and chunk_count > 0)
        ),
        completion_path="foreground",
    )
    await runtime._send_voice_reply(item.chat_id, response_text, item.request_id)
    runtime._schedule_audit_followup(
        item,
        core_raw=safe_core_raw,
        visible_text=visible_text,
        response=response,
        audit_collector=audit_collector,
        completion_path="foreground",
    )
    total_elapsed_s = (
        max(0.0, time.monotonic() - queued_monotonic)
        if queued_monotonic is not None
        else max(0.0, (datetime.now() - queued_at).total_seconds())
    )
    runtime.logger.info(
        f"Completed {item.request_id} delivery via {runtime.config.active_backend} "
        f"(queue_wait_s={queue_wait_s:.2f}, backend_s={backend_elapsed_s:.2f}, "
        f"telegram_send_s={send_elapsed_s:.2f}, total_s={total_elapsed_s:.2f}, "
        f"chunks={chunk_count})"
    )
    runtime._log_maintenance(item, "send_success", text_len=len(response_text or ""))
    if not cos_handled:
        await runtime._hchat_route_reply(item, response_text)
