from __future__ import annotations

import asyncio
from typing import Any


async def cmd_terminate(runtime: Any, update: Any, context: Any) -> None:
    if not runtime._is_authorized_user(update.effective_user.id):
        return

    orchestrator = getattr(runtime, "orchestrator", None)
    if orchestrator is None:
        await runtime._reply_text(update, "Dynamic lifecycle control is unavailable.")
        return

    await runtime._reply_text(update, "Shutting down.")
    asyncio.create_task(orchestrator.stop_agent(runtime.name))
