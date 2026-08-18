from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from adapters.her import ClawCommandError, ClawTaskResult, ClawTimeoutError, HERAdapter
from adapters.her_ultra import (
    HERUltraAuthorityEnvelope,
    HERUltraConfig,
    HERUltraContractError,
    HERUltraInvocationResult,
    HERUltraOrchestrator,
    HERUltraRunLedger,
    HERUltraSubtask,
    HERUltraTaskContractValidator,
    HERUltraWorkerResult,
    extract_json_object,
)
from adapters.stream_events import (
    DELIVERY_FINAL,
    DELIVERY_TECHNICAL,
    KIND_ACKNOWLEDGEMENT,
    KIND_PROGRESS,
    StreamEvent,
)


def _config(**overrides) -> HERUltraConfig:
    values = {
        "max_concurrent_subagents": 2,
        "subagent_retry_limit": 1,
        "max_plan_revisions": 2,
    }
    values.update(overrides)
    return HERUltraConfig.from_mapping(
        values,
        primary_model="model-pro",
        allowed_models=("model-pro", "model-flash"),
    )


def _authority(*, write_enabled: bool = False) -> HERUltraAuthorityEnvelope:
    return HERUltraAuthorityEnvelope.build(
        permission_mode="workspace-write" if write_enabled else "read-only",
        access_root="/workspace",
        allowed_tools=("Read", "Bash"),
        write_enabled=write_enabled,
    )


def _subtask_payload(
    subtask_id: str,
    *,
    depends_on: tuple[str, ...] = (),
    optional: bool = False,
    retry_safe: bool = True,
) -> dict:
    return {
        "id": subtask_id,
        "title": f"Task {subtask_id}",
        "objective": f"Complete {subtask_id}",
        "depends_on": list(depends_on),
        "model_class": "current",
        "effort": "high",
        "workspace_strategy": "shared_read_only",
        "retry_safe": retry_safe,
        "deliverables": [f"Result {subtask_id}"],
        "acceptance": [f"Evidence {subtask_id}"],
        "optional": optional,
    }


def _plan_payload(
    *,
    goal: str,
    parent_request_id: str,
    authority: HERUltraAuthorityEnvelope,
    subtasks: list[dict],
    direct_response: str = "",
) -> dict:
    return {
        "plan_id": f"{parent_request_id}:plan",
        "parent_request_id": parent_request_id,
        "authoritative_goal": goal,
        "authority_envelope_digest": authority.digest,
        "ultra_not_beneficial": bool(direct_response),
        "direct_response": direct_response,
        "subtasks": subtasks,
        "assembly_plan": {"strategy": "evidence_first"},
    }


def _worker_success(
    subtask_id: str, *, model: str = "model-flash"
) -> HERUltraInvocationResult:
    return HERUltraInvocationResult(
        text=json.dumps(
            {
                "subtask_id": subtask_id,
                "status": "completed",
                "claims": [f"claim:{subtask_id}"],
                "evidence": [{"type": "test", "reference": f"test:{subtask_id}"}],
                "artifacts": [],
                "validation": [f"validated:{subtask_id}"],
                "uncertainty": "",
                "unresolved_items": [],
                "retry_safe": True,
                "transient": False,
                "error": "",
                "error_type": "",
            }
        ),
        session_id=f"worker-{subtask_id}",
        model=model,
        input_tokens=10,
        output_tokens=5,
        duration_ms=2,
    )


def _primary_report(text: str, **kwargs: Any) -> HERUltraInvocationResult:
    """Construct an explicitly provenance-verified Primary model report."""

    kwargs.setdefault("terminal_kind", "model_report")
    kwargs.setdefault("message_origin", "primary_model")
    kwargs.setdefault("exit_reasoning_status", "embedded")
    return HERUltraInvocationResult(
        text=text,
        **kwargs,
    )


def test_ultra_worker_timeout_inherits_her_policy_unless_explicitly_configured():
    assert _config().subagent_timeout_sec is None
    assert _config(subagent_timeout_sec=900).subagent_timeout_sec == 900


def test_her_timeout_is_not_marked_retryable_for_ultra_workers():
    invocation = HERAdapter._ultra_error_invocation(
        ClawTimeoutError("HER command was idle", timeout_s=1800)
    )

    assert invocation.error_type == "timeout"
    assert invocation.retryable is False


def test_primary_report_provenance_is_fail_closed_by_default():
    assert (
        HERUltraInvocationResult(text="unattributed text").has_trusted_model_report
        is False
    )
    assert _primary_report("attributed model report").has_trusted_model_report is True


def test_extract_json_object_prefers_outer_latest_complete_object():
    text = 'diagnostic {"old": true}\n```json\n{"task": {"nested": 1}}\n```'

    assert extract_json_object(text) == {"task": {"nested": 1}}


def test_ultra_config_caps_concurrency_at_ten():
    config = HERUltraConfig.from_mapping(
        {
            "max_concurrent_subagents": 99,
            "max_plan_revisions": 99,
        }
    )

    assert config.max_concurrent_subagents == 10
    assert config.max_plan_revisions == 2
    assert config.primary_inner_effort == "high"


def test_task_contract_accepts_dag_and_rejects_cycles():
    goal = "Inspect and verify"
    request_id = "req-contract"
    authority = _authority()
    validator = HERUltraTaskContractValidator(_config())
    payload = _plan_payload(
        goal=goal,
        parent_request_id=request_id,
        authority=authority,
        subtasks=[
            _subtask_payload("a"),
            _subtask_payload("b", depends_on=("a",)),
        ],
    )

    plan = validator.validate_plan(
        payload,
        authoritative_goal=goal,
        parent_request_id=request_id,
        authority=authority,
        revision=1,
    )

    assert [task.subtask_id for task in plan.subtasks] == ["a", "b"]
    payload["subtasks"][0]["depends_on"] = ["b"]
    with pytest.raises(HERUltraContractError, match="cycle"):
        validator.validate_plan(
            payload,
            authoritative_goal=goal,
            parent_request_id=request_id,
            authority=authority,
            revision=2,
        )


def test_task_contract_does_not_reauthorize_or_narrow_parent_authority():
    goal = "Use the inherited Agent authority"
    request_id = "req-authority"
    authority = _authority()
    validator = HERUltraTaskContractValidator(_config())
    payload = _plan_payload(
        goal="different goal",
        parent_request_id=request_id,
        authority=authority,
        subtasks=[_subtask_payload("writer", retry_safe=False)],
    )
    payload["subtasks"][0].update(
        {
            "allowed_actions": [],
            "required_permission_mode": "read-only",
            "required_tools": [],
            "required_paths": ["/wrong/legacy/path"],
        }
    )

    plan = validator.validate_plan(
        payload,
        authoritative_goal=goal,
        parent_request_id=request_id,
        authority=authority,
        revision=1,
    )

    assert plan.subtasks[0].retry_safe is False
    assert not hasattr(plan.subtasks[0], "required_permission_mode")
    assert not hasattr(plan.subtasks[0], "required_tools")
    assert not hasattr(plan.subtasks[0], "required_paths")


def test_worker_result_accepts_natural_text_and_coerces_optional_json_fields():
    subtask = HERUltraSubtask(
        subtask_id="a",
        title="A",
        objective="A",
        depends_on=(),
        model="",
        model_class="current",
        effort="high",
        deliverables=("result",),
        acceptance=("evidence",),
        optional=False,
        retry_safe=True,
        workspace_strategy="shared_read_only",
    )
    natural = HERUltraWorkerResult.from_invocation(
        HERUltraInvocationResult(text="The relevant test passes."),
        subtask=subtask,
        result_id="result-natural",
        attempt=1,
    )
    assert natural.status == "completed"
    assert natural.claims == ("The relevant test passes.",)

    blocked = HERUltraWorkerResult.from_invocation(
        HERUltraInvocationResult(
            text="BLOCKED: tool 'bash' requires danger-full-access permission"
        ),
        subtask=subtask,
        result_id="result-blocked",
        attempt=1,
    )
    assert blocked.status == "blocked"
    assert blocked.completed is False
    assert blocked.error_type == "permission_blocked"

    invocation = HERUltraInvocationResult(
        text=json.dumps(
            {
                "subtask_id": "a",
                "status": "completed",
                "claims": "not-a-list",
                "evidence": [],
                "validation": [],
            }
        )
    )

    result = HERUltraWorkerResult.from_invocation(
        invocation,
        subtask=subtask,
        result_id="result-1",
        attempt=1,
    )

    assert result.status == "completed"
    assert result.claims == ("not-a-list",)


def test_worker_result_preserves_noncanonical_deliverable_payload():
    subtask = HERUltraSubtask(
        subtask_id="os-release",
        title="OS release",
        objective="Read OS facts",
        depends_on=(),
        model="model",
        model_class="current",
        effort="high",
        deliverables=("OS fields",),
        acceptance=("values returned",),
        optional=False,
        retry_safe=True,
        workspace_strategy="shared_read_only",
    )
    payload = {
        "status": "completed",
        "subtask_id": "os-release",
        "result": {"fields": {"NAME": "Ubuntu", "VERSION_ID": "22.04"}},
        "sources": ["/etc/os-release"],
        "validation_performed": "Read and cross-checked the source.",
        "unresolved": [],
    }

    result = HERUltraWorkerResult.from_invocation(
        HERUltraInvocationResult(text=json.dumps(payload)),
        subtask=subtask,
        result_id="result-os-release",
        attempt=1,
    )

    assert result.completed is True
    assert result.evidence == ("/etc/os-release",)
    assert result.validation == ("Read and cross-checked the source.",)
    assert result.raw_payload == payload


def test_worker_result_rejects_empty_completed_receipt():
    subtask = HERUltraSubtask(
        subtask_id="filesystem",
        title="Filesystem",
        objective="Report capacity",
        depends_on=(),
        model="model",
        model_class="current",
        effort="high",
        deliverables=("capacity values",),
        acceptance=("actual values returned",),
        optional=False,
        retry_safe=True,
        workspace_strategy="shared_read_only",
    )

    result = HERUltraWorkerResult.from_invocation(
        HERUltraInvocationResult(text=json.dumps({"status": "completed"})),
        subtask=subtask,
        result_id="result-empty",
        attempt=1,
    )

    assert result.status == "failed"
    assert result.error_type == "malformed_output"
    assert result.error == "completed worker result contains no deliverable payload"
    assert result.transient is True
    assert result.raw_payload == {"status": "completed"}


@pytest.mark.asyncio
async def test_assembly_receives_complete_noncanonical_worker_payload(tmp_path):
    goal = "Report OS facts"
    authority = _authority()
    plan_payload = _plan_payload(
        goal=goal,
        parent_request_id="req-raw-payload",
        authority=authority,
        subtasks=[_subtask_payload("os-release")],
    )
    worker_payload = {
        "status": "completed",
        "subtask_id": "os-release",
        "result": {"fields": {"NAME": "Ubuntu", "VERSION_ID": "22.04"}},
        "sources": ["/etc/os-release"],
        "validation_performed": "Exact values copied from the file.",
    }

    async def primary(spec):
        if spec.phase == "planning":
            return _primary_report(
                text=json.dumps(plan_payload), session_id="primary"
            )
        assert spec.phase == "assembly"
        assert '"NAME": "Ubuntu"' in spec.prompt
        assert '"VERSION_ID": "22.04"' in spec.prompt
        assert '"raw_payload"' in spec.prompt
        return _primary_report(text="verified answer", session_id="primary")

    async def worker(_spec):
        return HERUltraInvocationResult(text=json.dumps(worker_payload))

    outcome = await HERUltraOrchestrator(
        config=_config(),
        ledger_root=tmp_path,
        primary_executor=primary,
        worker_executor=worker,
        run_id_factory=lambda: "run-raw-payload",
    ).run(
        authoritative_goal=goal,
        parent_request_id="req-raw-payload",
        authority=authority,
    )

    assert outcome.is_success is True
    assert outcome.text == "verified answer"


def test_run_ledger_is_durable_idempotent_and_cancellation_fenced(tmp_path):
    ledger = HERUltraRunLedger(
        tmp_path,
        run_id="run-1",
        parent_request_id="req-1",
        authoritative_goal="goal",
    )

    assert ledger.transition(
        event_id="event-1", entity="run", state="running", data={"step": 1}
    )
    assert not ledger.transition(
        event_id="event-1", entity="run", state="running", data={"step": 2}
    )
    stale_generation = ledger.cancellation_generation
    new_generation = ledger.cancel("stop")

    assert new_generation == stale_generation + 1
    assert ledger.status == "cancelled"
    assert not ledger.transition(
        event_id="late-result",
        entity="subtask:a",
        state="completed",
        cancellation_generation=stale_generation,
    )
    snapshot = json.loads(ledger.snapshot_path.read_text(encoding="utf-8"))
    journal = ledger.journal_path.read_text(encoding="utf-8").splitlines()
    assert snapshot["status"] == "cancelled"
    assert snapshot["last_sequence"] == 2
    assert len(journal) == 2


@pytest.mark.asyncio
async def test_orchestrator_uses_parallel_workers_and_one_primary_assembly(tmp_path):
    goal = "Investigate three parts"
    request_id = "req-parallel"
    authority = _authority()
    phases = []
    worker_calls = []
    active = 0
    maximum_active = 0
    events = []
    commentary_facts = []
    plan_payload = _plan_payload(
        goal=goal,
        parent_request_id=request_id,
        authority=authority,
        subtasks=[
            _subtask_payload("a"),
            _subtask_payload("b"),
            _subtask_payload("c", depends_on=("a", "b")),
        ],
    )

    async def primary(spec):
        phases.append((spec.phase, spec.resume_session_id))
        if spec.phase == "planning":
            return _primary_report(
                text=json.dumps(plan_payload),
                session_id="primary-session",
                model="model-pro",
                input_tokens=20,
                output_tokens=10,
            )
        assert spec.phase == "assembly"
        assert spec.resume_session_id == "primary-session"
        assert '"a"' in spec.prompt and '"c"' in spec.prompt
        return _primary_report(
            text="assembled answer",
            session_id="primary-session",
            model="model-pro",
            input_tokens=15,
            output_tokens=8,
        )

    async def worker(spec):
        nonlocal active, maximum_active
        worker_calls.append(spec.subtask_id)
        active += 1
        maximum_active = max(maximum_active, active)
        await asyncio.sleep(0.02)
        active -= 1
        return _worker_success(spec.subtask_id)

    async def capture(event):
        events.append(event)

    async def render_commentary(facts):
        commentary_facts.append(dict(facts))
        return f"persona:{facts['phase']}"

    orchestrator = HERUltraOrchestrator(
        config=_config(),
        ledger_root=tmp_path,
        primary_executor=primary,
        worker_executor=worker,
        on_stream_event=capture,
        persona_commentary_renderer=render_commentary,
        run_id_factory=lambda: "run-parallel",
    )

    outcome = await orchestrator.run(
        authoritative_goal=goal,
        parent_request_id=request_id,
        authority=authority,
    )

    assert outcome.is_success is True
    assert outcome.status == "completed"
    assert outcome.text == "assembled answer"
    assert outcome.subtask_count == 3
    assert outcome.completed_subtasks == 3
    assert maximum_active == 2
    assert worker_calls[:2] == ["a", "b"]
    assert worker_calls[-1] == "c"
    assert [phase for phase, _session in phases] == ["planning", "assembly"]
    assert {event.delivery_class for event in events} == {
        DELIVERY_TECHNICAL,
        "user_commentary",
    }
    assert [facts["phase"] for facts in commentary_facts] == [
        "planning_started",
        "plan_accepted",
        "worker_progress",
        "worker_progress",
        "assembly_started",
    ]
    final_progress = next(
        facts
        for facts in commentary_facts
        if facts["phase"] == "worker_progress" and facts["terminal_subtasks"] == 3
    )
    assert final_progress == {
        "phase": "worker_progress",
        "terminal_subtasks": 3,
        "total_subtasks": 3,
        "completed_subtasks": 3,
        "failed_subtasks": 0,
        "blocked_subtasks": 0,
    }
    assert len({event.event_id for event in events}) == len(events)
    journal = [
        json.loads(line)
        for line in (
            tmp_path / "run-parallel" / "transitions.jsonl"
        ).read_text(encoding="utf-8").splitlines()
    ]
    accepted_plan = next(record for record in journal if record["state"] == "plan_accepted")
    assert accepted_plan["data"]["source_sha256"].startswith("sha256:")
    accepted_workers = [
        record
        for record in journal
        if record["entity"].startswith("subtask:")
        and record["state"] == "completed"
    ]
    assert {record["data"]["worker_session_id"] for record in accepted_workers} == {
        "worker-a",
        "worker-b",
        "worker-c",
    }
    assert all(
        record["data"]["source_sha256"].startswith("sha256:")
        for record in accepted_workers
    )
    persona_events = [
        event for event in events if event.delivery_class == "user_commentary"
    ]
    assert all(event.provenance == "persona_renderer" for event in persona_events)


@pytest.mark.asyncio
async def test_orchestrator_retries_only_transient_retry_safe_worker(tmp_path):
    goal = "Retry safely"
    request_id = "req-retry"
    authority = _authority()
    attempts = 0
    plan_payload = _plan_payload(
        goal=goal,
        parent_request_id=request_id,
        authority=authority,
        subtasks=[_subtask_payload("a")],
    )

    async def primary(spec):
        if spec.phase == "planning":
            return _primary_report(
                text=json.dumps(plan_payload), session_id="primary"
            )
        return _primary_report(text="done", session_id="primary")

    async def worker(spec):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return HERUltraInvocationResult(
                text="",
                is_success=False,
                error="temporary transport failure",
                error_type="transport",
                retryable=True,
            )
        return _worker_success(spec.subtask_id)

    outcome = await HERUltraOrchestrator(
        config=_config(),
        ledger_root=tmp_path,
        primary_executor=primary,
        worker_executor=worker,
        run_id_factory=lambda: "run-retry",
    ).run(
        authoritative_goal=goal,
        parent_request_id=request_id,
        authority=authority,
    )

    assert outcome.is_success is True
    assert attempts == 2


@pytest.mark.asyncio
async def test_orchestrator_does_not_retry_a_worker_timeout(tmp_path):
    goal = "Do not replay timed-out work"
    request_id = "req-timeout-no-retry"
    authority = _authority()
    attempts = 0
    phases = []
    plan_payload = _plan_payload(
        goal=goal,
        parent_request_id=request_id,
        authority=authority,
        subtasks=[_subtask_payload("a")],
    )

    async def primary(spec):
        phases.append(spec.phase)
        if spec.phase == "planning":
            return _primary_report(
                text=json.dumps(plan_payload), session_id="primary"
            )
        assert spec.phase == "failure_finalization"
        return _primary_report(
            text="The worker timed out; the task is incomplete.",
            session_id="primary",
        )

    async def worker(_spec):
        nonlocal attempts
        attempts += 1
        return HERUltraInvocationResult(
            text="",
            is_success=False,
            error="worker timed out",
            error_type="timeout",
            # Even a provider that labels timeout retryable must not make the
            # orchestrator replay the same isolated task from scratch.
            retryable=True,
        )

    outcome = await HERUltraOrchestrator(
        config=_config(subagent_retry_limit=3),
        ledger_root=tmp_path,
        primary_executor=primary,
        worker_executor=worker,
        run_id_factory=lambda: "run-timeout-no-retry",
    ).run(
        authoritative_goal=goal,
        parent_request_id=request_id,
        authority=authority,
    )

    assert attempts == 1
    assert phases == ["planning", "failure_finalization"]
    assert outcome.status == "incomplete"
    assert outcome.is_success is True


@pytest.mark.asyncio
async def test_worker_inherits_full_parent_authority_without_subtask_reauthorization(
    tmp_path,
):
    authority = HERUltraAuthorityEnvelope.build(
        permission_mode="danger-full-access",
        access_root="/",
        allowed_tools=("*",),
        write_enabled=True,
    )
    plan_payload = _plan_payload(
        goal="Perform system work",
        parent_request_id="req-full-authority",
        authority=authority,
        subtasks=[_subtask_payload("system-change", retry_safe=False)],
    )
    worker_specs = []

    async def primary(spec):
        if spec.phase == "planning":
            return _primary_report(
                text=json.dumps(plan_payload), session_id="primary"
            )
        return _primary_report(text="assembled", session_id="primary")

    async def worker(spec):
        worker_specs.append(spec)
        return _worker_success(spec.subtask_id)

    outcome = await HERUltraOrchestrator(
        config=_config(),
        ledger_root=tmp_path,
        primary_executor=primary,
        worker_executor=worker,
        run_id_factory=lambda: "run-full-authority",
    ).run(
        authoritative_goal="Perform system work",
        parent_request_id="req-full-authority",
        authority=authority,
    )

    assert outcome.is_success is True
    assert len(worker_specs) == 1
    assert worker_specs[0].permission_mode == "danger-full-access"
    assert worker_specs[0].allowed_tools == ("*",)
    assert worker_specs[0].workspace == "/"
    assert worker_specs[0].retry_safe is False


@pytest.mark.asyncio
async def test_primary_marks_non_idempotent_worker_as_not_retryable(tmp_path):
    authority = _authority(write_enabled=True)
    plan_payload = _plan_payload(
        goal="Change state once",
        parent_request_id="req-no-retry",
        authority=authority,
        subtasks=[_subtask_payload("change-once", retry_safe=False)],
    )
    attempts = 0

    async def primary(spec):
        if spec.phase == "planning":
            return _primary_report(
                text=json.dumps(plan_payload), session_id="primary"
            )
        raise AssertionError("assembly must not run without usable evidence")

    async def worker(_spec):
        nonlocal attempts
        attempts += 1
        return HERUltraInvocationResult(
            text="",
            is_success=False,
            error="uncertain transport result",
            error_type="transport",
            retryable=True,
        )

    outcome = await HERUltraOrchestrator(
        config=_config(subagent_retry_limit=3),
        ledger_root=tmp_path,
        primary_executor=primary,
        worker_executor=worker,
        run_id_factory=lambda: "run-no-retry",
    ).run(
        authoritative_goal="Change state once",
        parent_request_id="req-no-retry",
        authority=authority,
    )

    assert outcome.is_success is False
    assert attempts == 1


@pytest.mark.asyncio
async def test_persona_commentary_renderer_failure_uses_explicit_neutral_fallback(
    tmp_path,
):
    events = []

    async def broken_renderer(_facts):
        raise RuntimeError("renderer unavailable")

    async def capture(event):
        events.append(event)

    orchestrator = HERUltraOrchestrator(
        config=_config(),
        ledger_root=tmp_path,
        primary_executor=lambda _spec: None,
        worker_executor=lambda _spec: None,
        on_stream_event=capture,
        persona_commentary_renderer=broken_renderer,
    )

    await orchestrator._emit_persona_commentary(
        {"phase": "planning_started"},
        event_id="fallback-commentary",
        phase="planning",
    )

    assert len(events) == 1
    assert events[0].delivery_class == "user_commentary"
    assert events[0].summary.startswith("[HER neutral fallback]")
    assert events[0].provenance == "neutral_fallback"
    assert "error_type=RuntimeError" in events[0].detail


@pytest.mark.asyncio
async def test_failed_dependency_is_not_dispatched(tmp_path):
    goal = "Do dependent work"
    request_id = "req-dependency"
    authority = _authority()
    worker_calls = []
    plan_payload = _plan_payload(
        goal=goal,
        parent_request_id=request_id,
        authority=authority,
        subtasks=[
            _subtask_payload("a"),
            _subtask_payload("b", depends_on=("a",)),
        ],
    )

    primary_phases = []

    async def primary(spec):
        primary_phases.append(spec.phase)
        if spec.phase == "planning":
            return _primary_report(
                text=json.dumps(plan_payload), session_id="primary"
            )
        assert spec.phase == "failure_finalization"
        assert "No required worker produced usable evidence" in spec.prompt
        assert '"a"' in spec.prompt and '"b"' in spec.prompt
        assert "permanent failure" in spec.prompt
        return _primary_report(
            text="Both required tasks failed; the work is incomplete.",
            session_id="primary-final",
        )

    async def worker(spec):
        worker_calls.append(spec.subtask_id)
        return HERUltraInvocationResult(
            text="",
            is_success=False,
            error="permanent failure",
            error_type="permanent",
            retryable=False,
        )

    orchestrator = HERUltraOrchestrator(
        config=_config(),
        ledger_root=tmp_path,
        primary_executor=primary,
        worker_executor=worker,
        run_id_factory=lambda: "run-dependency",
    )
    outcome = await orchestrator.run(
        authoritative_goal=goal,
        parent_request_id=request_id,
        authority=authority,
    )

    assert outcome.is_success is True
    assert outcome.status == "incomplete"
    assert outcome.text == "Both required tasks failed; the work is incomplete."
    assert outcome.error == ""
    assert worker_calls == ["a"]
    assert primary_phases == ["planning", "failure_finalization"]
    state = json.loads(
        (tmp_path / "run-dependency" / "state.json").read_text(encoding="utf-8")
    )
    assert state["status"] == "incomplete"
    assert state["primary"]["state"] == "completed"
    assert state["primary"]["phase"] == "failure_finalization"
    assert state["primary"]["completion_status"] == "incomplete"
    assert state["subtasks"]["b"]["state"] == "failed"
    assert state["subtasks"]["b"]["failed_dependencies"] == ["a"]


@pytest.mark.asyncio
async def test_required_worker_failure_is_reported_as_incomplete(tmp_path):
    goal = "Report partial evidence honestly"
    request_id = "req-partial-required"
    authority = _authority()
    plan_payload = _plan_payload(
        goal=goal,
        parent_request_id=request_id,
        authority=authority,
        subtasks=[_subtask_payload("a"), _subtask_payload("b")],
    )

    async def primary(spec):
        if spec.phase == "planning":
            return _primary_report(
                text=json.dumps(plan_payload), session_id="primary"
            )
        assert spec.phase == "assembly"
        assert '"completion_status": "incomplete"' in spec.prompt
        assert '"required_failures": [\n    "b"\n  ]' in spec.prompt
        return _primary_report(
            text="Task a completed, but required task b failed.",
            session_id="primary-final",
        )

    async def worker(spec):
        if spec.subtask_id == "a":
            return _worker_success("a")
        return HERUltraInvocationResult(
            text="",
            is_success=False,
            error="provider rejected the request",
            error_type="permanent",
            retryable=False,
        )

    outcome = await HERUltraOrchestrator(
        config=_config(),
        ledger_root=tmp_path,
        primary_executor=primary,
        worker_executor=worker,
        run_id_factory=lambda: "run-partial-required",
    ).run(
        authoritative_goal=goal,
        parent_request_id=request_id,
        authority=authority,
    )

    assert outcome.is_success is True
    assert outcome.status == "incomplete"
    assert outcome.completed_subtasks == 1
    assert outcome.text == "Task a completed, but required task b failed."


@pytest.mark.asyncio
async def test_optional_worker_failure_does_not_make_run_incomplete(tmp_path):
    goal = "Complete required work"
    request_id = "req-optional-failure"
    authority = _authority()
    plan_payload = _plan_payload(
        goal=goal,
        parent_request_id=request_id,
        authority=authority,
        subtasks=[
            _subtask_payload("required"),
            _subtask_payload("optional", optional=True),
        ],
    )

    async def primary(spec):
        if spec.phase == "planning":
            return _primary_report(
                text=json.dumps(plan_payload), session_id="primary"
            )
        assert spec.phase == "assembly"
        assert '"completion_status": "completed"' in spec.prompt
        assert "optional check failed" in spec.prompt
        return _primary_report(
            text="Required work completed; the optional check failed.",
            session_id="primary-final",
        )

    async def worker(spec):
        if spec.subtask_id == "required":
            return _worker_success("required")
        return HERUltraInvocationResult(
            text="",
            is_success=False,
            error="optional check failed",
            error_type="permanent",
            retryable=False,
        )

    outcome = await HERUltraOrchestrator(
        config=_config(),
        ledger_root=tmp_path,
        primary_executor=primary,
        worker_executor=worker,
        run_id_factory=lambda: "run-optional-failure",
    ).run(
        authoritative_goal=goal,
        parent_request_id=request_id,
        authority=authority,
    )

    assert outcome.is_success is True
    assert outcome.status == "completed"
    assert outcome.completed_subtasks == 1


@pytest.mark.asyncio
async def test_primary_finalization_physical_failure_surfaces_exact_error(tmp_path):
    goal = "Always report worker failure"
    request_id = "req-finalization-fallback"
    authority = _authority()
    plan_payload = _plan_payload(
        goal=goal,
        parent_request_id=request_id,
        authority=authority,
        subtasks=[_subtask_payload("a")],
    )

    async def primary(spec):
        if spec.phase == "planning":
            return _primary_report(
                text=json.dumps(plan_payload), session_id="primary"
            )
        assert spec.phase == "failure_finalization"
        return HERUltraInvocationResult(
            text="",
            is_success=False,
            error="503 Service Unavailable: final renderer unavailable",
            error_type="provider_unavailable",
            terminal_kind="provider_error",
            message_origin="provider",
            exit_reasoning_status="failed_physical",
            checkpoint_preserved=True,
            session_id="primary",
        )

    async def worker(_spec):
        return HERUltraInvocationResult(
            text="",
            is_success=False,
            error="worker timed out",
            error_type="timeout",
            retryable=False,
        )

    outcome = await HERUltraOrchestrator(
        config=_config(),
        ledger_root=tmp_path,
        primary_executor=primary,
        worker_executor=worker,
        run_id_factory=lambda: "run-finalization-fallback",
    ).run(
        authoritative_goal=goal,
        parent_request_id=request_id,
        authority=authority,
    )

    assert outcome.is_success is False
    assert outcome.status == "failed"
    assert outcome.primary_session_id == "primary"
    assert outcome.text == ""
    assert outcome.error == "503 Service Unavailable: final renderer unavailable"
    assert outcome.terminal_kind == "provider_error"
    assert outcome.message_origin == "provider"
    assert outcome.exit_reasoning_status == "failed_physical"
    assert outcome.checkpoint_preserved is True
    state = json.loads(
        (tmp_path / "run-finalization-fallback" / "state.json").read_text(
            encoding="utf-8"
        )
    )
    assert state["status"] == "failed"
    assert state["primary"]["state"] == "failed"


@pytest.mark.asyncio
async def test_direct_plan_records_user_facing_answer_in_primary_session(tmp_path):
    goal = "Simple answer"
    request_id = "req-direct"
    authority = _authority()
    plan_payload = _plan_payload(
        goal=goal,
        parent_request_id=request_id,
        authority=authority,
        subtasks=[],
        direct_response="direct answer",
    )
    worker_called = False
    phases = []
    commentary_facts = []
    events = []

    async def primary(spec):
        phases.append((spec.phase, spec.resume_session_id))
        if spec.phase == "planning":
            return _primary_report(
                text=json.dumps(plan_payload), session_id="primary"
            )
        assert spec.phase == "direct_response"
        assert goal in spec.prompt
        assert "Momo persona" in spec.prompt
        assert "direct answer" in spec.prompt
        return _primary_report(
            text="persona-rendered direct answer", session_id="primary-direct"
        )

    async def worker(_spec):
        nonlocal worker_called
        worker_called = True
        raise AssertionError("worker must not run")

    async def render_commentary(facts):
        commentary_facts.append(dict(facts))
        return "must not be delivered"

    async def capture(event):
        events.append(event)

    outcome = await HERUltraOrchestrator(
        config=_config(),
        ledger_root=tmp_path,
        primary_executor=primary,
        worker_executor=worker,
        run_id_factory=lambda: "run-direct",
        persona_guidance="Use the Momo persona.",
        persona_commentary_renderer=render_commentary,
        on_stream_event=capture,
    ).run(
        authoritative_goal=goal,
        parent_request_id=request_id,
        authority=authority,
    )

    assert outcome.is_success is True
    assert outcome.text == "persona-rendered direct answer"
    assert worker_called is False
    assert phases == [("planning", ""), ("direct_response", "primary")]
    assert commentary_facts == []
    assert not [event for event in events if event.delivery_class == "user_commentary"]


def test_planning_prompt_contains_exact_authoritative_goal(tmp_path):
    goal = "帮我扫描整个磁盘，找找有没有垃圾可以清理"
    orchestrator = HERUltraOrchestrator(
        config=_config(),
        ledger_root=tmp_path,
        primary_executor=None,
        worker_executor=None,
    )

    prompt = orchestrator._planning_prompt(
        authoritative_goal=goal,
        parent_request_id="req-goal-binding",
        authority=_authority(),
        revision=1,
        prior_error="",
    )

    assert goal in prompt
    assert '"authoritative_goal"' in prompt
    assert "Do not ask for a goal that is present" in prompt


@pytest.mark.asyncio
async def test_invalid_plan_is_corrected_in_same_primary_session(tmp_path):
    goal = "Correct the plan"
    request_id = "req-correction"
    authority = _authority()
    valid = _plan_payload(
        goal=goal,
        parent_request_id=request_id,
        authority=authority,
        subtasks=[],
        direct_response="corrected draft",
    )
    calls = []

    async def primary(spec):
        calls.append((spec.phase, spec.resume_session_id, spec.prompt))
        if spec.phase == "planning":
            invalid = {**valid, "subtasks": "not-a-list"}
            return _primary_report(
                text=json.dumps(invalid), session_id="primary-invalid"
            )
        if spec.phase == "plan_correction":
            assert "subtasks must be a list" in spec.prompt
            return _primary_report(
                text=json.dumps(valid), session_id="primary-corrected"
            )
        assert spec.phase == "direct_response"
        return _primary_report(
            text="corrected final answer", session_id="primary-direct"
        )

    async def worker(_spec):
        raise AssertionError("worker must not run")

    outcome = await HERUltraOrchestrator(
        config=_config(),
        ledger_root=tmp_path,
        primary_executor=primary,
        worker_executor=worker,
        run_id_factory=lambda: "run-correction",
    ).run(
        authoritative_goal=goal,
        parent_request_id=request_id,
        authority=authority,
    )

    assert outcome.is_success is True
    assert outcome.text == "corrected final answer"
    assert outcome.plan_revision == 2
    assert [(phase, session) for phase, session, _prompt in calls] == [
        ("planning", ""),
        ("plan_correction", "primary-invalid"),
        ("direct_response", "primary-corrected"),
    ]


@pytest.mark.asyncio
async def test_exhausted_plan_corrections_end_with_primary_model_reasoning(tmp_path):
    calls = []

    async def primary(spec):
        calls.append((spec.phase, spec.resume_session_id, spec.prompt))
        if spec.phase in {"planning", "plan_correction"}:
            return _primary_report(
                text='{"subtasks":"still-invalid"}',
                session_id=f"primary-{spec.phase}",
            )
        assert spec.phase == "failure_finalization"
        assert "execution did not start" in spec.prompt
        assert "Investigate safely" in spec.prompt
        return _primary_report(
            text="The plan remained invalid, so execution did not start. I recommend narrowing the request.",
            session_id="primary-exit-report",
            exit_reasoning_status="completed",
            exit_reasoning_attempts=1,
        )

    async def worker(_spec):
        raise AssertionError("worker must not run without a valid plan")

    outcome = await HERUltraOrchestrator(
        config=_config(),
        ledger_root=tmp_path,
        primary_executor=primary,
        worker_executor=worker,
        run_id_factory=lambda: "run-invalid-plan-exit",
    ).run(
        authoritative_goal="Investigate safely",
        parent_request_id="req-invalid-plan-exit",
        authority=_authority(),
    )

    assert outcome.is_success is True
    assert outcome.status == "incomplete"
    assert outcome.text.startswith("The plan remained invalid")
    assert outcome.terminal_kind == "model_report"
    assert outcome.message_origin == "primary_model"
    assert outcome.exit_reasoning_status == "completed"
    assert [phase for phase, _session, _prompt in calls] == [
        "planning",
        "plan_correction",
        "failure_finalization",
    ]


@pytest.mark.asyncio
async def test_primary_provider_failure_is_not_treated_as_plan_correction(tmp_path):
    calls = []

    async def primary(spec):
        calls.append(spec.phase)
        return HERUltraInvocationResult(
            text="",
            is_success=False,
            error="503 Service Unavailable: provider unavailable",
            error_type="provider",
            retryable=True,
            terminal_kind="provider_error",
            message_origin="provider",
            exit_reasoning_status="failed_physical",
        )

    async def worker(_spec):
        raise AssertionError("worker must not run without a plan")

    outcome = await HERUltraOrchestrator(
        config=_config(),
        ledger_root=tmp_path,
        primary_executor=primary,
        worker_executor=worker,
        run_id_factory=lambda: "run-provider-failure",
    ).run(
        authoritative_goal="Investigate",
        parent_request_id="req-provider-failure",
        authority=_authority(),
    )

    assert outcome.is_success is False
    assert outcome.error == "503 Service Unavailable: provider unavailable"
    assert outcome.terminal_kind == "provider_error"
    assert outcome.message_origin == "provider"
    assert calls == ["planning"]


@pytest.mark.asyncio
async def test_pending_interaction_is_rendered_into_primary_session(tmp_path):
    goal = "Choose a deployment"
    request_id = "req-interaction"
    authority = _authority()
    plan_payload = _plan_payload(
        goal=goal,
        parent_request_id=request_id,
        authority=authority,
        subtasks=[_subtask_payload("inspect")],
    )
    phases = []

    async def primary(spec):
        phases.append((spec.phase, spec.resume_session_id))
        if spec.phase == "planning":
            return _primary_report(
                text=json.dumps(plan_payload), session_id="primary-planned"
            )
        assert spec.phase == "interaction"
        assert spec.resume_session_id == "primary-planned"
        assert '"interaction_id"' in spec.prompt
        return _primary_report(
            text="Choose A or B?",
            session_id="primary-question",
        )

    async def worker(spec):
        return HERUltraInvocationResult(
            text=json.dumps(
                {
                    "subtask_id": spec.subtask_id,
                    "status": "requires_user_input",
                    "claims": [],
                    "evidence": [],
                    "artifacts": [],
                    "validation": [],
                    "uncertainty": "Deployment target is unknown.",
                    "unresolved_items": ["target"],
                    "retry_safe": True,
                    "requires_user_input": {
                        "kind": "choice",
                        "prompt": "Choose A or B?",
                        "labels": ["A", "B"],
                    },
                }
            ),
            session_id="worker-inspect",
        )

    outcome = await HERUltraOrchestrator(
        config=_config(),
        ledger_root=tmp_path,
        primary_executor=primary,
        worker_executor=worker,
        run_id_factory=lambda: "run-interaction",
    ).run(
        authoritative_goal=goal,
        parent_request_id=request_id,
        authority=authority,
    )

    assert outcome.status == "incomplete"
    assert outcome.is_success is True
    assert outcome.text == "Choose A or B?"
    assert outcome.primary_session_id == "primary-question"
    assert outcome.pending_interaction["kind"] == "choice"
    assert outcome.pending_interaction["labels"] == ["A", "B"]
    assert phases == [
        ("planning", ""),
        ("interaction", "primary-planned"),
    ]


@pytest.mark.asyncio
async def test_orchestrator_cancellation_rejects_late_worker_result(tmp_path):
    goal = "Wait then cancel"
    request_id = "req-cancel"
    authority = _authority()
    started = asyncio.Event()
    release = asyncio.Event()
    plan_payload = _plan_payload(
        goal=goal,
        parent_request_id=request_id,
        authority=authority,
        subtasks=[_subtask_payload("a")],
    )

    async def primary(spec):
        return _primary_report(
            text=json.dumps(plan_payload), session_id="primary"
        )

    async def worker(spec):
        started.set()
        await release.wait()
        return _worker_success(spec.subtask_id)

    orchestrator = HERUltraOrchestrator(
        config=_config(),
        ledger_root=tmp_path,
        primary_executor=primary,
        worker_executor=worker,
        run_id_factory=lambda: "run-cancel",
    )
    run_task = asyncio.create_task(
        orchestrator.run(
            authoritative_goal=goal,
            parent_request_id=request_id,
            authority=authority,
        )
    )
    await asyncio.wait_for(started.wait(), timeout=1)

    orchestrator.cancel("test stop")
    release.set()
    outcome = await asyncio.wait_for(run_task, timeout=1)

    assert outcome.status == "cancelled"
    assert outcome.is_success is False
    state: Mapping = json.loads(
        (tmp_path / "run-cancel" / "state.json").read_text(encoding="utf-8")
    )
    assert state["status"] == "cancelled"
    assert state["subtasks"]["a"]["state"] != "completed"


@pytest.mark.asyncio
async def test_orchestrator_cancel_interrupts_primary_planning(tmp_path):
    started = asyncio.Event()

    async def primary(_spec):
        started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async def worker(_spec):
        raise AssertionError("worker must not start")

    orchestrator = HERUltraOrchestrator(
        config=_config(),
        ledger_root=tmp_path,
        primary_executor=primary,
        worker_executor=worker,
        run_id_factory=lambda: "run-cancel-planning",
    )
    run_task = asyncio.create_task(
        orchestrator.run(
            authoritative_goal="cancel planning",
            parent_request_id="req-cancel-planning",
            authority=_authority(),
        )
    )
    await asyncio.wait_for(started.wait(), timeout=1)

    orchestrator.cancel("test stop")
    outcome = await asyncio.wait_for(run_task, timeout=1)

    assert outcome.status == "cancelled"
    assert outcome.error == "test stop"


def _claw_result(
    text: str,
    *,
    model: str,
    session_id: str,
    duration_ms: float = 1,
) -> ClawTaskResult:
    return ClawTaskResult(
        text=text,
        model=model,
        permission_mode="read-only",
        cwd="/workspace",
        returncode=0,
        duration_ms=duration_ms,
        stdout="",
        stderr="",
        json_data={"usage": {"input_tokens": 3, "output_tokens": 2}},
        tool_uses=[],
        tool_results=[],
        session_id=session_id,
        iterations=1,
        completion_status="completed",
        stop_reason="end_turn",
        terminal_kind="model_report",
        message_origin="primary_model",
        exit_reasoning_status="embedded",
        exit_reasoning_attempts=0,
    )


@pytest.mark.asyncio
async def test_her_adapter_ultra_returns_one_response_and_checkpoints_primary(tmp_path):
    persona_path = tmp_path / "SYSTEM.md"
    persona_path.write_text("Use the Momo persona.", encoding="utf-8")
    cfg = SimpleNamespace(
        name="test",
        workspace_dir=tmp_path,
        system_md=persona_path,
        model="deepseek/deepseek-v4-flash",
        extra={
            "effort": "ultra",
            "ultra": {
                "max_concurrent_subagents": 2,
                "allowed_models": ["deepseek/deepseek-v4-pro"],
            },
        },
        resolve_access_root=lambda: tmp_path,
    )
    adapter = HERAdapter(cfg, SimpleNamespace(), api_key="test-key")
    adapter._binary = tmp_path / "hashi-her"
    adapter._session_id = "primary-existing"
    authority = adapter._ultra_authority()
    plan = _plan_payload(
        goal="Investigate in parallel",
        parent_request_id="req-ultra-adapter",
        authority=authority,
        subtasks=[_subtask_payload("a"), _subtask_payload("b")],
    )
    plan["subtasks"][0]["model_class"] = "pro"
    calls = []

    async def run_task(prompt, **kwargs):
        calls.append((prompt, kwargs))
        request_id = kwargs["request_id"]
        if prompt.startswith("HER ULTRA COMMENTARY RENDERER"):
            return _claw_result(
                "Momo persona progress",
                model=kwargs.get("model_override") or cfg.model,
                session_id="persona-commentary",
            )
        if prompt.startswith("[HER Ultra Primary Planning Contract]"):
            return _claw_result(
                json.dumps(plan),
                model=kwargs["model_override"],
                session_id="primary-planned",
            )
        if prompt.startswith("[HER Ultra Isolated Sub-agent Task]"):
            subtask_id = request_id.rsplit(":", 3)[-3]
            await asyncio.sleep(0.01)
            return _claw_result(
                _worker_success(subtask_id, model=kwargs["model_override"]).text,
                model=kwargs["model_override"],
                session_id=f"worker-{subtask_id}",
            )
        assert prompt.startswith("[HER Ultra Primary Assembly Contract]")
        await kwargs["on_stream_event"](
            StreamEvent(
                kind=KIND_ACKNOWLEDGEMENT,
                summary="internal acknowledgement",
                event_id="internal-final",
                delivery_class=DELIVERY_FINAL,
            )
        )
        await kwargs["on_stream_event"](
            StreamEvent(
                kind=KIND_PROGRESS,
                summary="assembly progress",
                event_id="assembly-progress",
                delivery_class=DELIVERY_TECHNICAL,
            )
        )
        return _claw_result(
            "one assembled answer",
            model=kwargs["model_override"],
            session_id="primary-final",
        )

    adapter._run_task_async = run_task
    events = []

    async def capture(event):
        events.append(event)

    response = await adapter.generate_response(
        "Investigate in parallel",
        "req-ultra-adapter",
        on_stream_event=capture,
    )

    assert response.is_success is True
    assert response.text == "one assembled answer"
    assert response.stream_metadata["claw_execution_effort"] == "ultra"
    assert response.stream_metadata["claw_inner_execution_effort"] == "high"
    assert response.stream_metadata["her_ultra"]["subtask_count"] == 2
    assert response.stream_metadata["her_ultra"]["completed_subtasks"] == 2
    assert response.stream_metadata["terminal_kind"] == "model_report"
    assert response.stream_metadata["message_origin"] == "primary_model"
    assert response.stream_metadata["exit_reasoning_status"] == "embedded"
    assert response.usage.input_tokens == 12
    assert response.usage.output_tokens == 8
    assert adapter._session_id == "primary-final"
    assert not adapter._ultra_runs

    planning_call = next(
        kwargs
        for prompt, kwargs in calls
        if prompt.startswith("[HER Ultra Primary Planning Contract]")
    )
    assembly_call = next(
        kwargs
        for prompt, kwargs in calls
        if prompt.startswith("[HER Ultra Primary Assembly Contract]")
    )
    worker_calls = [
        kwargs
        for prompt, kwargs in calls
        if prompt.startswith("[HER Ultra Isolated Sub-agent Task]")
    ]
    assert planning_call["resume"] == "primary-existing"
    assert planning_call["on_stream_event"] is not None
    assert planning_call["allowed_tools_override"] == []
    assert assembly_call["resume"] == "primary-planned"
    assert assembly_call["on_stream_event"] is not None
    assert assembly_call["permission_mode_override"] == "read-only"
    assert assembly_call["allowed_tools_override"] == []
    assert all(call["resume"] is None for call in worker_calls)
    assert all(call["track_session_identity"] is False for call in worker_calls)
    assert all(
        call["permission_mode_override"] == "workspace-write" for call in worker_calls
    )
    assert {call["model_override"] for call in worker_calls} == {
        "deepseek/deepseek-v4-flash",
        "deepseek/deepseek-v4-pro",
    }
    assert all(
        call["task_env_overrides"]["CLAW_EXECUTION_EFFORT"] == "high"
        for call in worker_calls
    )
    assert all(
        kwargs["task_env_overrides"]["CLAW_TASK_PLANNING"] == "0"
        for _prompt, kwargs in calls
    )
    persona_events = [
        event for event in events if event.delivery_class == "user_commentary"
    ]
    assert persona_events
    assert all(event.summary == "Momo persona progress" for event in persona_events)
    assert any(event.delivery_class == "user_commentary" for event in events)
    assert "assembly-progress" in {event.event_id for event in events}
    assert "internal-final" not in {event.event_id for event in events}
    assert all(
        call["cwd_override"] == adapter.effective_workdir for call in worker_calls
    )
    run_id = response.stream_metadata["her_ultra"]["run_id"]
    state = json.loads(
        (
            tmp_path / "backend_state" / "her_ultra_runs" / run_id / "state.json"
        ).read_text(encoding="utf-8")
    )
    assert state["status"] == "completed"
    assert state["primary"]["state"] == "completed"
    assert state["primary"]["phase"] == "assembly"
    assert state["primary"]["completion_status"] == "completed"


@pytest.mark.asyncio
async def test_her_adapter_reports_all_worker_failures_without_backend_error(tmp_path):
    persona_path = tmp_path / "SYSTEM.md"
    persona_path.write_text("Use the Momo persona.", encoding="utf-8")
    cfg = SimpleNamespace(
        name="test",
        workspace_dir=tmp_path,
        system_md=persona_path,
        model="deepseek/deepseek-v4-pro",
        extra={"effort": "ultra"},
        resolve_access_root=lambda: tmp_path,
    )
    adapter = HERAdapter(cfg, SimpleNamespace(), api_key="test-key")
    adapter._binary = tmp_path / "hashi-her"
    adapter._session_id = "primary-existing"
    authority = adapter._ultra_authority()
    plan = _plan_payload(
        goal="Investigate one failing worker",
        parent_request_id="req-ultra-incomplete",
        authority=authority,
        subtasks=[_subtask_payload("a")],
    )
    calls = []

    async def run_task(prompt, **kwargs):
        calls.append((prompt, kwargs))
        if prompt.startswith("HER ULTRA COMMENTARY RENDERER"):
            return _claw_result(
                "Momo persona progress",
                model=kwargs.get("model_override") or cfg.model,
                session_id="persona-commentary",
            )
        if prompt.startswith("[HER Ultra Primary Planning Contract]"):
            return _claw_result(
                json.dumps(plan),
                model=kwargs["model_override"],
                session_id="primary-planned",
            )
        if prompt.startswith("[HER Ultra Isolated Sub-agent Task]"):
            raise ClawCommandError(
                "provider unavailable",
                returncode=1,
                parsed_error={
                    "error_kind": "provider_unavailable",
                    "retryable": False,
                },
            )
        assert prompt.startswith(
            "[HER Ultra Primary Failure Finalization Contract]"
        )
        assert kwargs["permission_mode_override"] == "read-only"
        assert kwargs["allowed_tools_override"] == []
        return _claw_result(
            "The required worker failed; the requested work is incomplete.",
            model=kwargs["model_override"],
            session_id="primary-final",
        )

    adapter._run_task_async = run_task

    response = await adapter.generate_response(
        "Investigate one failing worker",
        "req-ultra-incomplete",
    )

    assert response.is_success is True
    assert response.error is None
    assert response.text == (
        "The required worker failed; the requested work is incomplete."
    )
    assert response.stop_reason == "incomplete"
    assert response.stream_metadata["claw_completion_status"] == "incomplete"
    assert response.stream_metadata["her_ultra"]["status"] == "incomplete"
    assert response.stream_metadata["her_ultra"]["completed_subtasks"] == 0
    assert response.stream_metadata["terminal_kind"] == "model_report"
    assert response.stream_metadata["message_origin"] == "primary_model"
    assert adapter._session_id == "primary-final"
    assert any(
        prompt.startswith("[HER Ultra Primary Failure Finalization Contract]")
        for prompt, _kwargs in calls
    )


@pytest.mark.asyncio
async def test_her_adapter_ultra_primary_finalization_preserves_exact_provider_error(
    tmp_path,
):
    cfg = SimpleNamespace(
        name="test",
        workspace_dir=tmp_path,
        model="deepseek/deepseek-v4-pro",
        extra={"effort": "ultra"},
        resolve_access_root=lambda: tmp_path,
    )
    adapter = HERAdapter(cfg, SimpleNamespace(), api_key="test-key")
    adapter._binary = tmp_path / "hashi-her"
    adapter._session_id = "primary-existing"
    adapter._persist_session_identity()
    authority = adapter._ultra_authority()
    plan = _plan_payload(
        goal="Investigate one failing worker",
        parent_request_id="req-ultra-provider-error",
        authority=authority,
        subtasks=[_subtask_payload("a")],
    )
    provider_error = "503 Service Unavailable\nrequest_id=req_ultra_exact_123"

    async def run_task(prompt, **kwargs):
        if prompt.startswith("[HER Ultra Primary Planning Contract]"):
            return _claw_result(
                json.dumps(plan),
                model=kwargs["model_override"],
                session_id="primary-planned",
            )
        if prompt.startswith("[HER Ultra Isolated Sub-agent Task]"):
            raise ClawCommandError(
                "worker failed",
                returncode=1,
                parsed_error={"error_kind": "worker_error", "retryable": False},
            )
        assert prompt.startswith(
            "[HER Ultra Primary Failure Finalization Contract]"
        )
        raise ClawCommandError(
            provider_error,
            returncode=1,
            parsed_error={
                "error_message": provider_error,
                "terminal_kind": "provider_error",
                "message_origin": "provider",
                "exit_reasoning_status": "failed_physical",
                "checkpoint_preserved": True,
                "session_id": "primary-final-error",
                "model": cfg.model,
                "provider": "deepseek",
            },
        )

    adapter._run_task_async = run_task

    response = await adapter.generate_response(
        "Investigate one failing worker",
        "req-ultra-provider-error",
    )

    assert response.is_success is False
    assert response.text == ""
    assert response.error == provider_error
    assert response.stream_metadata["terminal_kind"] == "provider_error"
    assert response.stream_metadata["message_origin"] == "provider"
    assert response.stream_metadata["exit_reasoning_status"] == "failed_physical"
    assert response.stream_metadata["checkpoint_preserved"] is True
    assert adapter._session_id == "primary-final-error"


def test_her_ultra_effort_maps_cli_environment_to_inner_effort(tmp_path):
    cfg = SimpleNamespace(
        name="test",
        workspace_dir=tmp_path,
        model="deepseek/test",
        extra={"effort": "ultra", "ultra": {"primary_inner_effort": "xhigh"}},
        resolve_access_root=lambda: tmp_path,
    )
    adapter = HERAdapter(cfg, SimpleNamespace(), api_key="test-key")

    assert adapter.effort == "ultra"
    assert adapter._task_env()["CLAW_EXECUTION_EFFORT"] == "xhigh"
    assert adapter._task_env()["CLAW_MAX_TOOL_ITERATIONS"] == "192"
    assert adapter._ultra_config().subagent_timeout_sec is None


def test_her_ultra_inherits_parent_permission_and_access_root(tmp_path):
    cfg = SimpleNamespace(
        name="test",
        workspace_dir=tmp_path,
        model="deepseek/test",
        extra={"effort": "ultra", "permission_mode": "danger-full-access"},
        resolve_access_root=lambda: Path("/"),
    )
    adapter = HERAdapter(cfg, SimpleNamespace(), api_key="test-key")

    authority = adapter._ultra_authority()

    assert authority.permission_mode == "danger-full-access"
    assert authority.access_root == "/"
    assert authority.write_enabled is True
    assert authority.allowed_tools == ("*",)


@pytest.mark.asyncio
async def test_her_adapter_ultra_isolated_resume_does_not_mutate_primary_checkpoint(
    tmp_path,
):
    request_id = "req-ultra-isolated"
    runtime = SimpleNamespace(
        _request_meta_by_id={
            request_id: {
                "session_scope": "isolated_resume",
                "resume_session_id": "receipt-session",
            }
        }
    )
    system_md = tmp_path / "momo-system.md"
    system_md.write_text("Call the user 哥哥 and answer warmly.", encoding="utf-8")
    cfg = SimpleNamespace(
        name="test",
        workspace_dir=tmp_path,
        model="deepseek/test",
        system_md=system_md,
        extra={"effort": "ultra"},
        resolve_access_root=lambda: tmp_path,
        _hashi_runtime=runtime,
    )
    adapter = HERAdapter(cfg, SimpleNamespace(), api_key="test-key")
    adapter._binary = tmp_path / "hashi-her"
    adapter._session_id = "persistent-session"
    authority = adapter._ultra_authority()
    plan = _plan_payload(
        goal="Answer inside the isolated receipt session",
        parent_request_id=request_id,
        authority=authority,
        subtasks=[],
        direct_response="isolated answer",
    )
    calls = []

    async def run_task(prompt, **kwargs):
        calls.append((prompt, kwargs))
        if prompt.startswith("[HER Ultra Primary Planning Contract]"):
            return _claw_result(
                json.dumps(plan),
                model=kwargs["model_override"],
                session_id="isolated-planned",
            )
        assert prompt.startswith("[HER Ultra Primary Direct Response Contract]")
        assert "Call the user 哥哥 and answer warmly." in prompt
        return _claw_result(
            "persona-rendered isolated answer",
            model=kwargs["model_override"],
            session_id="isolated-direct",
        )

    adapter._run_task_async = run_task

    response = await adapter.generate_response(
        "Answer inside the isolated receipt session",
        request_id,
    )

    assert response.is_success is True
    assert response.text == "persona-rendered isolated answer"
    assert response.stream_metadata["her_session_scope"] == "isolated_resume"
    assert response.stream_metadata["her_session_id"] == "isolated-direct"
    assert response.stream_metadata["her_resumed_session"] is True
    assert calls[0][1]["resume"] == "receipt-session"
    assert len(calls) == 2
    assert all(call[1]["permission_mode_override"] == "read-only" for call in calls)
    assert all(call[1]["allowed_tools_override"] == [] for call in calls)
    assert adapter._session_id == "persistent-session"


@pytest.mark.asyncio
async def test_her_runner_rejects_dispatch_during_stop_with_typed_error(tmp_path):
    cfg = SimpleNamespace(
        name="test",
        workspace_dir=tmp_path,
        model="deepseek/test",
        extra={},
        resolve_access_root=lambda: tmp_path,
    )
    adapter = HERAdapter(cfg, SimpleNamespace(), api_key="test-key")
    adapter._binary = tmp_path / "hashi-her"
    adapter._stopping_active_processes = True

    with pytest.raises(ClawCommandError) as captured:
        await adapter._run_task_async(
            "must not start",
            resume=None,
            request_id="req-stopping",
        )

    assert captured.value.returncode == 1
