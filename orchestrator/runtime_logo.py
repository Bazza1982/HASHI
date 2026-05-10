from __future__ import annotations

import asyncio
from typing import Any


async def cmd_logo(runtime: Any, update: Any, context: Any) -> None:
    if not runtime._is_authorized_user(update.effective_user.id):
        return

    from orchestrator.agent_runtime import _show_logo_animation

    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _show_logo_animation)
    await runtime._reply_text(update, "Logo displayed in console.")
