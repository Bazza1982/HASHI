from __future__ import annotations

import asyncio
import time
from typing import Any

from orchestrator import (
    runtime_background_status,
    runtime_delivery_order,
    runtime_pipeline,
)
from orchestrator.audit_mode import AuditTelemetryCollector, should_audit_source
from orchestrator.memory_plus_mode import (
    ensure_memory_plus_observer,
    is_memory_plus_enabled,
    migrate_legacy_memory_plus_runtime,
    prepare_memory_plus_store,
)
from orchestrator.wrapper_mode import SESSION_RESET_SOURCE

RUNTIME_TASK_SHUTDOWN_TIMEOUT_SECONDS = 5.0
RUNTIME_SERVICE_SHUTDOWN_TIMEOUT_SECONDS = 5.0
# python-telegram-bot's default getUpdates request timeout is also 5 seconds.
# Updater.stop() performs one final, non-long-polling getUpdates request, so a
# matching outer deadline can cancel an otherwise healthy shutdown at the exact
# moment the request layer is about to return (for example during a transient
# Telegram 502).  Keep enough headroom for that cleanup request to settle while
# remaining inside the per-runtime teardown deadline.
RUNTIME_TELEGRAM_UPDATER_SHUTDOWN_TIMEOUT_SECONDS = 10.0


def _consume_shutdown_task_result(task: asyncio.Future) -> None:
    try:
        task.result()
    except (asyncio.CancelledError, Exception):
        pass


async def _settle_shutdown_task(
    runtime: Any,
    task: asyncio.Future,
    *,
    label: str,
    timeout_s: float,
    cancel_first: bool,
    allow_exception: bool = False,
) -> bool:
    if cancel_first:
        task.cancel()
    try:
        done, _pending = await asyncio.wait({task}, timeout=timeout_s)
    except asyncio.CancelledError:
        task.cancel()
        if not task.done():
            task.add_done_callback(_consume_shutdown_task_result)
        raise
    if task not in done:
        task.cancel()
        task.add_done_callback(_consume_shutdown_task_result)
        runtime.error_logger.warning(
            f"Shutdown step '{label}' timed out after {timeout_s:.1f}s."
        )
        return False
    try:
        task.result()
    except asyncio.CancelledError:
        return True
    except Exception as exc:
        runtime.error_logger.warning(
            f"Shutdown step '{label}' failed: {type(exc).__name__}: {exc}"
        )
        return allow_exception
    return True


async def _run_shutdown_step(
    runtime: Any,
    awaitable,
    *,
    label: str,
    timeout_s: float | None = None,
    allow_exception: bool = False,
) -> bool:
    if timeout_s is None:
        timeout_s = RUNTIME_SERVICE_SHUTDOWN_TIMEOUT_SECONDS
    task = asyncio.create_task(awaitable, name=f"shutdown-{runtime.name}-{label}")
    return await _settle_shutdown_task(
        runtime,
        task,
        label=label,
        timeout_s=timeout_s,
        cancel_first=False,
        allow_exception=allow_exception,
    )


async def initialize(runtime: Any) -> bool:
    runtime.logger.info(f"Initializing flex agent '{runtime.name}'...")
    result = await runtime.backend_manager.initialize_active_backend()
    if result:
        if str(getattr(runtime.config, "active_backend", "")) == "her-v2":
            from orchestrator.context_compaction import ensure_route_state

            if ensure_route_state(runtime):
                runtime.logger.info(
                    "Persisted HER v2 Compact default: active Quick/Light at high effort"
                )
        migrated = migrate_legacy_memory_plus_runtime(runtime)
        if is_memory_plus_enabled(runtime.workspace_dir):
            ensure_memory_plus_observer(runtime.workspace_dir)
            prepare_memory_plus_store(runtime.workspace_dir)
        if migrated:
            runtime.logger.info(
                "Migrated legacy memory+ mode to mode=%s with continuity enabled",
                runtime.backend_manager.agent_mode,
            )
    runtime.reload_post_turn_observers()
    if result:
        backend = runtime.backend_manager.current_backend
        supports_sessions = bool(
            getattr(getattr(backend, "capabilities", None), "supports_sessions", False)
        )
        session_enabled = runtime.backend_manager.agent_mode == "fixed" and supports_sessions
        if hasattr(backend, "set_session_mode"):
            backend.set_session_mode(session_enabled)
        if session_enabled:
            runtime.logger.info(
                f"fixed mode active — session persistence enabled on {runtime.config.active_backend}"
            )
    return result


async def shutdown(runtime: Any) -> None:
    runtime.logger.info(f"Shutting down flex agent '{runtime.name}'...")
    runtime.is_shutting_down = True
    try:
        from orchestrator.context_compaction import cancel_runtime_compaction

        await cancel_runtime_compaction(runtime)
    except Exception as exc:
        runtime.error_logger.warning(
            "Context compaction cancellation warning during shutdown: %s: %s",
            type(exc).__name__,
            exc,
        )
    clean = await _cancel_tasks(
        runtime,
        runtime._scheduled_retry_tasks,
        label="retry-tasks",
    )
    clean = (
        await _cancel_tasks(
            runtime,
            getattr(runtime, "_context_compaction_tasks", set()),
            label="context-compaction-tasks",
        )
        and clean
    )
    long_batch_tasks = {
        task
        for task in (
            getattr(runtime, "_long_buffer_timeout_task", None),
            getattr(runtime, "_long_finalize_task", None),
        )
        if isinstance(task, asyncio.Task)
    }
    clean = (
        await _cancel_tasks(
            runtime,
            long_batch_tasks,
            label="long-batch-tasks",
        )
        and clean
    )
    runtime._long_buffer_timeout_task = None
    runtime._long_finalize_task = None
    clean = (
        await _cancel_tasks(
            runtime,
            getattr(runtime, "_persona_background_status_tasks", set()),
            label="persona-status-tasks",
        )
        and clean
    )
    clean = (
        await _cancel_tasks(
            runtime,
            runtime._background_tasks,
            label="background-tasks",
        )
        and clean
    )
    if runtime.process_task:
        process_stopped = await _settle_shutdown_task(
            runtime,
            runtime.process_task,
            label="queue-processor",
            timeout_s=RUNTIME_TASK_SHUTDOWN_TIMEOUT_SECONDS,
            cancel_first=True,
        )
        clean = process_stopped and clean
        if process_stopped:
            runtime.process_task = None
    clean = (
        await _run_shutdown_step(
            runtime,
            runtime.backend_manager.shutdown(),
            label="backend",
        )
        and clean
    )

    if runtime.startup_success:
        for label, action, timeout_s in (
            (
                "telegram-updater",
                runtime.app.updater.stop,
                RUNTIME_TELEGRAM_UPDATER_SHUTDOWN_TIMEOUT_SECONDS,
            ),
            (
                "telegram-app-stop",
                runtime.app.stop,
                RUNTIME_SERVICE_SHUTDOWN_TIMEOUT_SECONDS,
            ),
            (
                "telegram-app-shutdown",
                runtime.app.shutdown,
                RUNTIME_SERVICE_SHUTDOWN_TIMEOUT_SECONDS,
            ),
        ):
            clean = (
                await _run_shutdown_step(
                    runtime,
                    action(),
                    label=label,
                    timeout_s=timeout_s,
                    allow_exception=True,
                )
                and clean
            )
        if clean:
            runtime.logger.info("Telegram app shut down cleanly.")

    runtime._mark_runtime_shutdown(clean=clean)
    if not clean:
        raise RuntimeError(
            "Runtime shutdown was incomplete; at least one task or service exceeded "
            "its shutdown deadline."
        )


async def process_queue(runtime: Any) -> None:
    runtime.logger.info("Flex queue processor started.")
    while True:
        item = None
        try:
            item = await runtime.queue.get()
            if not item.prompt or not item.prompt.strip():
                runtime.logger.debug(f"Skipping empty prompt in queue (source={item.source}, id={item.request_id})")
                continue
            queue_start = runtime_pipeline.begin_queue_item(runtime, item)
            is_bridge_request = queue_start.is_bridge_request
            queued_at = queue_start.queued_at
            queued_monotonic = queue_start.queued_monotonic
            queue_wait_s = queue_start.queue_wait_s
            remote_backend_block = runtime._remote_backend_block_reason(item.source)
            if remote_backend_block:
                runtime.error_logger.warning(remote_backend_block)
                if item.deliver_to_telegram:
                    await runtime.send_long_message(
                        item.chat_id,
                        f"⚠️ {remote_backend_block}",
                        request_id=item.request_id,
                        purpose="remote-backend-policy",
                    )
                continue
            turn_prompt = await runtime_pipeline.build_turn_prompt(
                runtime,
                item,
                is_bridge_request=is_bridge_request,
            )
            effective_prompt = turn_prompt.effective_prompt
            final_prompt = turn_prompt.final_prompt
            incremental = turn_prompt.incremental
            runtime_pipeline.surface_context_compaction_warnings(
                runtime,
                item,
                turn_prompt.context_warnings,
            )
            runtime._notify_right_brain_started(
                item,
                effective_prompt,
                final_prompt=final_prompt,
                is_bridge_request=is_bridge_request,
            )

            audit_active = runtime._audit_enabled() and should_audit_source(item.source)
            audit_collector = AuditTelemetryCollector() if audit_active else None
            feedback = await runtime_pipeline.setup_interactive_feedback(
                runtime,
                item,
                audit_active=audit_active,
                audit_collector=audit_collector,
            )
            runtime_background_status.prepare(runtime, item)

            generation = await runtime_pipeline.run_backend_generation(
                runtime,
                item,
                final_prompt,
                on_stream_event=feedback.on_stream_event,
                audit_active=audit_active,
            )
            response = generation.response
            backend_started_monotonic = generation.backend_started_monotonic

            if generation.detached:
                if feedback.stop_typing:
                    feedback.stop_typing.set()
                await runtime_pipeline.settle_interactive_feedback_task(
                    runtime,
                    feedback.typing_task,
                    label="typing-detach",
                )
                await runtime_pipeline.settle_interactive_feedback_task(
                    runtime,
                    feedback.escalation_task,
                    label="escalation-detach",
                )
                await runtime_pipeline.settle_interactive_feedback_task(
                    runtime,
                    feedback.answer_preview_task,
                    label="answer-preview-detach",
                )
                setattr(item, "_audit_collector", audit_collector)
                setattr(item, "_her_message_router", feedback.her_message_router)
                status_placeholder = feedback.placeholder
                if feedback.verbose_display_state is not None:
                    status_placeholder = feedback.verbose_display_state.current_message
                if (
                    feedback.answer_stream_state is not None
                    and feedback.answer_stream_state.has_text
                ):
                    # Never overwrite a real streamed answer preview with status.
                    status_placeholder = None
                runtime_background_status.schedule_delivery(
                    runtime,
                    item,
                    generation.generation_task,
                    status_placeholder,
                )
                runtime._register_background_task(generation.generation_task, item)
                runtime.logger.info(
                    f"Detached {item.request_id} to background "
                    f"(threshold={generation.detach_after_s}s, backend={runtime.config.active_backend})"
                )
                runtime._log_maintenance(item, "bg_detached", detach_after_s=generation.detach_after_s)
                continue

            recovered = await runtime_pipeline.recover_typed_context_capacity_rejection(
                runtime,
                item,
                response,
                on_stream_event=feedback.on_stream_event,
            )
            if recovered is not None:
                response, final_prompt = recovered

            backend_elapsed = max(
                0.0, time.monotonic() - backend_started_monotonic
            )
            runtime_pipeline.log_backend_finished(
                runtime,
                item,
                response,
                backend_elapsed_s=backend_elapsed,
                final_prompt=final_prompt,
            )

            await runtime_pipeline.cleanup_interactive_feedback(
                runtime,
                item,
                stop_typing=feedback.stop_typing,
                typing_task=feedback.typing_task,
                escalation_task=feedback.escalation_task,
                answer_preview_task=feedback.answer_preview_task,
                think_flush_task=feedback.think_flush_task,
                placeholder=feedback.placeholder,
                verbose_display_state=feedback.verbose_display_state,
                delete_placeholder=not (
                    response.is_success
                    and bool(response.text)
                    and feedback.answer_stream_state is not None
                ),
            )

            if response.is_success and not response.text:
                runtime._notify_right_brain_interrupted(
                    item,
                    effective_prompt,
                    is_bridge_request=is_bridge_request,
                    reason="empty_success",
                    error="backend returned success with empty text",
                )
                await runtime_pipeline.handle_empty_success_response(runtime, item)
            elif response.is_success and response.text:
                success_result = await runtime_pipeline.prepare_successful_response(
                    runtime,
                    item,
                    completion_path="foreground",
                    response=response,
                )
                visible_text = success_result.visible_text
                wrapper_result = success_result.wrapper_result
                if not visible_text.strip():
                    runtime._notify_right_brain_interrupted(
                        item,
                        effective_prompt,
                        is_bridge_request=is_bridge_request,
                        reason="empty_visible_success",
                        error="backend returned only hidden control content",
                    )
                    await runtime_pipeline.handle_empty_success_response(runtime, item)
                    continue
                runtime._notify_right_brain_completed(
                    item,
                    effective_prompt,
                    visible_text,
                    is_bridge_request=is_bridge_request,
                    completion_path="foreground",
                )
                runtime_pipeline.record_foreground_usage_audit(
                    runtime,
                    item,
                    response,
                    visible_text=visible_text,
                    wrapper_result=wrapper_result,
                    final_prompt=final_prompt,
                    effective_prompt=effective_prompt,
                    incremental=incremental,
                )
                if not item.silent:
                    await runtime_pipeline.handle_success_delivery(
                        runtime,
                        item,
                        response,
                        visible_text=visible_text,
                        wrapper_result=wrapper_result,
                        is_bridge_request=is_bridge_request,
                        session_reset_source=SESSION_RESET_SOURCE,
                        queued_at=queued_at,
                        queue_wait_s=queue_wait_s,
                        backend_elapsed_s=backend_elapsed,
                        audit_collector=audit_collector,
                        answer_stream_state=feedback.answer_stream_state,
                        her_message_router=feedback.her_message_router,
                        queued_monotonic=queued_monotonic,
                    )
            else:
                from orchestrator.runtime_control import consume_user_interrupt

                # /stop, /steer, and /retry already notified with user_* reason; do not
                # re-label the intentional kill as backend_error or show ❌.
                interrupt_reason = consume_user_interrupt(
                    runtime, getattr(item, "request_id", None)
                )
                if not interrupt_reason:
                    runtime._notify_right_brain_interrupted(
                        item,
                        effective_prompt,
                        is_bridge_request=is_bridge_request,
                        reason="backend_error",
                        error=response.error or "Unknown error",
                    )
                await runtime_pipeline.handle_backend_error(
                    runtime,
                    item,
                    response,
                    queued_at=queued_at,
                    queue_wait_s=queue_wait_s,
                    backend_elapsed_s=backend_elapsed,
                    user_interrupt_reason=interrupt_reason,
                    queued_monotonic=queued_monotonic,
                )

        except asyncio.CancelledError:
            break
        except Exception as exc:
            runtime._mark_error(str(exc))
            if item is not None:
                try:
                    is_bridge_request = item.source.startswith("bridge:") or item.source.startswith("bridge-transfer:")
                    runtime._notify_right_brain_interrupted(
                        item,
                        item.prompt,
                        is_bridge_request=is_bridge_request,
                        reason="runtime_exception",
                        error=str(exc),
                    )
                except Exception:
                    pass
            runtime.error_logger.exception(f"Error in flex queue processing: {exc}")
            runtime.is_generating = False
        finally:
            if item is not None:
                background_ids = getattr(runtime, "_background_request_ids", set())
                if item.request_id not in background_ids:
                    registry = getattr(runtime, "_request_meta_by_id", None)
                    if isinstance(registry, dict):
                        registry.pop(item.request_id, None)
                current_meta = getattr(runtime, "current_request_meta", None)
                if isinstance(current_meta, dict) and current_meta.get("request_id") == item.request_id:
                    runtime.current_request_meta = None
                if item.request_id not in background_ids:
                    await runtime_delivery_order.complete_turn(runtime, item.request_id)
                    runtime_pipeline.clear_context_compaction_request_state(
                        runtime,
                        item.request_id,
                    )
                runtime.queue.task_done()
            else:
                runtime.current_request_meta = None


async def _cancel_tasks(
    runtime: Any,
    tasks: set[asyncio.Task],
    *,
    label: str,
) -> bool:
    task_list = [task for task in list(tasks) if not task.done()]
    for task in task_list:
        task.cancel()
    if not task_list:
        return True
    try:
        done, pending = await asyncio.wait(
            task_list,
            timeout=RUNTIME_TASK_SHUTDOWN_TIMEOUT_SECONDS,
        )
    except asyncio.CancelledError:
        for task in task_list:
            task.cancel()
            if not task.done():
                task.add_done_callback(_consume_shutdown_task_result)
        raise
    for task in done:
        _consume_shutdown_task_result(task)
    if not pending:
        return True
    for task in pending:
        task.cancel()
        task.add_done_callback(_consume_shutdown_task_result)
    runtime.error_logger.warning(
        f"Shutdown step '{label}' left {len(pending)} task(s) pending after "
        f"{RUNTIME_TASK_SHUTDOWN_TIMEOUT_SECONDS:.1f}s."
    )
    return False
