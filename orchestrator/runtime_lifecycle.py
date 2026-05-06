from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import datetime
from typing import Any

from orchestrator import runtime_pipeline
from orchestrator.audit_mode import AuditTelemetryCollector, should_audit_source
from orchestrator.wrapper_mode import SESSION_RESET_SOURCE


async def initialize(runtime: Any) -> bool:
    runtime.logger.info(f"Initializing flex agent '{runtime.name}'...")
    result = await runtime.backend_manager.initialize_active_backend()
    runtime.reload_post_turn_observers()
    if result and runtime.backend_manager.agent_mode == "fixed":
        backend = runtime.backend_manager.current_backend
        if hasattr(backend, "set_session_mode"):
            backend.set_session_mode(True)
            runtime.logger.info(f"Fixed mode active — session persistence enabled on {runtime.config.active_backend}")
    return result


async def shutdown(runtime: Any) -> None:
    runtime.logger.info(f"Shutting down flex agent '{runtime.name}'...")
    runtime.is_shutting_down = True
    await _cancel_tasks(runtime._scheduled_retry_tasks)
    await _cancel_tasks(runtime._background_tasks)
    if runtime.process_task:
        runtime.process_task.cancel()
        with suppress(asyncio.CancelledError):
            await runtime.process_task
        runtime.process_task = None
    await runtime.backend_manager.shutdown()
    runtime._mark_runtime_shutdown(clean=True)

    if runtime.startup_success:
        for action in (runtime.app.updater.stop, runtime.app.stop, runtime.app.shutdown):
            try:
                await action()
            except Exception as exc:
                runtime.error_logger.warning(f"Shutdown warning: {exc}")
        runtime.logger.info("Telegram app shut down cleanly.")


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

            audit_active = runtime._audit_enabled() and should_audit_source(item.source)
            audit_collector = AuditTelemetryCollector() if audit_active else None
            feedback = await runtime_pipeline.setup_interactive_feedback(
                runtime,
                item,
                audit_active=audit_active,
                audit_collector=audit_collector,
            )

            generation = await runtime_pipeline.run_backend_generation(
                runtime,
                item,
                final_prompt,
                on_stream_event=feedback.on_stream_event,
                audit_active=audit_active,
            )
            response = generation.response
            backend_started = generation.backend_started

            if generation.detached:
                if feedback.stop_typing and feedback.typing_task:
                    feedback.stop_typing.set()
                    await feedback.typing_task
                    if feedback.escalation_task is not None:
                        with suppress(asyncio.CancelledError):
                            await feedback.escalation_task
                if feedback.placeholder:
                    with suppress(Exception):
                        await runtime.app.bot.edit_message_text(
                            chat_id=item.chat_id,
                            message_id=feedback.placeholder.message_id,
                            text="⏳ Still running in the background — I'll notify you here when done! 📬",
                        )
                setattr(item, "_audit_collector", audit_collector)
                runtime._register_background_task(generation.generation_task, item)
                runtime.logger.info(
                    f"Detached {item.request_id} to background "
                    f"(threshold={generation.detach_after_s}s, backend={runtime.config.active_backend})"
                )
                runtime._log_maintenance(item, "bg_detached", detach_after_s=generation.detach_after_s)
                continue

            backend_elapsed = (datetime.now() - backend_started).total_seconds()
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
                think_flush_task=feedback.think_flush_task,
                placeholder=feedback.placeholder,
            )

            if response.is_success and not response.text:
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
                    )
            else:
                await runtime_pipeline.handle_backend_error(
                    runtime,
                    item,
                    response,
                    queued_at=queued_at,
                    queue_wait_s=queue_wait_s,
                    backend_elapsed_s=backend_elapsed,
                )

        except asyncio.CancelledError:
            break
        except Exception as exc:
            runtime._mark_error(str(exc))
            if item is not None:
                runtime._record_habit_outcome(item, success=False, error_text=str(exc))
            runtime.error_logger.exception(f"Error in flex queue processing: {exc}")
            runtime.is_generating = False
        finally:
            runtime.current_request_meta = None
            if item is not None:
                runtime.queue.task_done()


async def _cancel_tasks(tasks: set[asyncio.Task]) -> None:
    for task in list(tasks):
        task.cancel()
    for task in list(tasks):
        with suppress(asyncio.CancelledError):
            await task
