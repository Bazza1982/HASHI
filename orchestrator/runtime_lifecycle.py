from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import Any


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


async def _cancel_tasks(tasks: set[asyncio.Task]) -> None:
    for task in list(tasks):
        task.cancel()
    for task in list(tasks):
        with suppress(asyncio.CancelledError):
            await task
