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
