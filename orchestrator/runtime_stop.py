from __future__ import annotations

import asyncio
from typing import Any


async def cmd_stop(runtime: Any, update: Any, context: Any) -> None:
    if not runtime._is_authorized_user(update.effective_user.id):
        return

    runtime.logger.warning(
        f"Manual stop requested for flex agent {runtime.name} "
        f"(queue_size={runtime.queue.qsize()}, backend={runtime.config.active_backend})"
    )
    if runtime.backend_manager.current_backend:
        await runtime.backend_manager.current_backend.shutdown()

    dropped = 0
    while not runtime.queue.empty():
        try:
            runtime.queue.get_nowait()
            runtime.queue.task_done()
            dropped += 1
        except asyncio.QueueEmpty:
            break

    await runtime._reply_text(
        update,
        f"Stopped execution. Cleared {dropped} queued messages and killed active backend process tree.",
    )
