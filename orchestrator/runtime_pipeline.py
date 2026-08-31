from __future__ import annotations

import asyncio
import copy
import hashlib as _hashlib
import html
import inspect
import json
import os
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from telegram.error import BadRequest, RetryAfter

from orchestrator import (
    runtime_cross_session,
    runtime_delivery_order,
    runtime_retry,
    runtime_session,
    terminal_console,
    telegram_delivery_failover,
    telegram_notifications,
    telegram_stream_policy,
    ui_language,
)
from orchestrator.command_ui import card_title
from orchestrator.flexible_backend_registry import canonical_backend_engine
from orchestrator.memory_plus_mode import (
    extract_memory_plus_update_details,
    is_memory_plus_enabled,
)
from orchestrator.runtime_common import (
    _md_to_html,
    _print_final_response,
    _safe_excerpt,
)

EMPTY_SUCCESS_TOOL_FAILURE_MESSAGE = (
    "I wasn't able to complete that — a tool I tried to use didn't return a result. "
    "Please check that all required API keys (e.g. brave_api_key for web search) are configured in secrets.json."
)
INTERACTIVE_FEEDBACK_CLEANUP_TIMEOUT_SECONDS = 5.0

SESSION_SCOPE_PERSISTENT = "persistent"
SESSION_SCOPE_ISOLATED = "isolated_per_run"
SESSION_SCOPE_ISOLATED_RESUME = "isolated_resume"


def response_has_deliverable_content(response: Any) -> bool:
    declared = getattr(response, "has_deliverable_content", None)
    if isinstance(declared, bool):
        return declared
    if str(getattr(response, "text", "") or "").strip():
        return True
    from orchestrator.native_audio_delivery import audio_parts

    return bool(audio_parts(getattr(response, "content", ())))


def observe_terminal_response(runtime, item, response) -> None:
    """Capture content-free response metrics before terminal completion."""

    runtime_name = getattr(runtime, "name", None)
    if not runtime_name:
        return
    terminal_console.observe_response(runtime_name, item.request_id, response)
    if getattr(response, "usage", None) is not None:
        return
    response_text = str(getattr(response, "text", "") or "")
    terminal_console.observe_estimated_usage(
        runtime_name,
        item.request_id,
        input_tokens=None,
        output_tokens=(len(response_text) + 3) // 4,
        thinking_tokens=None,
    )


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


def _canonical_record(
    runtime,
    event_type: str,
    payload: Any,
    *,
    request_id: str = "",
    provenance: dict[str, Any] | None = None,
) -> None:
    store = getattr(runtime, "canonical_audit", None)
    if store is None:
        return
    try:
        store.record(
            event_type,
            payload,
            request_id=request_id,
            provenance=provenance,
        )
    except Exception as exc:
        runtime.error_logger.error(
            "Canonical audit write failed for %s/%s: %s",
            request_id or "<none>",
            event_type,
            exc,
        )


@dataclass
class _ProvisionalTelegramMessage:
    """Telegram state needed to resolve one provisional HER message."""

    message_id: Any
    rendered_text: str
    parse_mode: str | None = "HTML"
    reply_markup: Any | None = None


def _is_message_not_modified_error(exc: Exception) -> bool:
    """Recognise only Telegram's explicit idempotent no-change response."""

    return isinstance(exc, BadRequest) and (
        "message is not modified" in str(exc).casefold()
    )


def _her_event_suppression_reason(event) -> str:
    if str(getattr(event, "delivery_class", "") or "") != "internal":
        return ""
    detail = str(getattr(event, "detail", "") or "")
    for part in detail.split(";"):
        key, separator, value = part.strip().partition("=")
        if separator and key == "suppressed_reason":
            return value.strip()
    return ""


def _contains_dangling_tool_markup(text: Any) -> bool:
    normalized = str(text or "").lower()
    visible = "\n".join(normalized.split("```")[::2])
    return any(marker in visible for marker in _DANGLING_TOOL_MARKERS)


def _safe_blocked_tool_markup_final(runtime, item) -> str:
    locale = ui_language.preferred_locale(
        runtime,
        actor_id=getattr(item, "owner_id", None) or getattr(item, "chat_id", None),
    )
    return ui_language.tr("safety.dangling_tool", locale=locale)


def queued_elapsed_s(item) -> float:
    """Return request age from a process-local clock when available."""

    queued_monotonic = getattr(item, "queued_monotonic", None)
    if queued_monotonic is not None:
        return max(0.0, time.monotonic() - float(queued_monotonic))
    return max(
        0.0,
        (datetime.now() - datetime.fromisoformat(item.created_at)).total_seconds(),
    )


def skill_usage_audit_fields(item: Any) -> dict[str, str]:
    """Return optional Skill attribution without emitting empty audit fields."""

    fields: dict[str, str] = {}
    skill_id = str(getattr(item, "skill_id", "") or "").strip()
    usage_event_id = str(
        getattr(item, "skill_usage_event_id", "") or ""
    ).strip()
    if skill_id:
        fields["skill_id"] = skill_id
    if usage_event_id:
        fields["skill_usage_event_id"] = usage_event_id
    return fields


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
    """Persist one low-volume HER user-message lineage audit record."""

    from adapters.stream_events import (
        DELIVERY_CONTROL,
        DELIVERY_USER_COMMENTARY,
        KIND_COMMENTARY,
    )

    delivery_class = str(getattr(event, "delivery_class", "") or "")
    suppression_reason = _her_event_suppression_reason(event)
    suppressed_commentary = getattr(event, "kind", None) == KIND_COMMENTARY and bool(
        suppression_reason
    )
    if delivery_class == DELIVERY_CONTROL and not bool(
        getattr(event, "required", False)
    ):
        return
    if (
        delivery_class not in {DELIVERY_CONTROL, DELIVERY_USER_COMMENTARY}
        and not suppressed_commentary
    ):
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


def _store_context_compaction_warnings(
    runtime,
    request_id: str,
    warnings: tuple[str, ...],
    *,
    field: str = "context_compaction_warnings",
) -> None:
    if not warnings:
        return
    warning_list = list(warnings)
    registry = getattr(runtime, "_request_meta_by_id", None)
    if isinstance(registry, dict):
        meta = registry.get(str(request_id or ""))
        if isinstance(meta, dict):
            meta[field] = warning_list
    current = getattr(runtime, "current_request_meta", None)
    if isinstance(current, dict) and str(current.get("request_id") or "") == str(
        request_id or ""
    ):
        current[field] = warning_list


def request_context_warning_fields(runtime, request_id: str) -> dict[str, Any]:
    meta = request_meta_for(runtime, request_id)
    result: dict[str, Any] = {}
    for field in ("context_compaction_warnings", "wip_recovery_warnings"):
        warnings = meta.get(field)
        if isinstance(warnings, list) and warnings:
            result[field] = list(warnings)
    return result


def surface_context_compaction_warnings(
    runtime,
    item,
    warnings: tuple[str, ...],
    *,
    _metadata_field: str = "context_compaction_warnings",
    _log_label: str = "Context compaction warning",
    _audit_prefix: str = "capacity",
    _purpose: str = "context-compaction-warning",
    _origin: str = "hashi_context_compaction",
    _stream_summary: str = (
        "⚠️ Context compaction did not complete; the current model request is continuing."
    ),
) -> None:
    """Expose mandatory warnings without delaying or cancelling model work."""

    if not warnings:
        return

    def audit_presentation(
        event: str,
        warning: str,
        index: int,
        **payload: Any,
    ) -> None:
        try:
            from orchestrator.context_compaction import coordinator_for

            coordinator_for(runtime, request_ref=item.request_id).store.append_audit(
                event,
                compaction_id=(
                    "warning-"
                    + _sha256_text(
                        f"{item.request_id}:{index}:{warning}"
                    ).removeprefix("sha256:")[:24]
                ),
                payload={
                    "request_ref": item.request_id,
                    "warning_index": index,
                    "warning_sha256": _sha256_text(warning),
                    "will_continue": True,
                    **payload,
                },
            )
        except Exception as exc:  # warning audit must not gate model work
            runtime.logger.warning(
                "Context compaction warning presentation audit failed safely for %s: %s",
                item.request_id,
                type(exc).__name__,
            )

    _store_context_compaction_warnings(
        runtime,
        item.request_id,
        warnings,
        field=_metadata_field,
    )
    for index, warning in enumerate(warnings, start=1):
        runtime.logger.warning(
            "%s; continuing request=%s: %s",
            _log_label,
            item.request_id,
            _safe_excerpt(warning, 600),
        )
        audit_presentation(
            f"{_audit_prefix}_warning_scheduled",
            warning,
            index,
            telegram_requested=bool(
                getattr(item, "deliver_to_telegram", False)
            ),
            request_metadata_exposed=True,
        )

    activity_store = getattr(runtime, "request_activity", None)
    if activity_store is not None:
        try:
            from adapters.stream_events import (
                DELIVERY_CONTROL,
                KIND_PROGRESS,
                StreamEvent,
            )

            for index, warning in enumerate(warnings, start=1):
                activity_store.publish_stream(
                    item.request_id,
                    StreamEvent(
                        kind=KIND_PROGRESS,
                        summary=_stream_summary,
                        detail=_safe_excerpt(warning, 1200),
                        event_id=(
                            f"{_purpose}:{item.request_id}:{index}"
                        ),
                        delivery_class=DELIVERY_CONTROL,
                        origin=_origin,
                        phase="pre_model",
                        required=True,
                    ),
                )
        except Exception as exc:  # presentation telemetry must never stop work
            runtime.logger.warning(
                "Context compaction activity warning failed safely for %s: %s",
                item.request_id,
                type(exc).__name__,
            )

    if not bool(getattr(item, "deliver_to_telegram", False)):
        return

    async def deliver() -> None:
        for index, warning in enumerate(warnings, start=1):
            try:
                _elapsed, chunk_count = await runtime.send_long_message(
                    item.chat_id,
                    warning,
                    request_id=item.request_id,
                    purpose=_purpose,
                    parse_mode="HTML",
                )
                audit_presentation(
                    f"{_audit_prefix}_warning_delivery",
                    warning,
                    index,
                    channel="telegram",
                    delivered=bool(chunk_count > 0),
                    chunk_count=int(chunk_count),
                )
            except Exception as exc:  # warning delivery cannot gate model work
                runtime.error_logger.warning(
                    "Context compaction user warning delivery failed safely for %s: %s: %s",
                    item.request_id,
                    type(exc).__name__,
                    exc,
                )
                audit_presentation(
                    f"{_audit_prefix}_warning_delivery",
                    warning,
                    index,
                    channel="telegram",
                    delivered=False,
                    error_type=type(exc).__name__,
                )

    task = asyncio.create_task(
        deliver(),
        name=f"{_purpose}-{item.request_id}",
    )
    background_tasks = getattr(runtime, "_background_tasks", None)
    if isinstance(background_tasks, set):
        background_tasks.add(task)
        task.add_done_callback(background_tasks.discard)


def surface_wip_recovery_warning(
    runtime,
    item,
    *,
    record_count: int,
    size_bytes: int,
    first_request_id: str = "",
) -> None:
    """Warn visibly when unfinished HER v2 recovery state precedes a turn."""

    locale = ui_language.preferred_locale(
        runtime,
        actor_id=getattr(item, "owner_id", None) or getattr(item, "chat_id", None),
    )
    lines = [
        card_title("⚠️", "Unfinished work", locale=locale),
        "",
        f"<b>{html.escape(ui_language.tr('wip.status_label', locale=locale))}</b> · "
        f"<code>{html.escape(ui_language.tr('wip.status_ready', locale=locale))}</code>",
        "",
        f"<b>{html.escape(ui_language.tr('wip.records', locale=locale))}</b> · "
        f"<code>{max(0, int(record_count)):,}</code>",
        f"<b>{html.escape(ui_language.tr('wip.saved_data', locale=locale))}</b> · "
        f"<code>{html.escape(ui_language.tr('wip.bytes_value', locale=locale, count=f'{max(0, int(size_bytes)):,}'))}</code>",
    ]
    if first_request_id:
        lines.append(
            f"<b>{html.escape(ui_language.tr('wip.first_request', locale=locale))}</b> · "
            f"<code>{html.escape(first_request_id)}</code>"
        )
    lines.extend(
        [
            "",
            html.escape(ui_language.tr("wip.explanation", locale=locale)),
            "",
            ui_language.tr("wip.action", locale=locale).replace(
                "/compact",
                "<code>/compact</code>",
            ),
        ]
    )
    warning = "\n".join(lines)
    surface_context_compaction_warnings(
        runtime,
        item,
        (warning,),
        _metadata_field="wip_recovery_warnings",
        _log_label="HER v2 WIP recovery warning",
        _audit_prefix="wip_recovery",
        _purpose="wip-recovery-warning",
        _origin="hashi_wip_recovery",
        _stream_summary="⚠️ " + ui_language.tr("wip.stream_summary", locale=locale),
    )


def _her_v2_delivery_metadata(response: Any) -> dict[str, Any]:
    metadata = getattr(response, "stream_metadata", None)
    if not isinstance(metadata, dict):
        return {}
    her_v2 = metadata.get("her_v2")
    if not isinstance(her_v2, dict):
        return {}
    delivery = her_v2.get("delivery")
    if not isinstance(delivery, dict) or not str(
        delivery.get("delivery_id") or ""
    ).strip():
        return {}
    return {
        **delivery,
        "final_already_delivered": bool(
            her_v2.get("final_already_delivered")
        ),
    }


async def record_her_v2_transport_receipt(
    runtime: Any,
    item: Any,
    response: Any,
    *,
    delivered: bool,
    disposition: str,
    chunk_count: int = 0,
    completion_path: str = "foreground",
    error_type: str = "",
) -> bool:
    """Write the ordinary HASHI transport result back to the HER v2 audit."""

    delivery = _her_v2_delivery_metadata(response)
    if not delivery:
        return False
    backend = getattr(getattr(runtime, "backend_manager", None), "current_backend", None)
    recorder = getattr(backend, "record_transport_delivery_receipt", None)
    if not callable(recorder):
        runtime.logger.warning(
            f"HER v2 transport receipt recorder unavailable: request={item.request_id} "
            f"delivery_id={delivery['delivery_id']}"
        )
        return False
    try:
        result = recorder(
            request_id=str(item.request_id),
            delivery_id=str(delivery["delivery_id"]),
            delivered=bool(delivered),
            disposition=str(disposition or "unknown"),
            transport="telegram",
            chunk_count=max(0, int(chunk_count or 0)),
            completion_path=str(completion_path or "foreground"),
            error_type=str(error_type or ""),
        )
        if inspect.isawaitable(result):
            result = await result
        return bool(result)
    except Exception as exc:  # delivery already happened; preserve and surface audit failure
        runtime.logger.warning(
            f"HER v2 transport receipt write failed: request={item.request_id} "
            f"delivery_id={delivery['delivery_id']} error_type={type(exc).__name__}"
        )
        return False


def _resolve_session_scope(item) -> str:
    explicit = str(getattr(item, "session_scope", None) or "").strip().lower()
    if explicit in {
        SESSION_SCOPE_PERSISTENT,
        SESSION_SCOPE_ISOLATED,
        SESSION_SCOPE_ISOLATED_RESUME,
    }:
        return explicit
    scheduler_context = getattr(item, "scheduler_context", None)
    if isinstance(scheduler_context, Mapping):
        kind = str(scheduler_context.get("kind") or "").strip().lower()
        task_id = str(scheduler_context.get("task_id") or "").strip()
        trigger = str(scheduler_context.get("trigger") or "").strip().lower()
        if (
            kind in {"cron", "heartbeat"}
            and task_id
            and trigger in {"scheduled", "manual", "recovery"}
        ):
            # Scheduled prompt work is a standalone invocation. Sharing the
            # ordinary Session timeline lets the immediately preceding job
            # masquerade as context for the next job, even though the new job
            # prompt is the authoritative request.
            return SESSION_SCOPE_ISOLATED
    return SESSION_SCOPE_PERSISTENT


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
    extra_sections: list[tuple]
    incremental: bool
    prompt_audit: dict[str, Any]
    context_warnings: tuple[str, ...] = ()


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


@dataclass
class VerboseDisplayState:
    """Mutable Telegram message ownership for a rolling verbose display."""

    current_message: Any
    message_ids: list[int]
    rollover_count: int = 0


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
    verbose_display_state: VerboseDisplayState | None = None


def begin_queue_item(runtime, item) -> QueueItemStart:
    runtime_session.apply_item_workzone(runtime, item)
    runtime_session.activate_backend_binding(runtime, item)
    runtime_session.mark_running(runtime, item)
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
        "meter_at_start": bool(getattr(runtime, "_meter", False)),
        "silent": bool(item.silent),
        "deliver_to_telegram": bool(item.deliver_to_telegram),
        "hashi_session_id": getattr(item, "session_id", None),
        "hashi_run_id": getattr(item, "run_id", None),
        "hashi_message_id": getattr(item, "message_id", None),
        "context_generation": int(getattr(item, "context_generation", 1) or 1),
        "owner_id": getattr(item, "owner_id", None),
        "session_surface": getattr(item, "session_surface", None),
        "session_channel_key": getattr(item, "session_channel_key", None),
        "session_workspace": str(
            (getattr(item, "request_metadata", None) or {}).get("session_workspace")
            or ""
        ),
        "habit_learning_eligible": bool(
            getattr(item, "habit_learning_eligible", True)
        ),
        **skill_usage_audit_fields(item),
        "session_scope": _resolve_session_scope(item),
    }
    scheduler_context = getattr(item, "scheduler_context", None)
    if isinstance(scheduler_context, dict) and scheduler_context:
        request_meta["scheduler_context"] = dict(scheduler_context)
    request_metadata = getattr(item, "request_metadata", None)
    if isinstance(request_metadata, dict) and request_metadata:
        request_meta["request_metadata"] = copy.deepcopy(request_metadata)
    request_content = getattr(item, "request_content", None)
    if isinstance(request_content, dict) and request_content:
        request_meta["request_content"] = copy.deepcopy(request_content)
    manifest = getattr(item, "attachment_manifest", ())
    if manifest:
        request_meta["attachment_manifest"] = [
            copy.deepcopy(entry) for entry in manifest
        ]
    runtime.current_request_meta = request_meta
    _canonical_record(
        runtime,
        "request_received",
        request_meta,
        request_id=item.request_id,
        provenance={"source": item.source, "queue": "flex_runtime"},
    )
    registry = getattr(runtime, "_request_meta_by_id", None)
    if not isinstance(registry, dict):
        registry = {}
        runtime._request_meta_by_id = registry
    registry[item.request_id] = request_meta
    runtime._mark_activity()
    activity_store = getattr(runtime, "request_activity", None)
    if activity_store is not None:
        activity_store.mark_running(item.request_id)
    terminal_console.start_request(
        runtime.name,
        item.request_id,
        source=item.source,
        backend=runtime.config.active_backend,
    )
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
    refresh_tool_context = getattr(
        getattr(runtime, "backend_manager", None),
        "_refresh_tool_runtime_context",
        None,
    )
    if callable(refresh_tool_context):
        refresh_tool_context(item.request_id)
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
    provider_session_id = getattr(backend, "_session_id", None)
    session_scope = str(request_meta.get("session_scope") or SESSION_SCOPE_PERSISTENT)
    isolated_scheduler_run = session_scope == SESSION_SCOPE_ISOLATED
    incremental = (
        supports_sessions
        and provider_session_id is not None
        and runtime.backend_manager.agent_mode == "fixed"
        and session_scope == SESSION_SCOPE_PERSISTENT
    )
    continuity_enabled = (
        is_memory_plus_enabled(runtime.workspace_dir)
        and not isolated_scheduler_run
    )
    session_scoped = bool(str(getattr(item, "session_id", "") or ""))
    session_workspace = runtime_session.item_session_workspace(runtime, item)
    session_history = (
        []
        if isolated_scheduler_run
        else runtime_session.recent_exchanges(
            runtime,
            item,
            limit=int(
                getattr(runtime.context_assembler, "MAX_RECENT_EXCHANGES", 8)
            ),
        )
    )
    extra_sections = runtime._workzone_prompt_section()
    pre_turn_builder = runtime._build_pre_turn_context_sections
    pre_turn_kwargs = {"is_bridge_request": is_bridge_request}
    if "metadata" in inspect.signature(pre_turn_builder).parameters:
        pre_turn_kwargs["metadata"] = {
            "incremental": incremental,
            "continuity_enabled": continuity_enabled,
            "supports_sessions": supports_sessions,
            "session_id": getattr(item, "session_id", None) or "",
            "backend_session_id": provider_session_id or "",
            "context_generation": int(getattr(item, "context_generation", 1) or 1),
            "session_workspace": str(session_workspace),
            "engine": runtime.config.active_backend,
        }
    if not isolated_scheduler_run:
        extra_sections += await pre_turn_builder(
            item, effective_prompt, **pre_turn_kwargs
        )
    base_extra_sections = list(extra_sections)
    context_profile = None
    if continuity_enabled:
        context_profile = "memory_plus_session" if supports_sessions and runtime.backend_manager.agent_mode == "fixed" else "memory_plus_stateless"
    prompt_builder = runtime.context_assembler.build_prompt_payload
    prompt_kwargs = {
        "extra_sections": extra_sections,
        "inject_memory": (
            not item.skip_memory_injection and not isolated_scheduler_run
        ),
        "incremental": incremental,
    }
    if "context_profile" in inspect.signature(prompt_builder).parameters:
        prompt_kwargs["context_profile"] = context_profile
    if (
        session_history is not None
        and "recent_exchanges" in inspect.signature(prompt_builder).parameters
    ):
        prompt_kwargs["recent_exchanges"] = session_history
    compaction_snapshot = None
    history_compaction_enabled = False
    cross_session_timeline_entries: list[dict[str, Any]] = []
    if (
        runtime.config.active_backend == "her-v2"
        and not incremental
        and not item.skip_memory_injection
        and not isolated_scheduler_run
        and not is_bridge_request
        and bool(
            getattr(
                getattr(runtime, "context_assembler", None),
                "turns_injection_enabled",
                True,
            )
        )
    ):
        from orchestrator.context_compaction import install_history_section

        cross_session_timeline_entries = runtime_cross_session.timeline_entries(
            runtime,
            item,
        )
        extra_sections, compaction_snapshot = install_history_section(
            runtime,
            base_extra_sections,
            cross_session_entries=cross_session_timeline_entries,
            primary_timeline_entries=session_history,
            workspace_dir=session_workspace if session_scoped else None,
            memory_store=(
                runtime_session.session_memory_store(runtime, item)
                if session_scoped
                else None
            ),
        )
        history_compaction_enabled = compaction_snapshot is not None
        prompt_kwargs["extra_sections"] = extra_sections

    def assemble(sections: list[tuple]) -> dict[str, Any]:
        current_kwargs = dict(prompt_kwargs)
        current_kwargs["extra_sections"] = sections
        return prompt_builder(
            effective_prompt,
            runtime.config.active_backend,
            **current_kwargs,
        )

    prompt_payload = assemble(extra_sections)
    context_warnings: list[str] = []
    if runtime.config.active_backend == "her-v2" and not incremental:
        from orchestrator.context_compaction import (
            coordinator_for,
            estimate_effective_context_tokens,
            estimate_tokens,
            load_policy,
            resolve_trigger_budget,
        )

        prompt_tokens = estimate_tokens(prompt_payload["final_prompt"])
        if history_compaction_enabled:
            budget = resolve_trigger_budget(runtime, policy=load_policy(runtime))
            effective_tokens = estimate_effective_context_tokens(
                runtime,
                prompt_tokens=prompt_tokens,
                coordinator=coordinator_for(
                    runtime,
                    request_ref=item.request_id,
                    workspace_dir=session_workspace if session_scoped else None,
                    memory_store=(
                        runtime_session.session_memory_store(runtime, item)
                        if session_scoped
                        else None
                    ),
                ),
            )
            if effective_tokens > budget.high_projected_tokens:
                coordinator = coordinator_for(
                    runtime,
                    request_ref=item.request_id,
                    workspace_dir=session_workspace if session_scoped else None,
                    memory_store=(
                        runtime_session.session_memory_store(runtime, item)
                        if session_scoped
                        else None
                    ),
                )
                outcome = await coordinator.compact(
                    trigger="pre_triage_context_pressure",
                    request_ref=item.request_id,
                    force=True,
                )
                if outcome.changed:
                    from orchestrator.context_compaction import install_history_section

                    extra_sections, compaction_snapshot = install_history_section(
                        runtime,
                        base_extra_sections,
                        cross_session_entries=cross_session_timeline_entries,
                        primary_timeline_entries=session_history,
                        workspace_dir=session_workspace if session_scoped else None,
                        memory_store=(
                            runtime_session.session_memory_store(runtime, item)
                            if session_scoped
                            else None
                        ),
                    )
                    prompt_payload = assemble(extra_sections)
                    prompt_tokens = estimate_tokens(prompt_payload["final_prompt"])
        request_tokens = getattr(runtime, "_context_compaction_prompt_tokens", None)
        if not isinstance(request_tokens, dict):
            request_tokens = {}
            runtime._context_compaction_prompt_tokens = request_tokens
        request_tokens[item.request_id] = prompt_tokens
        runtime._last_full_prompt_tokens = prompt_tokens
    _canonical_record(
        runtime,
        "provider_request",
        {
            "engine": runtime.config.active_backend,
            "effective_user_prompt": effective_prompt,
            "final_prompt": prompt_payload.get("final_prompt"),
            "pcm_envelope": prompt_payload.get("envelope"),
            "prompt_audit": prompt_payload.get("audit"),
            "incremental": incremental,
        },
        request_id=item.request_id,
        provenance={"source": "hashi_pcm_assembler"},
    )
    final_prompt = prompt_payload["final_prompt"]
    prompt_audit = prompt_payload.get("audit", {})
    runtime._last_prompt_audit = prompt_audit
    runtime._thinking_chars_this_req = 0
    if runtime.config.active_backend != "her-v2" or incremental:
        runtime._last_full_prompt_tokens = len(final_prompt) // 4
    terminal_console.observe_estimated_usage(
        runtime.name,
        item.request_id,
        input_tokens=int(getattr(runtime, "_last_full_prompt_tokens", 0) or 0),
        output_tokens=0,
        thinking_tokens=0,
    )
    if runtime.config.active_backend == "her-v2" and not incremental:
        states = getattr(runtime, "_context_compaction_prompt_states", None)
        if not isinstance(states, dict):
            states = {}
            runtime._context_compaction_prompt_states = states
        request_token_map = getattr(
            runtime,
            "_context_compaction_prompt_tokens",
            None,
        )
        if not isinstance(request_token_map, dict):
            request_token_map = {}
        states[item.request_id] = {
            "effective_prompt": effective_prompt,
            "base_extra_sections": list(base_extra_sections),
            "cross_session_timeline_entries": list(
                cross_session_timeline_entries
            ),
            "primary_timeline_entries": (
                list(session_history) if session_history is not None else None
            ),
            "session_workspace": str(session_workspace) if session_scoped else "",
            "context_profile": context_profile,
            "inject_memory": history_compaction_enabled,
            "is_bridge_request": bool(is_bridge_request),
            "final_prompt": final_prompt,
            "prompt_tokens": int(
                request_token_map.get(
                    item.request_id,
                    max(1, len(final_prompt) // 4),
                )
            ),
            "capacity_recovery_attempted": False,
        }
    return TurnPrompt(
        effective_prompt=effective_prompt,
        final_prompt=final_prompt,
        extra_sections=extra_sections,
        incremental=incremental,
        prompt_audit=prompt_audit,
        context_warnings=tuple(context_warnings),
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

    generation_kwargs = {
        "is_retry": item.is_retry,
        "silent": item.silent,
        "on_stream_event": on_stream_event,
    }
    request_content = getattr(item, "request_content", None)
    if request_content is not None:
        generation_kwargs["request_content"] = copy.deepcopy(request_content)

    if background_mode:
        generation_task = asyncio.create_task(
            runtime.backend_manager.generate_response(
                final_prompt,
                item.request_id,
                **generation_kwargs,
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
            **generation_kwargs,
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
    response_payload = dict(vars(response)) if hasattr(response, "__dict__") else {"repr": repr(response)}
    response_payload["engine"] = runtime.config.active_backend
    response_payload["backend_elapsed_s"] = backend_elapsed_s
    response_payload["final_prompt"] = final_prompt
    _canonical_record(
        runtime,
        "provider_response",
        response_payload,
        request_id=item.request_id,
        provenance={"source": "active_backend"},
    )
    reasoning_seen = getattr(runtime, "_canonical_reasoning_seen", set())
    if item.request_id not in reasoning_seen:
        _canonical_record(
            runtime,
            "provider_reasoning",
            {"availability": "unavailable", "reason": "provider_exposed_no_reasoning_event"},
            request_id=item.request_id,
            provenance={"source": "active_backend", "fabricated": False},
        )
    if isinstance(reasoning_seen, set):
        reasoning_seen.discard(item.request_id)


def _consume_feedback_task_result(task: asyncio.Future) -> None:
    """Retrieve a detached cleanup task result without surfacing loop warnings."""

    try:
        task.result()
    except (asyncio.CancelledError, Exception):
        pass


async def settle_interactive_feedback_task(
    runtime,
    task: asyncio.Future | None,
    *,
    label: str,
    cancel_first: bool = False,
    timeout_s: float | None = None,
) -> bool:
    """Bound feedback cleanup while preserving cancellation of the queue worker.

    Awaiting a child task directly makes it easy to accidentally consume the
    queue worker's ``CancelledError``. ``asyncio.wait`` lets us distinguish a
    child that was already cancelled from cancellation of this caller.
    """

    if task is None:
        return True
    if cancel_first:
        task.cancel()
    deadline = (
        INTERACTIVE_FEEDBACK_CLEANUP_TIMEOUT_SECONDS
        if timeout_s is None
        else max(0.0, float(timeout_s))
    )
    try:
        done, _pending = await asyncio.wait({task}, timeout=deadline)
    except asyncio.CancelledError:
        task.cancel()
        if not task.done():
            task.add_done_callback(_consume_feedback_task_result)
        raise

    if task not in done:
        task.cancel()
        task.add_done_callback(_consume_feedback_task_result)
        runtime.error_logger.warning(
            f"Interactive feedback cleanup timed out for {label} after {deadline:.1f}s; "
            "continuing without blocking the agent queue."
        )
        return False

    try:
        task.result()
    except asyncio.CancelledError:
        # The child itself was cancelled. Caller cancellation is handled above.
        return True
    except Exception as exc:
        runtime.error_logger.warning(
            f"Interactive feedback cleanup warning for {label}: "
            f"{type(exc).__name__}: {exc}"
        )
        return False
    return True


async def _run_interactive_feedback_cleanup_step(
    runtime,
    awaitable,
    *,
    label: str,
) -> bool:
    task = asyncio.create_task(awaitable, name=f"feedback-cleanup-{label}")
    return await settle_interactive_feedback_task(runtime, task, label=label)


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
    verbose_display_state: VerboseDisplayState | None = None,
) -> None:
    if stop_typing:
        stop_typing.set()
    await settle_interactive_feedback_task(runtime, typing_task, label="typing")
    await settle_interactive_feedback_task(runtime, escalation_task, label="escalation")
    await settle_interactive_feedback_task(
        runtime,
        answer_preview_task,
        label="answer-preview",
    )

    if think_flush_task is not None:
        await settle_interactive_feedback_task(
            runtime,
            think_flush_task,
            label="thinking-flush-loop",
            cancel_first=True,
        )
        await _run_interactive_feedback_cleanup_step(
            runtime,
            runtime._flush_thinking(item.chat_id),
            label="thinking-flush-final",
        )

    active_placeholder = (
        verbose_display_state.current_message
        if verbose_display_state is not None
        else placeholder
    )
    if active_placeholder and delete_placeholder:
        delete_started = time.monotonic()
        deleted = await _run_interactive_feedback_cleanup_step(
            runtime,
            runtime.app.bot.delete_message(
                chat_id=item.chat_id,
                message_id=active_placeholder.message_id,
            ),
            label="placeholder-delete",
        )
        if deleted:
            delete_elapsed_s = max(0.0, time.monotonic() - delete_started)
            runtime.telegram_logger.info(
                f"Deleted placeholder for {item.request_id} "
                f"(elapsed_s={delete_elapsed_s:.2f})"
            )


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


def _transport_accepted(result: Any) -> bool:
    """Normalize supported transport receipts without guessing from truthiness."""

    if result is None:
        return False
    if isinstance(result, bool):
        return result
    if isinstance(result, int):
        return result > 0
    if isinstance(result, str):
        return bool(result.strip())
    if isinstance(result, dict):
        for key in ("accepted", "delivered", "ok"):
            if key in result:
                return result.get(key) is True
        return result.get("message_id") is not None
    if isinstance(result, (tuple, list)):
        if len(result) >= 2 and isinstance(result[1], int):
            return result[1] > 0
        return any(_transport_accepted(item) for item in result)
    for key in ("accepted", "delivered", "ok"):
        accepted = getattr(result, key, None)
        if isinstance(accepted, bool):
            return accepted
    return getattr(result, "message_id", None) is not None


def _transport_message_id(result: Any) -> Any | None:
    if result is None or isinstance(result, bool):
        return None
    if isinstance(result, dict):
        return result.get("message_id")
    message_id = getattr(result, "message_id", None)
    if message_id is not None:
        return message_id
    if isinstance(result, (str, int)):
        return result
    return None


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

    normalized_backend = canonical_backend_engine(backend_name).lower()
    if normalized_backend != "her-v2":
        return presentation_callback

    effort = str(getattr(backend, "effort", "low") or "low").strip().lower()
    runtime.logger.info(
        f"HER acknowledgement policy and message router: request={item.request_id} "
        f"commentary={bool(getattr(runtime, '_commentary', True))} "
        f"verbose={bool(getattr(runtime, '_verbose', False))} "
        f"think={bool(getattr(runtime, '_think', False))} effort={effort} "
        f"delivery_requested={delivery_requested} blocked={delivery_blocked}"
    )
    provisional_messages: dict[str, _ProvisionalTelegramMessage] = {}
    provisional_audio_events: set[str] = set()
    bot = getattr(getattr(runtime, "app", None), "bot", None)
    initial_resolution_capable = bool(
        callable(getattr(runtime, "_send_text", None))
        and callable(getattr(bot, "edit_message_text", None))
    )

    async def _send_event(event, *, purpose: str, commentary: bool = False):
        await runtime_delivery_order.wait_for_turn(runtime, item.request_id)
        from orchestrator.native_audio_delivery import (
            audio_parts,
            native_reply_content_policy,
            send_native_audio_parts,
        )

        typed_content = tuple(
            dict(part)
            for part in dict(getattr(event, "metadata", {}) or {}).get(
                "content", ()
            )
            if isinstance(part, Mapping)
        )
        has_audio = bool(audio_parts(typed_content))
        reply_policy = native_reply_content_policy(runtime, item)
        audio_task = None
        if has_audio and reply_policy != "text_only":
            audio_task = asyncio.create_task(
                send_native_audio_parts(
                    runtime,
                    item,
                    typed_content,
                    purpose=purpose,
                )
            )
        raw_text = str(getattr(event, "summary", "") or "").strip()
        if has_audio and reply_policy == "audio_only":
            raw_text = ""
        if not raw_text and audio_task is None:
            return False
        if not raw_text:
            try:
                audio_accepted = bool(await audio_task)
            except Exception as exc:
                runtime.logger.warning(
                    "HER native audio delivery failed: request=%s error_type=%s",
                    item.request_id,
                    type(exc).__name__,
                )
                return False
            event_id = str(getattr(event, "event_id", "") or "").strip()
            if audio_accepted and event_id:
                provisional_audio_events.add(event_id)
            return audio_accepted
        text = raw_text
        if commentary and not has_audio and not text.startswith("💬"):
            text = f"💬 {text}"
        rendered_text = _md_to_html(text)
        runtime.logger.info(
            f"HER message delivery started: request={item.request_id} "
            f"purpose={purpose} text_len={len(text)}"
        )
        try:
            if hasattr(runtime, "_send_text") and len(rendered_text) <= 3_500:
                result = await runtime._send_text(
                    item.chat_id,
                    rendered_text,
                    parse_mode="HTML",
                    _request_id=item.request_id,
                    _purpose=purpose,
                )
                transport_accepted = _transport_accepted(result)
            else:
                result = await runtime.send_long_message(
                    item.chat_id,
                    text,
                    request_id=item.request_id,
                    purpose=purpose,
                )
                transport_accepted = _transport_accepted(result)
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
        audio_accepted = False
        if audio_task is not None:
            try:
                audio_accepted = bool(await audio_task)
            except Exception as exc:
                runtime.logger.warning(
                    "HER native audio delivery failed: request=%s error_type=%s",
                    item.request_id,
                    type(exc).__name__,
                )
        if not transport_accepted and not audio_accepted:
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
        event_id = str(getattr(event, "event_id", "") or "").strip()
        if audio_accepted and event_id:
            provisional_audio_events.add(event_id)
        message_id = _transport_message_id(result)
        if event_id and message_id is not None:
            provisional_messages[event_id] = _ProvisionalTelegramMessage(
                message_id=message_id,
                rendered_text=rendered_text,
                parse_mode="HTML",
                reply_markup=None,
            )
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
        return bool(transport_accepted or audio_accepted)

    async def _commentary_presenter(event):
        if getattr(event, "kind", None) == "voice_warning":
            return await _send_event(
                event, purpose="native_audio_fallback_warning", commentary=False
            )
        purpose = (
            "task_acknowledgement"
            if getattr(event, "kind", None) == KIND_ACKNOWLEDGEMENT
            else "task_commentary"
        )
        return await _send_event(event, purpose=purpose, commentary=True)

    async def _control_presenter(event):
        return await _send_event(event, purpose="her_control")

    async def _initial_resolution_presenter(event):
        target_event_id = str(
            getattr(event, "target_event_id", "") or ""
        ).strip()
        provisional = provisional_messages.get(target_event_id)
        if provisional is None:
            # A voice message is immutable on Telegram.  Resolution changes
            # only the internal disposition; the already-delivered audio stays.
            return target_event_id in provisional_audio_events
        resolution = str(getattr(event, "resolution", "") or "").strip()
        rendered_text = provisional.rendered_text
        parse_mode = provisional.parse_mode
        reply_markup = provisional.reply_markup
        try:
            if resolution == "discard":
                delete_message = getattr(runtime.app.bot, "delete_message", None)
                if not callable(delete_message):
                    return False
                await delete_message(
                    chat_id=item.chat_id,
                    message_id=provisional.message_id,
                )
                provisional_messages.pop(target_event_id, None)
                return True
            raw_text = str(getattr(event, "summary", "") or "").strip()
            if not raw_text:
                return False
            if resolution == "commentary" and not raw_text.startswith("💬"):
                raw_text = f"💬 {raw_text}"
            rendered_text = _md_to_html(raw_text)
            parse_mode = "HTML"
            reply_markup = None
            if (
                provisional.rendered_text == rendered_text
                and provisional.parse_mode == parse_mode
                and provisional.reply_markup == reply_markup
            ):
                debug = getattr(runtime.logger, "debug", None)
                if callable(debug):
                    debug(
                        f"HER initial resolution already current: "
                        f"request={item.request_id} "
                        f"target_event_id={target_event_id} "
                        f"resolution={resolution}"
                    )
                if resolution in {"final", "clarification"}:
                    provisional_messages.pop(target_event_id, None)
                return True
            await runtime.app.bot.edit_message_text(
                chat_id=item.chat_id,
                message_id=provisional.message_id,
                text=rendered_text,
                parse_mode=parse_mode,
            )
            if resolution in {"final", "clarification"}:
                provisional_messages.pop(target_event_id, None)
            else:
                provisional.rendered_text = rendered_text
                provisional.parse_mode = parse_mode
                provisional.reply_markup = reply_markup
            return True
        except Exception as exc:
            if resolution != "discard" and _is_message_not_modified_error(exc):
                debug = getattr(runtime.logger, "debug", None)
                if callable(debug):
                    debug(
                        f"HER initial resolution reached Telegram idempotently: "
                        f"request={item.request_id} "
                        f"target_event_id={target_event_id} "
                        f"resolution={resolution}"
                    )
                if resolution in {"final", "clarification"}:
                    provisional_messages.pop(target_event_id, None)
                else:
                    provisional.rendered_text = rendered_text
                    provisional.parse_mode = parse_mode
                    provisional.reply_markup = reply_markup
                return True
            runtime.logger.warning(
                f"HER initial resolution failed: request={item.request_id} "
                f"target_event_id={target_event_id} "
                f"error_type={type(exc).__name__} "
                f"error={_safe_excerpt(str(exc), 500)}"
            )
            return False

    async def _persist_event(event):
        event_suppression_reason = _her_event_suppression_reason(event)
        _append_her_message_audit(
            runtime,
            item,
            event,
            status="generated",
            include_text=True,
        )
        content = tuple(
            dict(part)
            for part in dict(getattr(event, "metadata", {}) or {}).get(
                "content", ()
            )
            if isinstance(part, Mapping)
        )
        append_audio_event = getattr(
            getattr(runtime, "session_store", None),
            "append_native_audio_runtime_event",
            None,
        )
        if callable(append_audio_event):
            try:
                append_audio_event(
                    request_id=item.request_id,
                    source_event_id=str(getattr(event, "event_id", "") or ""),
                    event_kind=str(getattr(event, "kind", "") or ""),
                    summary=str(getattr(event, "summary", "") or ""),
                    phase=str(getattr(event, "phase", "") or ""),
                    content=content,
                    resolution=str(getattr(event, "resolution", "") or ""),
                    target_event_id=str(
                        getattr(event, "target_event_id", "") or ""
                    ),
                )
            except Exception as exc:
                runtime.logger.warning(
                    "Session native audio Event projection failed safely: "
                    "request=%s error_type=%s",
                    item.request_id,
                    type(exc).__name__,
                )
        delivery_class = str(getattr(event, "delivery_class", "") or "")
        suppression_reason = event_suppression_reason
        if not suppression_reason and delivery_class == "user_commentary":
            if not delivery_requested:
                suppression_reason = "delivery_not_requested"
            elif delivery_blocked:
                suppression_reason = "delivery_blocked"
            elif (
                not bool(getattr(event, "required", False))
                and not bool(getattr(runtime, "_commentary", True))
            ):
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
                status=(
                    "superseded"
                    if suppression_reason == "superseded_by_final"
                    else "suppressed"
                ),
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
        initial_resolution_presenter=(
            _initial_resolution_presenter if initial_resolution_capable else None
        ),
        verbose_enabled=lambda: bool(getattr(runtime, "_verbose", False)),
        think_enabled=lambda: bool(getattr(runtime, "_think", False)),
        commentary_enabled=lambda: bool(getattr(runtime, "_commentary", True)),
        persist_event=_persist_event,
        delivery_requested=delivery_requested,
        delivery_blocked=delivery_blocked,
    )

    async def callback(event):
        return await router.route(event)

    setattr(callback, "her_message_router", router)
    setattr(
        callback,
        "supports_initial_resolution",
        router.supports_initial_resolution,
    )
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
    verbose_display_state = None
    delivery_requested = not item.silent and item.deliver_to_telegram
    display_policy = telegram_stream_policy.get_display_policy(runtime)
    delivery_blocked = telegram_delivery_failover.is_delivery_blocked(runtime)
    typing_delivery_enabled = (
        delivery_requested and display_policy.typing_enabled and not delivery_blocked
    )
    verbose_delivery_enabled = delivery_requested and runtime._verbose and not delivery_blocked
    think_delivery_enabled = delivery_requested and runtime._think and not delivery_blocked
    backend = runtime.backend_manager.current_backend
    normalized_backend = canonical_backend_engine(
        getattr(runtime.config, "active_backend", "")
    ).lower()
    is_her_backend = normalized_backend == "her-v2"

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
                    disable_notification=telegram_notifications.disable_notification(
                        runtime, purpose="placeholder"
                    ),
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
            verbose_display_state = VerboseDisplayState(
                current_message=placeholder,
                message_ids=[placeholder.message_id],
            )
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
                    display_state=verbose_display_state,
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
        downstream_callback = stream_callback

        async def _activity_callback(event):
            activity_store.publish_stream(item.request_id, event)
            if downstream_callback is not None:
                result = downstream_callback(event)
                if inspect.isawaitable(result):
                    await result

        stream_callback = _activity_callback

    presentation_callback = stream_callback

    async def _canonical_stream_callback(event):
        terminal_console.record_stream_event(runtime.name, item.request_id, event)
        payload = dict(vars(event)) if hasattr(event, "__dict__") else {"repr": repr(event)}
        _canonical_record(
            runtime,
            "provider_stream_event",
            payload,
            request_id=item.request_id,
            provenance={
                "source": str(getattr(event, "origin", "") or "active_backend"),
                "provider_provenance": str(getattr(event, "provenance", "") or ""),
            },
        )
        if str(getattr(event, "kind", "") or "") == "thinking":
            seen = getattr(runtime, "_canonical_reasoning_seen", None)
            if not isinstance(seen, set):
                seen = set()
                runtime._canonical_reasoning_seen = seen
            seen.add(item.request_id)
            _canonical_record(
                runtime,
                "provider_reasoning",
                {
                    "availability": "available",
                    "raw_delta": str(getattr(event, "raw_delta", "") or ""),
                    "summary": str(getattr(event, "summary", "") or ""),
                    "detail": str(getattr(event, "detail", "") or ""),
                },
                request_id=item.request_id,
                provenance={
                    "source": str(getattr(event, "origin", "") or "active_backend"),
                    "provider_provenance": str(getattr(event, "provenance", "") or ""),
                    "fabricated": False,
                },
            )
        if presentation_callback is not None:
            result = presentation_callback(event)
            if inspect.isawaitable(result):
                return await result
            return result
        return None

    # The canonical collector is a transparent transport wrapper.  HER and
    # other backends may attach capability metadata to their callback (for
    # example ``supports_initial_resolution``); losing those attributes while
    # adding audit collection changes provider behaviour.  Preserve arbitrary
    # callback metadata as well as the known protocol attributes while keeping
    # the unwrapped presentation callback separately observable on
    # ``InteractiveFeedback.stream_callback``.
    if presentation_callback is not None:
        try:
            callback_metadata = vars(presentation_callback)
        except TypeError:
            callback_metadata = {}
        for attribute, value in callback_metadata.items():
            setattr(_canonical_stream_callback, attribute, value)
        for attribute in ("supports_initial_resolution", "her_message_router"):
            if hasattr(presentation_callback, attribute):
                setattr(
                    _canonical_stream_callback,
                    attribute,
                    getattr(presentation_callback, attribute),
                )
    setattr(_canonical_stream_callback, "presentation_callback", presentation_callback)

    # Canonical evidence collection is independent of Telegram visibility and
    # the sanitised operational audit toggle.
    on_stream_event = _canonical_stream_callback
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
        verbose_display_state=verbose_display_state,
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
            **request_context_warning_fields(runtime, item.request_id),
        },
    )


async def prepare_successful_response(runtime, item, response, *, completion_path: str) -> SuccessfulResponse:
    observe_terminal_response(runtime, item, response)
    if item.source == "bridge:hchat-draft" and hasattr(runtime, "_prepare_hchat_draft_success"):
        return await runtime._prepare_hchat_draft_success(
            item,
            core_raw=response.text,
            completion_path=completion_path,
        )
    from orchestrator.native_audio_delivery import audio_parts, claim_audio_parts

    claim_audio_parts(runtime, item, getattr(response, "content", ()))
    display_text = runtime._strip_transfer_accept_prefix(item, response.text)
    visible_text, wrapper_result = await runtime._apply_wrapper_to_visible_text(
        item,
        display_text or response.text,
    )
    if _contains_dangling_tool_markup(response.text) or _contains_dangling_tool_markup(
        visible_text
    ):
        fallback = _safe_blocked_tool_markup_final(runtime, item)
        runtime.logger.error(
            f"Blocked dangling tool markup at the final delivery boundary: request={item.request_id}"
        )
        response.text = fallback
        response.stop_reason = "no_final_text"
        metadata = getattr(response, "stream_metadata", None)
        metadata = dict(metadata) if isinstance(metadata, dict) else {}
        metadata.update(
            {
                "completion_status": "incomplete",
                "completion_stop_reason": "no_final_text",
                "dangling_tool_markup_blocked": True,
            }
        )
        response.stream_metadata = metadata
        display_text = fallback
        visible_text = fallback
    has_typed_audio = bool(audio_parts(getattr(response, "content", ())))
    if not visible_text.strip() and not has_typed_audio:
        return SuccessfulResponse(
            display_text=display_text,
            visible_text=visible_text,
            wrapper_result=wrapper_result,
        )
    runtime._mark_success()
    safe_core_raw = extract_memory_plus_update_details(response.text).visible_text
    if visible_text.strip():
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
            "content": [
                dict(part)
                for part in getattr(response, "content", ())
                if isinstance(part, Mapping)
            ],
            **request_context_warning_fields(runtime, item.request_id),
            **runtime._wrapper_listener_fields(safe_core_raw, visible_text, wrapper_result),
        },
    )
    return SuccessfulResponse(
        display_text=display_text,
        visible_text=visible_text,
        wrapper_result=wrapper_result,
    )


def _meter_line_items_from_response(response):
    """Extract per-stage HER v2 line items from a response's stream metadata."""
    metadata = getattr(response, "stream_metadata", None) or {}
    meter = metadata.get("meter") if isinstance(metadata, dict) else None
    if not isinstance(meter, dict):
        return None
    raw_items = meter.get("line_items")
    if not isinstance(raw_items, list) or not raw_items:
        return None
    try:
        from tools.meter_cost import line_item_from_dict

        return [
            line_item_from_dict(item)
            for item in raw_items
            if isinstance(item, dict)
        ]
    except Exception:
        return None


def remember_meter_receipt(runtime, request_id: str, receipt) -> None:
    """Keep one bounded receipt per request for foreground and async tails."""

    registry = getattr(runtime, "_meter_receipt_by_id", None)
    if not isinstance(registry, dict):
        registry = {}
        runtime._meter_receipt_by_id = registry
    registry[str(request_id or "")] = receipt
    while len(registry) > 512:
        registry.pop(next(iter(registry)), None)


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
        from tools.token_tracker import (
            estimate_tokens,
            record_audit_event,
            record_usage,
        )

        meter_line_items = _meter_line_items_from_response(response)
        if response.usage:
            input_tokens = response.usage.input_tokens
            output_tokens = response.usage.output_tokens
            thinking_tokens = response.usage.thinking_tokens
            token_source = "api"
            receipt = record_usage(
                runtime.workspace_dir,
                model=runtime.get_current_model(),
                backend=runtime.config.active_backend,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                thinking_tokens=thinking_tokens,
                session_id=runtime.session_id_dt,
                cost_usd=getattr(response, "cost_usd", None),
                request_id=item.request_id,
                phase="foreground",
                engine=runtime.config.active_backend,
                line_items=meter_line_items,
                token_source="provider",
            )
        else:
            input_tokens = estimate_tokens(final_prompt)
            output_tokens = estimate_tokens(visible_text)
            thinking_tokens = runtime._thinking_chars_this_req // 4
            token_source = "estimated"
            receipt = record_usage(
                runtime.workspace_dir,
                model=runtime.get_current_model(),
                backend=runtime.config.active_backend,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                thinking_tokens=thinking_tokens,
                session_id=runtime.session_id_dt,
                request_id=item.request_id,
                phase="foreground",
                engine=runtime.config.active_backend,
                line_items=meter_line_items,
            )
        remember_meter_receipt(runtime, item.request_id, receipt)
        prompt_audit = runtime._last_prompt_audit
        section_chars = {s["key"]: s["chars"] for s in prompt_audit.get("sections", [])}
        section_tokens = {
            s["key"]: s.get("tokens_est") or max(1, s["chars"] // 4)
            for s in prompt_audit.get("sections", [])
        }
        section_counts = {s["key"]: s.get("item_count", 0) for s in prompt_audit.get("sections", [])}
        stream_metadata = getattr(response, "stream_metadata", None) or {}
        thinking_metadata = stream_metadata.get("thinking") or {}
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
                **skill_usage_audit_fields(item),
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
                "thinking_chars": int(thinking_metadata.get("thinking_chars") or 0),
                "thinking_event_count": int(thinking_metadata.get("thinking_event_count") or 0),
                "thinking_redacted_count": int(thinking_metadata.get("thinking_redacted_count") or 0),
                "thinking_sources": list(thinking_metadata.get("thinking_sources") or []),
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
    _canonical_record(
        runtime,
        "chat_exchange",
        {
            "user": item.prompt,
            "assistant_provider_text": response.text,
            "assistant_delivered_text": visible_text,
            "source": item.source,
            "session_id": getattr(item, "session_id", None),
            "run_id": getattr(item, "run_id", None),
            "message_id": getattr(item, "message_id", None),
            "response": dict(vars(response)) if hasattr(response, "__dict__") else repr(response),
        },
        request_id=item.request_id,
        provenance={"source": "completed_external_exchange"},
    )
    memory_user_text = item.prompt
    if item.source.lower() in {"document", "photo", "voice", "audio", "video", "sticker", "multimodal"}:
        memory_user_text = f"[{item.source}] {item.summary}"
    if item.source not in {"startup", "system", session_reset_source} and not is_bridge_request:
        memory_assistant_text = runtime._core_memory_assistant_text(
            response.text,
            visible_text,
            wrapper_result,
        )
        # Ordinary chat is written only to the current Session working store.
        # Agent-level episodic memory is populated later by /promote or the
        # configured daily promotion job.
        runtime_session.record_working_exchange(
            runtime,
            item,
            user_text=memory_user_text,
            assistant_text=memory_assistant_text,
            assistant_source=runtime.config.active_backend,
        )
        runtime._schedule_post_turn_observers(
            item,
            memory_user_text,
            memory_assistant_text,
            is_bridge_request=is_bridge_request,
        )
        try:
            from orchestrator.context_compaction import (
                estimate_tokens,
                schedule_post_turn,
            )

            request_tokens = getattr(runtime, "_context_compaction_prompt_tokens", {})
            prompt_tokens = int(request_tokens.get(item.request_id) or 0)
            if prompt_tokens > 0:
                schedule_post_turn(
                    runtime,
                    request_ref=item.request_id,
                    prompt_tokens=(
                        prompt_tokens + estimate_tokens(memory_assistant_text)
                    ),
                    chat_id=item.chat_id,
                    deliver_to_telegram=bool(item.deliver_to_telegram),
                )
        except Exception as exc:
            runtime.logger.warning(
                "Post-turn context compaction scheduling failed safely for %s: %s: %s",
                item.request_id,
                type(exc).__name__,
                exc,
            )
        finally:
            request_tokens = getattr(runtime, "_context_compaction_prompt_tokens", None)
            if isinstance(request_tokens, dict):
                request_tokens.pop(item.request_id, None)
    if not is_bridge_request:
        handoff_builder = runtime_session.session_handoff_builder(runtime, item=item)
        handoff_builder.append_transcript("user", item.prompt, item.source)
        handoff_builder.append_transcript("assistant", visible_text, item.source)
        handoff_builder.refresh_recent_context()
        runtime.project_chat_logger.log_exchange(item.prompt, visible_text, item.source)


def clear_context_compaction_request_state(runtime, request_id: str) -> None:
    request_tokens = getattr(runtime, "_context_compaction_prompt_tokens", None)
    if isinstance(request_tokens, dict):
        request_tokens.pop(str(request_id), None)
    prompt_states = getattr(runtime, "_context_compaction_prompt_states", None)
    if isinstance(prompt_states, dict):
        prompt_states.pop(str(request_id), None)
    execution_requests = getattr(
        runtime,
        "_context_compaction_execution_requests",
        None,
    )
    if isinstance(execution_requests, set):
        execution_requests.discard(str(request_id))


def backend_failure_fields(response: Any) -> dict[str, Any]:
    """Expose typed provider failure metadata without changing error text."""

    fields: dict[str, Any] = {}
    for name in (
        "error_code",
        "error_retryable",
        "http_status",
        "provider_request_id",
        "retry_after_s",
    ):
        value = getattr(response, name, None)
        if value is not None and value != "":
            fields[name] = value
    tool_call_count = int(getattr(response, "tool_call_count", 0) or 0)
    if tool_call_count:
        fields["tool_call_count"] = tool_call_count
    if bool(getattr(response, "side_effects_possible", False)):
        fields["side_effects_possible"] = True
    return fields


def _typed_capacity_recovery_is_safe(response: Any) -> bool:
    if str(getattr(response, "error_code", "") or "") != "CONTEXT_CAPACITY_REJECTED":
        return False
    if bool(getattr(response, "side_effects_possible", False)):
        return False
    if int(getattr(response, "tool_call_count", 0) or 0) > 0:
        return False
    if int(getattr(response, "tool_loop_count", 0) or 0) > 0:
        return False
    metadata = getattr(response, "stream_metadata", None)
    her = metadata.get("her_v2") if isinstance(metadata, dict) else None
    if not isinstance(her, dict):
        return True
    failure_chain = her.get("failure_chain")
    primary = (
        failure_chain.get("primary_failure")
        if isinstance(failure_chain, dict)
        else None
    )
    if isinstance(primary, dict) and bool(primary.get("side_effects_possible")):
        return False
    delivery = her.get("delivery")
    if isinstance(delivery, dict) and str(delivery.get("delivery_id") or "").strip():
        return False
    return True


async def recover_typed_context_capacity_rejection(
    runtime,
    item,
    response,
    *,
    on_stream_event=None,
) -> tuple[Any, str] | None:
    """Compact and retry once only when a typed rejection proved side-effect free."""

    if not _typed_capacity_recovery_is_safe(response):
        return None
    states = getattr(runtime, "_context_compaction_prompt_states", None)
    state = states.get(item.request_id) if isinstance(states, dict) else None
    if not isinstance(state, dict) or state.get("capacity_recovery_attempted"):
        return None
    state["capacity_recovery_attempted"] = True
    if not state.get("inject_memory") or state.get("is_bridge_request"):
        return None

    from orchestrator.context_compaction import (
        coordinator_for,
        estimate_target_overhead_tokens,
        estimate_tokens,
        install_history_section,
        resolve_target_capacity,
    )

    session_workspace_value = str(state.get("session_workspace") or "").strip()
    session_workspace = Path(session_workspace_value) if session_workspace_value else None
    working_memory_store = runtime_session.session_memory_store(runtime, item)
    coordinator = coordinator_for(
        runtime,
        request_ref=item.request_id,
        workspace_dir=session_workspace,
        memory_store=working_memory_store,
    )
    outcome = await coordinator.compact(
        trigger="typed_provider_capacity_rejection",
        request_ref=item.request_id,
        force=True,
    )
    if not outcome.changed:
        runtime.logger.warning(
            "Typed context-capacity recovery could not compact request=%s status=%s code=%s",
            item.request_id,
            outcome.status,
            outcome.code,
        )
        return None

    base_sections = list(state.get("base_extra_sections") or [])
    sections, _snapshot = install_history_section(
        runtime,
        base_sections,
        cross_session_entries=list(
            state.get("cross_session_timeline_entries") or []
        ),
        primary_timeline_entries=state.get("primary_timeline_entries"),
        workspace_dir=session_workspace,
        memory_store=working_memory_store,
    )
    builder = runtime.context_assembler.build_prompt_payload
    kwargs = {
        "extra_sections": sections,
        "inject_memory": True,
        "incremental": False,
    }
    if "context_profile" in inspect.signature(builder).parameters:
        kwargs["context_profile"] = state.get("context_profile")
    if (
        state.get("primary_timeline_entries") is not None
        and "recent_exchanges" in inspect.signature(builder).parameters
    ):
        kwargs["recent_exchanges"] = list(
            state.get("primary_timeline_entries") or []
        )
    payload = builder(
        str(state.get("effective_prompt") or item.prompt),
        runtime.config.active_backend,
        **kwargs,
    )
    retry_prompt = str(payload["final_prompt"])
    runtime._last_prompt_audit = dict(payload.get("audit") or {})
    retry_tokens = estimate_tokens(retry_prompt)
    previous_tokens = int(state.get("prompt_tokens") or estimate_tokens(state.get("final_prompt") or ""))
    if retry_tokens >= previous_tokens:
        runtime.logger.warning(
            "Typed context-capacity recovery aborted because effective prompt did not shrink: "
            "request=%s before=%s after=%s",
            item.request_id,
            previous_tokens,
            retry_tokens,
        )
        return None
    target = resolve_target_capacity(runtime)
    target_overhead_tokens = estimate_target_overhead_tokens(runtime)
    if (
        target is not None
        and retry_tokens
        + target_overhead_tokens
        + target.response_headroom_tokens
        > target.context_window_tokens
    ):
        runtime.logger.warning(
            "Typed context-capacity recovery remains above declared capacity: "
            "request=%s tokens=%s capacity=%s",
            item.request_id,
            retry_tokens,
            target.context_window_tokens,
        )
        return None

    runtime.logger.warning(
        "Retrying one side-effect-free HER v2 request after typed capacity recovery: "
        "request=%s compaction=%s before=%s after=%s",
        item.request_id,
        outcome.compaction_id,
        previous_tokens,
        retry_tokens,
    )
    _canonical_record(
        runtime,
        "provider_request",
        {
            "engine": runtime.config.active_backend,
            "effective_user_prompt": str(state.get("effective_prompt") or item.prompt),
            "final_prompt": retry_prompt,
            "pcm_envelope": payload.get("envelope"),
            "prompt_audit": payload.get("audit"),
            "incremental": False,
            "retry": {
                "kind": "typed_context_capacity_recovery",
                "compaction_id": outcome.compaction_id,
                "previous_tokens": previous_tokens,
                "retry_tokens": retry_tokens,
            },
        },
        request_id=item.request_id,
        provenance={"source": "hashi_pcm_assembler", "retry": True},
    )
    runtime.is_generating = True
    try:
        retry_response = await runtime.backend_manager.generate_response(
            retry_prompt,
            item.request_id,
            is_retry=True,
            silent=item.silent,
            on_stream_event=on_stream_event,
        )
    finally:
        runtime.is_generating = False
    state["final_prompt"] = retry_prompt
    state["prompt_tokens"] = retry_tokens
    request_tokens = getattr(runtime, "_context_compaction_prompt_tokens", None)
    if isinstance(request_tokens, dict):
        request_tokens[item.request_id] = retry_tokens
    return retry_response, retry_prompt


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
    observe_terminal_response(runtime, item, response)
    err_msg = response.error or "Unknown error"
    failure_fields = backend_failure_fields(response)
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
                **request_context_warning_fields(runtime, item.request_id),
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
            **failure_fields,
            **request_context_warning_fields(runtime, item.request_id),
        },
    )
    # Silent suppresses routine success chatter, never a concrete terminal
    # failure for a request that has a user delivery target. Provider/model
    # failures must not disappear merely because the originating job was
    # scheduled or otherwise marked silent.
    if item.silent and not item.deliver_to_telegram:
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
        "Flex Backend error for %s (%s, source=%s, code=%s, retryable=%s, "
        "status=%s, provider_request_id=%s, side_effects=%s): %s",
        item.request_id,
        runtime.config.active_backend,
        item.source,
        failure_fields.get("error_code") or "untyped",
        failure_fields.get("error_retryable"),
        failure_fields.get("http_status"),
        failure_fields.get("provider_request_id") or "none",
        failure_fields.get("side_effects_possible", False),
        err_msg,
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
        await record_her_v2_transport_receipt(
            runtime,
            item,
            response,
            delivered=False,
            disposition="buffered_during_transfer",
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
        await record_her_v2_transport_receipt(
            runtime,
            item,
            response,
            delivered=False,
            disposition="telegram_delivery_not_requested",
        )
        return

    response_text = visible_text
    from orchestrator.native_audio_delivery import (
        audio_parts,
        native_reply_content_policy,
        send_native_audio_parts,
    )

    native_parts = audio_parts(getattr(response, "content", ()))
    native_policy = native_reply_content_policy(runtime, item)
    cos_handled = False
    safe_core_raw = extract_memory_plus_update_details(response.text).visible_text
    await runtime._send_wrapper_verbose_trace(item, safe_core_raw, visible_text, wrapper_result)
    if (
        runtime._cos_enabled
        and runtime.name != "lily"
        and not item.source.startswith("cos-query:")
        and response_text
        and not native_parts
        and response_text.rstrip().endswith(("?", "？"))
    ):
        cos_result = await runtime.cos_query(response_text)
        if cos_result.get("answered") and cos_result.get("response"):
            response_text = cos_result["response"]
        else:
            cos_handled = True
    _print_final_response(runtime.name, response_text)
    her_delivery = _her_v2_delivery_metadata(response)
    delivered_at_initial_resolution = bool(
        her_delivery.get("final_already_delivered")
    )
    native_delivery_task = None
    native_delivery_attempted = bool(
        native_parts
        and native_policy != "text_only"
        and not delivered_at_initial_resolution
    )
    if native_delivery_attempted:
        native_delivery_task = asyncio.create_task(
            send_native_audio_parts(
                runtime,
                item,
                getattr(response, "content", ()),
                purpose="native_audio_final",
            )
        )
    delivery_text = "" if native_policy == "audio_only" and native_parts else response_text
    receipt_disposition = "transport_returned_no_receipt"
    try:
        if delivered_at_initial_resolution:
            stream_finalization = StreamFinalization(
                streamed=False,
                final_delivered=True,
                fallback_required=False,
            )
            send_elapsed_s, chunk_count = 0.0, 0
            receipt_disposition = "initial_resolution_delivered"
        else:
            if delivery_text:
                stream_finalization = await finalize_streamed_answer(
                    runtime,
                    item,
                    stream_state=answer_stream_state,
                    final_text=delivery_text,
                )
            else:
                stream_finalization = StreamFinalization(
                    streamed=False,
                    final_delivered=False,
                    fallback_required=False,
                )
            if stream_finalization.final_delivered:
                send_elapsed_s = 0.0
                chunk_count = 1 + stream_finalization.continuation_chunks_sent
                receipt_disposition = "stream_final_promoted"
            elif stream_finalization.fallback_required:
                send_elapsed_s, chunk_count = await runtime.send_long_message(
                    chat_id=item.chat_id,
                    text=delivery_text,
                    request_id=item.request_id,
                    purpose="response",
                )
                receipt_disposition = (
                    "transport_delivered"
                    if chunk_count > 0
                    else "transport_returned_no_receipt"
                )
            else:
                send_elapsed_s, chunk_count = 0.0, 0
                receipt_disposition = (
                    "stream_finalization_failed"
                    if stream_finalization.error
                    else "transport_not_attempted"
                )
    except Exception as exc:
        if native_delivery_task is not None:
            native_delivery_task.cancel()
            await asyncio.gather(native_delivery_task, return_exceptions=True)
        await record_her_v2_transport_receipt(
            runtime,
            item,
            response,
            delivered=False,
            disposition="transport_exception",
            error_type=type(exc).__name__,
        )
        raise
    native_delivered = False
    native_delivery_error = None
    if native_delivery_task is not None:
        try:
            native_delivered = bool(await native_delivery_task)
        except Exception as exc:
            native_delivery_error = exc
            runtime.logger.warning(
                "Native audio final delivery failed: request=%s error_type=%s",
                item.request_id,
                type(exc).__name__,
            )
    if native_delivery_attempted and not native_delivered:
        warning_text = (
            "Native audio delivery was unavailable; HASHI is using the local "
            "text-to-speech fallback."
        )
        try:
            await runtime._send_text(
                item.chat_id,
                warning_text,
                _request_id=item.request_id,
                _purpose="native_audio_fallback_warning",
            )
        except Exception:
            await runtime.send_long_message(
                item.chat_id,
                warning_text,
                request_id=item.request_id,
                purpose="native_audio_fallback_warning",
            )
        if response_text:
            native_delivered = bool(
                await runtime._send_voice_reply(
                    item.chat_id,
                    response_text,
                    item.request_id,
                    force=True,
                )
            )
        receipt_disposition = (
            "native_audio_fallback_delivered"
            if native_delivered
            else "native_audio_fallback_failed"
        )
    final_delivered = bool(
        delivered_at_initial_resolution
        or stream_finalization.final_delivered
        or (stream_finalization.fallback_required and chunk_count > 0)
        or native_delivered
    )
    await record_her_v2_transport_receipt(
        runtime,
        item,
        response,
        delivered=final_delivered,
        disposition=receipt_disposition,
        chunk_count=chunk_count + int(native_delivered),
        error_type=(
            type(native_delivery_error).__name__
            if native_delivery_error is not None
            else ""
        ),
    )
    runtime_cross_session.record_turn_result(
        runtime,
        item,
        assistant_text=response_text,
        response=response,
        delivered=final_delivered,
        completion_path="foreground",
    )
    if not native_parts:
        request_metadata = getattr(item, "request_metadata", None)
        voice_origin = bool(
            isinstance(request_metadata, Mapping)
            and request_metadata.get("voice_origin")
        )
        raw_response_metadata = getattr(response, "stream_metadata", None)
        response_metadata = (
            raw_response_metadata
            if isinstance(raw_response_metadata, Mapping)
            else {}
        )
        native_fallback = bool(response_metadata.get("native_audio_fallback"))
        manager = getattr(runtime, "voice_manager", None)
        native_enabled = getattr(manager, "native_audio_enabled", None)
        force_voice = bool(
            native_fallback
            or (
                voice_origin
                and callable(native_enabled)
                and native_enabled()
            )
        )
        if force_voice:
            await runtime._send_voice_reply(
                item.chat_id,
                response_text,
                item.request_id,
                force=True,
            )
        else:
            await runtime._send_voice_reply(
                item.chat_id, response_text, item.request_id
            )
    if final_delivered and callable(getattr(runtime, "_send_meter_cost_tail", None)):
        await runtime._send_meter_cost_tail(item)
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
