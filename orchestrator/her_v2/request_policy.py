"""Request-scoped HER v2 effort policy for scheduled work.

HER effort controls orchestration stages only.  It must never be reused as a
provider reasoning setting or stored back into the owning Agent's global
configuration.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .models import Effort, parse_effort


HER_V2_JOB_EFFORT_FIELD = "her_v2_effort"
HER_V2_SCHEDULED_DEFAULT_EFFORT = Effort.LOW
SCHEDULED_JOB_KINDS = frozenset({"cron", "heartbeat"})
SCHEDULER_TRIGGERS = frozenset({"scheduled", "manual", "recovery"})


def normalize_job_effort(value: Any) -> Effort | None:
    """Return a validated optional job override.

    Blank or absent values mean that the scheduled-job default applies.  Bad
    values are rejected rather than silently changing orchestration policy.
    """

    if value is None or not str(value).strip():
        return None
    try:
        return parse_effort(str(value))
    except ValueError as exc:
        allowed = ", ".join(item.value for item in Effort)
        raise ValueError(
            f"{HER_V2_JOB_EFFORT_FIELD} must be one of: {allowed}"
        ) from exc


def normalize_job_effort_in_place(job: dict[str, Any]) -> Effort | None:
    """Validate and canonicalise an optional persisted job override."""

    effort = normalize_job_effort(job.get(HER_V2_JOB_EFFORT_FIELD))
    if effort is None:
        job.pop(HER_V2_JOB_EFFORT_FIELD, None)
    else:
        job[HER_V2_JOB_EFFORT_FIELD] = effort.value
    return effort


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
    override = normalize_job_effort(job.get(HER_V2_JOB_EFFORT_FIELD))
    context = {
        "kind": normalized_kind,
        "task_id": task_id,
        "trigger": normalized_trigger,
    }
    if override is not None:
        context["her_v2_effort_override"] = override.value
    return context


def job_effort_policy(job: Mapping[str, Any]) -> dict[str, str]:
    """Describe the effective HER v2 policy represented by a job record."""

    override = normalize_job_effort(job.get(HER_V2_JOB_EFFORT_FIELD))
    return {
        "effective": (
            override.value
            if override is not None
            else HER_V2_SCHEDULED_DEFAULT_EFFORT.value
        ),
        "source": "job_override" if override is not None else "scheduled_job_default",
        "applies_to": "her-v2",
    }


@dataclass(frozen=True)
class EffortResolution:
    configured: Effort
    effective: Effort
    reason: str
    scheduler_kind: str | None = None
    scheduler_task_id: str | None = None
    scheduler_trigger: str | None = None
    job_override: Effort | None = None

    def metadata(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "configured": self.configured.value,
            "effective": self.effective.value,
            "reason": self.reason,
        }
        if self.scheduler_kind:
            payload["scheduler_kind"] = self.scheduler_kind
        if self.scheduler_task_id:
            payload["scheduler_task_id"] = self.scheduler_task_id
        if self.scheduler_trigger:
            payload["scheduler_trigger"] = self.scheduler_trigger
        if self.job_override is not None:
            payload["job_override"] = self.job_override.value
        return payload


def resolve_request_effort(
    configured_effort: Effort | str,
    request_meta: Mapping[str, Any] | None,
) -> EffortResolution:
    """Resolve one HER v2 request without mutating Agent configuration."""

    configured = (
        configured_effort
        if isinstance(configured_effort, Effort)
        else parse_effort(str(configured_effort))
    )
    meta = request_meta if isinstance(request_meta, Mapping) else {}
    raw_context = meta.get("scheduler_context")
    if not isinstance(raw_context, Mapping):
        return EffortResolution(
            configured=configured,
            effective=configured,
            reason="agent_default",
        )

    kind = str(raw_context.get("kind") or "").strip().lower()
    task_id = str(raw_context.get("task_id") or "").strip()
    trigger = str(raw_context.get("trigger") or "").strip().lower()
    if (
        kind not in SCHEDULED_JOB_KINDS
        or not task_id
        or trigger not in SCHEDULER_TRIGGERS
    ):
        return EffortResolution(
            configured=configured,
            effective=configured,
            reason="agent_default",
        )

    override = normalize_job_effort(
        raw_context.get("her_v2_effort_override")
    )
    return EffortResolution(
        configured=configured,
        effective=override or HER_V2_SCHEDULED_DEFAULT_EFFORT,
        reason="job_override" if override is not None else "scheduled_job_default",
        scheduler_kind=kind,
        scheduler_task_id=task_id,
        scheduler_trigger=trigger,
        job_override=override,
    )
