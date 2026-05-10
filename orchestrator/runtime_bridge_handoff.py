from __future__ import annotations

from typing import Any


async def cmd_transfer(runtime: Any, update: Any, context: Any) -> None:
    await runtime._cmd_bridge_handoff(update, context, mode="transfer")


async def cmd_fork(runtime: Any, update: Any, context: Any) -> None:
    await runtime._cmd_bridge_handoff(update, context, mode="fork")
