"""HAL v0.1 event source mapping.

Maps the author-defined event vocabulary onto the ONE existing durable event
source (``orchestrator/automation_events.py``). HAL never invents a second event
system: every available event is derived from the real append-only feed.

Vocabulary without a real source is reported as unavailable, never faked.
``#system_idle`` has no reliable active->idle transition source in v0.1, so it
is reported unavailable; the vocabulary token and its compile support remain.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

# The only real feed kinds (orchestrator/automation_events.py::EVENT_KINDS).
FEED_KIND_RUN_COMPLETED = "run.completed"
FEED_KIND_RUN_FAILED = "run.failed"

# Author-defined system-idle vocabulary token. In v0.1 there is no reliable
# active->idle transition event source, so this event stays unavailable.
EVENT_SYSTEM_IDLE = "#system_idle"

# Agent-scoped events mapped 1:1 from real feed kinds. The feed projects the
# raw kind into "agent.run.*"; both spellings map to the same HAL event.
FEED_TO_HAL_EVENT = {
    FEED_KIND_RUN_COMPLETED: "#turn_finish",
    FEED_KIND_RUN_FAILED: "#turn_error",
    "agent.run.completed": "#turn_finish",
    "agent.run.failed": "#turn_error",
}

# Author vocabulary with no real event source. These remain valid grammar and
# are listed in validation, but a runtime trigger can never be produced today.
UNAVAILABLE_EVENT_REASONS = {
    "#turn_commentary": "no commentary event source exists in the durable event feed",
    "#hashideploy_finish": "no deployment-completion event source exists",
    "#reboot_ok": "no reboot-completion event source exists",
    "#system_idle": "no reliable active-to-idle transition event source in v0.1",
}

# Automation-origin idempotency prefixes. Runs dispatched by automation must
# never re-enter entry matching (recursion guard). Kept in one place so the
# feed projection and the HAL runtime agree.
AUTOMATION_ORIGIN_PREFIXES = ("hea:", "hal:")

# Namespace used by HAL-dispatched operations (prompt runs, receipts).
HAL_IDEMPOTENCY_PREFIX = "hal:"


@dataclass(frozen=True)
class HalTrigger:
    """One runtime trigger derived from the real feed."""

    event: str
    # Canonical agent identity (``@name@instance``) for agent events.
    agent_identity: str | None
    # Real feed event identity for dedupe/correlation.
    feed_event_id: str | None
    # ``run.completed``/``run.failed`` for feed events.
    feed_kind: str
    # Idempotency key of the originating run (correlation evidence).
    run_idempotency_key: str | None
    # True when the originating run was automation-dispatched.
    automation_origin: bool
    # Monotonic feed position (the event's ``cursor``); None for synthetic
    # triggers built in tests. Used for the per-rule enable baseline.
    feed_cursor: int | None = None


def feed_event_hal_event(event: Mapping[str, Any]) -> HalTrigger | None:
    """Map one real feed event to its HAL trigger, or None when unmappable."""
    kind = str(event.get("kind", "") or event.get("type", "") or "")
    hal_event = FEED_TO_HAL_EVENT.get(kind)
    if hal_event is None:
        return None
    key = event.get("run_idempotency_key")
    cursor = event.get("cursor")
    return HalTrigger(
        event=hal_event,
        agent_identity=event.get("agent_id") or None,
        feed_event_id=str(event.get("event_id", "") or ""),
        feed_kind=kind,
        run_idempotency_key=(str(key) if key else None),
        automation_origin=bool(event.get("automation_origin")),
        feed_cursor=(int(cursor) if isinstance(cursor, int) else None),
    )


def event_availability(event_name: str) -> tuple[bool, str]:
    """True when a real source can produce this event, plus a truthful reason."""
    if event_name in FEED_TO_HAL_EVENT.values():
        return True, "real durable run-terminal feed event"
    reason = UNAVAILABLE_EVENT_REASONS.get(event_name)
    if reason is not None:
        return False, reason
    return False, "unknown event vocabulary"


def is_automation_origin(idempotency_key: str | None) -> bool:
    """Shared recursion guard for feed projection and HAL runtime."""
    if not idempotency_key:
        return False
    return idempotency_key.startswith(AUTOMATION_ORIGIN_PREFIXES)
# HASHI-Fingerprint: hfp_06fd706020fc4264ad698c09e9a1de26
