from __future__ import annotations

from typing import Any


def _scheduler(runtime: Any):
    orchestrator = getattr(runtime, "orchestrator", None)
    return getattr(orchestrator, "scheduler", None) if orchestrator is not None else None


def context_section(runtime: Any, source: str) -> list[tuple[str, str]]:
    """Expose durable recovery facts to user-driven agent turns."""
    if str(source or "").startswith("scheduler"):
        return []
    scheduler = _scheduler(runtime)
    builder = getattr(scheduler, "build_recovery_context", None)
    if not callable(builder):
        return []
    body = builder(getattr(runtime, "name", ""))
    return [("SCHEDULER RECOVERY", body)] if body else []


async def handle_reply(runtime: Any, *, text: str, chat_id: int) -> bool:
    """Resolve an unambiguous recovery choice before invoking an agent."""
    from orchestrator.fresh_context import automatic_context_suppressed

    if (
        str(getattr(getattr(runtime, "config", None), "active_backend", ""))
        == "her-v2"
        and automatic_context_suppressed(runtime)
    ):
        return False
    scheduler = _scheduler(runtime)
    handler = getattr(scheduler, "handle_recovery_reply", None)
    if not callable(handler):
        return False
    result = await handler(
        agent_name=getattr(runtime, "name", ""),
        text=text,
        runtime_map={getattr(runtime, "name", ""): runtime},
    )
    if result is None:
        return False
    await runtime.send_long_message(
        chat_id=chat_id,
        text=result,
        request_id="scheduler-recovery-reply",
        purpose="scheduler-recovery",
    )
    return True
