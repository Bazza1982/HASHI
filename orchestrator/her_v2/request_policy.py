"""Request-scoped HER v2 execution policy for deterministic HASHI actions.

HER v3 has one foreground execution path. Request metadata may explain why a
turn exists, but it must never override the Agent's selected model reasoning
effort. Scheduled and HChat work therefore preserve the configured effort.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .models import Effort, parse_effort


HER_V2_JOB_EFFORT_FIELD = "her_v2_effort"
HCHAT_REQUEST_SOURCES = frozenset({"bridge:hchat", "bridge:hchat-draft"})
SCHEDULED_JOB_KINDS = frozenset({"cron", "heartbeat"})
SCHEDULER_TRIGGERS = frozenset({"scheduled", "manual", "recovery"})


def discard_legacy_job_effort_in_place(job: dict[str, Any]) -> bool:
    """Remove the retired per-job override and report whether it was present.

    Older task files remain loadable, but their override cannot bypass the
    selected model reasoning policy. Mutation boundaries use this helper to migrate
    those records opportunistically.
    """

    present = HER_V2_JOB_EFFORT_FIELD in job
    job.pop(HER_V2_JOB_EFFORT_FIELD, None)
    return present


def infer_scheduler_job_kind(
    job: Mapping[str, Any],
    explicit_kind: str | None = None,
) -> str:
    """Resolve cron versus heartbeat from explicit authority or job schema."""

    kind = str(explicit_kind or "").strip().lower()
    if not kind:
        kind = "heartbeat" if "interval_seconds" in job else "cron"
    if kind not in SCHEDULED_JOB_KINDS:
        raise ValueError("scheduler kind must be cron or heartbeat")
    return kind


def build_scheduler_request_context(
    job: Mapping[str, Any],
    *,
    kind: str,
    trigger: str,
) -> dict[str, str]:
    """Build explicit request metadata for one cron/heartbeat invocation."""

    normalized_kind = infer_scheduler_job_kind(job, kind)
    normalized_trigger = str(trigger or "").strip().lower()
    if normalized_trigger not in SCHEDULER_TRIGGERS:
        raise ValueError(
            "scheduler trigger must be scheduled, manual, or recovery"
        )
    task_id = str(job.get("id") or "").strip()
    if not task_id:
        raise ValueError("scheduled job id is required")
    return {
        "kind": normalized_kind,
        "task_id": task_id,
        "trigger": normalized_trigger,
    }


def job_effort_policy(job: Mapping[str, Any]) -> dict[str, str]:
    """Describe the selected model reasoning policy for a job record."""

    return {
        "effective": "inherit",
        "source": "herv3_model_reasoning",
        "applies_to": "her-v2",
    }


@dataclass(frozen=True)
class EffortResolution:
    configured: Effort
    effective: Effort
    reason: str
    model_reasoning: str
    scheduler_kind: str | None = None
    scheduler_task_id: str | None = None
    scheduler_trigger: str | None = None

    def metadata(self) -> dict[str, Any]:
        # The retained Effort enum represents the binary Provider option as
        # HIGH internally; report the configured Provider value to callers.
        configured_value = (
            self.model_reasoning if self.model_reasoning == "enabled"
            else self.configured.value
        )
        payload: dict[str, Any] = {
            "configured": configured_value,
            "effective": (
                self.model_reasoning if self.model_reasoning == "enabled"
                else self.effective.value
            ),
            "reason": self.reason,
        }
        if self.scheduler_kind:
            payload["scheduler_kind"] = self.scheduler_kind
        if self.scheduler_task_id:
            payload["scheduler_task_id"] = self.scheduler_task_id
        if self.scheduler_trigger:
            payload["scheduler_trigger"] = self.scheduler_trigger
        return payload


def resolve_request_effort(
    configured_effort: Effort | str,
    request_meta: Mapping[str, Any] | None,
) -> EffortResolution:
    """Preserve the selected model reasoning effort for every HER v3 request."""

    raw_effort = (
        configured_effort.value
        if isinstance(configured_effort, Effort)
        else str(configured_effort).strip().casefold()
    )
    # A binary Provider reasoning switch is a valid HER v3 model setting.
    # HER's retained internal Effort enum needs a nonzero value for its Direct
    # turn bookkeeping, while the Provider must still receive "enabled".
    configured = Effort.HIGH if raw_effort == "enabled" else parse_effort(raw_effort)
    model_reasoning = (
        "enabled" if raw_effort == "enabled"
        else "off" if configured is Effort.ZERO
        else configured.value
    )
    meta = request_meta if isinstance(request_meta, Mapping) else {}
    raw_context = meta.get("scheduler_context")
    if isinstance(raw_context, Mapping):
        kind = str(raw_context.get("kind") or "").strip().lower() or None
        task_id = str(raw_context.get("task_id") or "").strip() or None
        trigger = str(raw_context.get("trigger") or "").strip().lower() or None
    else:
        kind = task_id = trigger = None
    return EffortResolution(
        configured=configured,
        effective=configured,
        reason="model_reasoning_effort",
        model_reasoning=model_reasoning,
        scheduler_kind=kind,
        scheduler_task_id=task_id,
        scheduler_trigger=trigger,
    )
