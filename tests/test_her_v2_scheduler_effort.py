from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from orchestrator import runtime_jobs
from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime
from orchestrator.her_v2.models import Effort
from orchestrator.her_v2.request_policy import (
    build_scheduler_request_context,
    job_effort_policy,
    resolve_request_effort,
)
from orchestrator.legacy.bridge_agent_runtime import BridgeAgentRuntime


@pytest.mark.parametrize("kind", ["cron", "heartbeat"])
def test_scheduled_job_defaults_to_low_execution_effort(kind):
    context = build_scheduler_request_context(
        {"id": f"{kind}-1"},
        kind=kind,
        trigger="scheduled",
    )

    resolution = resolve_request_effort(
        Effort.MAX,
        {"scheduler_context": context},
    )

    assert resolution.configured is Effort.MAX
    assert resolution.effective is Effort.LOW
    assert resolution.reason == "scheduled_job_default"
    assert resolution.scheduler_kind == kind
    assert resolution.scheduler_trigger == "scheduled"


@pytest.mark.parametrize("effort", list(Effort))
def test_job_override_wins_without_changing_configured_effort(effort):
    job = {"id": "nightly", "her_v2_effort": effort.value}
    context = build_scheduler_request_context(
        job,
        kind="cron",
        trigger="manual",
    )

    resolution = resolve_request_effort(
        Effort.MAX,
        {"scheduler_context": context},
    )

    assert resolution.configured is Effort.MAX
    assert resolution.effective is effort
    assert resolution.reason == "job_override"
    assert resolution.job_override is effort
    assert job == {"id": "nightly", "her_v2_effort": effort.value}


def test_recovery_keeps_job_override_and_trigger_identity():
    context = build_scheduler_request_context(
        {"id": "replay", "her_v2_effort": "high"},
        kind="heartbeat",
        trigger="recovery",
    )

    resolution = resolve_request_effort(
        "medium",
        {"scheduler_context": context},
    )

    assert resolution.effective is Effort.HIGH
    assert resolution.scheduler_task_id == "replay"
    assert resolution.scheduler_trigger == "recovery"


def test_scheduler_source_without_explicit_job_context_keeps_agent_effort():
    resolution = resolve_request_effort(
        Effort.XHIGH,
        {"source": "scheduler", "summary": "Nudge Task [nudge-1]"},
    )

    assert resolution.effective is Effort.XHIGH
    assert resolution.reason == "agent_default"


@pytest.mark.parametrize(
    "scheduler_context",
    [
        {"kind": "cron", "trigger": "scheduled"},
        {"kind": "cron", "task_id": "job-1", "trigger": "unknown"},
        {"kind": "nudge", "task_id": "job-1", "trigger": "scheduled"},
    ],
)
def test_incomplete_or_non_job_context_cannot_change_agent_effort(
    scheduler_context,
):
    resolution = resolve_request_effort(
        Effort.MAX,
        {"scheduler_context": scheduler_context},
    )

    assert resolution.effective is Effort.MAX
    assert resolution.reason == "agent_default"


def test_ordinary_and_delayed_requests_keep_agent_effort():
    for source in ("text", "bridge:api", "delay"):
        resolution = resolve_request_effort(Effort.HIGH, {"source": source})
        assert resolution.effective is Effort.HIGH
        assert resolution.reason == "agent_default"


def test_invalid_job_override_is_rejected_instead_of_silently_coerced():
    with pytest.raises(ValueError, match="her_v2_effort must be one of"):
        build_scheduler_request_context(
            {"id": "broken", "her_v2_effort": "low-reasoning"},
            kind="cron",
            trigger="scheduled",
        )


def test_job_policy_reports_default_and_override_for_user_surfaces():
    assert job_effort_policy({"id": "default"}) == {
        "effective": "low",
        "source": "scheduled_job_default",
        "applies_to": "her-v2",
    }
    assert job_effort_policy(
        {"id": "override", "her_v2_effort": "medium"}
    ) == {
        "effective": "medium",
        "source": "job_override",
        "applies_to": "her-v2",
    }


@pytest.mark.asyncio
async def test_manual_run_now_propagates_kind_trigger_and_job_override():
    runtime = SimpleNamespace(
        _primary_chat_id=lambda: 123,
        enqueue_request=AsyncMock(return_value="req-1"),
    )
    job = {
        "id": "daily-report",
        "agent": "momo",
        "prompt": "Send the daily report",
        "her_v2_effort": "medium",
    }

    result = await FlexibleAgentRuntime._run_job_now(
        runtime,
        job,
        kind="cron",
    )

    assert result == (True, "Queued cron task [daily-report]")
    runtime.enqueue_request.assert_awaited_once_with(
        chat_id=123,
        prompt="Send the daily report",
        source="scheduler",
        summary="Cron Task [daily-report]",
        scheduler_context={
            "kind": "cron",
            "task_id": "daily-report",
            "trigger": "manual",
            "her_v2_effort_override": "medium",
        },
    )


@pytest.mark.asyncio
async def test_manual_run_now_rejects_invalid_effort_before_queueing():
    runtime = SimpleNamespace(
        _primary_chat_id=lambda: 123,
        enqueue_request=AsyncMock(return_value="req-1"),
    )
    job = {
        "id": "broken-job",
        "agent": "momo",
        "prompt": "Do work",
        "her_v2_effort": "turbo",
    }

    ok, message = await FlexibleAgentRuntime._run_job_now(
        runtime,
        job,
        kind="heartbeat",
    )

    assert ok is False
    assert "Invalid scheduler policy" in message
    assert "her_v2_effort must be one of" in message
    runtime.enqueue_request.assert_not_awaited()


@pytest.mark.asyncio
async def test_legacy_manual_heartbeat_run_uses_same_request_policy():
    runtime = SimpleNamespace(
        global_config=SimpleNamespace(authorized_id=456),
        enqueue_request=AsyncMock(return_value="req-legacy"),
    )
    job = {
        "id": "pulse",
        "agent": "momo",
        "interval_seconds": 300,
        "prompt": "Check status",
    }

    result = await BridgeAgentRuntime._run_job_now(
        runtime,
        job,
        kind="heartbeat",
    )

    assert result == (True, "Queued heartbeat task [pulse]")
    runtime.enqueue_request.assert_awaited_once_with(
        chat_id=456,
        prompt="Check status",
        source="scheduler",
        summary="Heartbeat Task [pulse]",
        scheduler_context={
            "kind": "heartbeat",
            "task_id": "pulse",
            "trigger": "manual",
        },
    )


@pytest.mark.asyncio
async def test_telegram_manual_run_preserves_explicit_job_kind():
    job = {
        "id": "pulse",
        "agent": "momo",
        "prompt": "Check status",
    }

    class _Query:
        async def answer(self, *_args, **_kwargs):
            return None

    runtime = SimpleNamespace(
        skill_manager=SimpleNamespace(
            get_job=lambda kind, task_id: (
                job if (kind, task_id) == ("heartbeat", "pulse") else None
            )
        ),
        _run_job_now=AsyncMock(return_value=(True, "queued")),
    )

    handled = await runtime_jobs.handle_skill_job_callback(
        runtime,
        _Query(),
        "skilljob:heartbeat:run:pulse:go",
    )

    assert handled is True
    runtime._run_job_now.assert_awaited_once_with(job, kind="heartbeat")
