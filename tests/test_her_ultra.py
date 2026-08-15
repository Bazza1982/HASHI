from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from types import SimpleNamespace

import pytest

from adapters.her import ClawCommandError, ClawTaskResult, HERAdapter
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
    allowed_actions: tuple[str, ...] = ("read",),
) -> dict:
    return {
        "id": subtask_id,
        "title": f"Task {subtask_id}",
        "objective": f"Complete {subtask_id}",
        "depends_on": list(depends_on),
        "model_class": "current",
        "effort": "high",
        "allowed_actions": list(allowed_actions),
        "workspace_strategy": (
            "isolated_worktree" if "write" in allowed_actions else "shared_read_only"
        ),
        "retry_safe": "write" not in allowed_actions,
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


def test_extract_json_object_prefers_outer_latest_complete_object():
    text = 'diagnostic {"old": true}\n```json\n{"task": {"nested": 1}}\n```'

    assert extract_json_object(text) == {"task": {"nested": 1}}


def test_ultra_config_caps_concurrency_at_ten():
    config = HERUltraConfig.from_mapping(
        {"max_concurrent_subagents": 99, "write_tasks_enabled": "false"}
    )

    assert config.max_concurrent_subagents == 10
    assert config.write_tasks_enabled is False


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


def test_task_contract_fails_closed_on_authority_model_and_write_drift():
    goal = "Read only"
    request_id = "req-authority"
    authority = _authority()
    validator = HERUltraTaskContractValidator(_config(write_tasks_enabled=True))
    payload = _plan_payload(
        goal="different goal",
        parent_request_id=request_id,
        authority=authority,
        subtasks=[_subtask_payload("writer", allowed_actions=("write",))],
    )
    payload["subtasks"][0]["model"] = "not-allowed"

    with pytest.raises(HERUltraContractError) as captured:
        validator.validate_plan(
            payload,
            authoritative_goal=goal,
            parent_request_id=request_id,
            authority=authority,
            revision=1,
        )

    error = str(captured.value)
    assert "authoritative_goal" in error
    assert "mutating subtask" in error
    assert "model is not allowed" in error

    write_authority = _authority(write_enabled=True)
    write_payload = _plan_payload(
        goal=goal,
        parent_request_id=request_id,
        authority=write_authority,
        subtasks=[_subtask_payload("writer", allowed_actions=("write",))],
    )
    with pytest.raises(HERUltraContractError, match="worktree integration"):
        validator.validate_plan(
            write_payload,
            authoritative_goal=goal,
            parent_request_id=request_id,
            authority=write_authority,
            revision=1,
        )


def test_worker_result_requires_strict_evidence():
    subtask = HERUltraSubtask(
        subtask_id="a",
        title="A",
        objective="A",
        depends_on=(),
        model="",
        model_class="current",
        effort="high",
        allowed_actions=("read",),
        deliverables=("result",),
        acceptance=("evidence",),
        optional=False,
        retry_safe=True,
        workspace_strategy="shared_read_only",
    )
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
        strict=True,
    )

    assert result.status == "failed"
    assert result.error_type == "malformed_output"
    assert "claims must be a list" in result.error


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
            return HERUltraInvocationResult(
                text=json.dumps(plan_payload),
                session_id="primary-session",
                model="model-pro",
                input_tokens=20,
                output_tokens=10,
            )
        assert spec.phase == "assembly"
        assert spec.resume_session_id == "primary-session"
        assert '"a"' in spec.prompt and '"c"' in spec.prompt
        return HERUltraInvocationResult(
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

    orchestrator = HERUltraOrchestrator(
        config=_config(),
        ledger_root=tmp_path,
        primary_executor=primary,
        worker_executor=worker,
        on_stream_event=capture,
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
    assert all(event.delivery_class == DELIVERY_TECHNICAL for event in events)
    assert len({event.event_id for event in events}) == len(events)


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
            return HERUltraInvocationResult(
                text=json.dumps(plan_payload), session_id="primary"
            )
        return HERUltraInvocationResult(text="done", session_id="primary")

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

    async def primary(spec):
        return HERUltraInvocationResult(
            text=json.dumps(plan_payload), session_id="primary"
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

    assert outcome.is_success is False
    assert worker_calls == ["a"]
    state = json.loads(
        (tmp_path / "run-dependency" / "state.json").read_text(encoding="utf-8")
    )
    assert state["subtasks"]["b"]["state"] == "failed"
    assert state["subtasks"]["b"]["failed_dependencies"] == ["a"]


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

    async def primary(spec):
        phases.append((spec.phase, spec.resume_session_id))
        if spec.phase == "planning":
            return HERUltraInvocationResult(
                text=json.dumps(plan_payload), session_id="primary"
            )
        assert spec.phase == "direct_response"
        assert spec.resume_session_id == "primary"
        return HERUltraInvocationResult(text="direct answer", session_id="primary")

    async def worker(_spec):
        nonlocal worker_called
        worker_called = True
        raise AssertionError("worker must not run")

    outcome = await HERUltraOrchestrator(
        config=_config(),
        ledger_root=tmp_path,
        primary_executor=primary,
        worker_executor=worker,
        run_id_factory=lambda: "run-direct",
    ).run(
        authoritative_goal=goal,
        parent_request_id=request_id,
        authority=authority,
    )

    assert outcome.is_success is True
    assert outcome.text == "direct answer"
    assert worker_called is False
    assert phases == [("planning", ""), ("direct_response", "primary")]


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
            invalid = {**valid, "authoritative_goal": "wrong goal"}
            return HERUltraInvocationResult(
                text=json.dumps(invalid), session_id="primary-invalid"
            )
        if spec.phase == "plan_correction":
            assert "authoritative_goal does not exactly match" in spec.prompt
            return HERUltraInvocationResult(
                text=json.dumps(valid), session_id="primary-corrected"
            )
        assert spec.phase == "direct_response"
        return HERUltraInvocationResult(
            text="corrected answer", session_id="primary-final"
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
    assert outcome.text == "corrected answer"
    assert outcome.plan_revision == 2
    assert [(phase, session) for phase, session, _prompt in calls] == [
        ("planning", ""),
        ("plan_correction", "primary-invalid"),
        ("direct_response", "primary-corrected"),
    ]


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
            return HERUltraInvocationResult(
                text=json.dumps(plan_payload), session_id="primary-planned"
            )
        assert spec.phase == "interaction"
        assert spec.resume_session_id == "primary-planned"
        assert '"interaction_id"' in spec.prompt
        return HERUltraInvocationResult(
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
        return HERUltraInvocationResult(
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
    )


@pytest.mark.asyncio
async def test_her_adapter_ultra_returns_one_response_and_checkpoints_primary(tmp_path):
    cfg = SimpleNamespace(
        name="test",
        workspace_dir=tmp_path,
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
        if prompt.startswith("[HER Ultra Primary Planning Contract]"):
            return _claw_result(
                json.dumps(plan),
                model=kwargs["model_override"],
                session_id="primary-planned",
            )
        if prompt.startswith("[HER Ultra Isolated Sub-agent Contract]"):
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
    assert response.stream_metadata["claw_inner_execution_effort"] == "max+"
    assert response.stream_metadata["her_ultra"]["subtask_count"] == 2
    assert response.stream_metadata["her_ultra"]["completed_subtasks"] == 2
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
        if prompt.startswith("[HER Ultra Isolated Sub-agent Contract]")
    ]
    assert planning_call["resume"] == "primary-existing"
    assert planning_call["on_stream_event"] is None
    assert assembly_call["resume"] == "primary-planned"
    assert assembly_call["on_stream_event"] is not None
    assert all(call["resume"] is None for call in worker_calls)
    assert all(call["track_session_identity"] is False for call in worker_calls)
    assert all(call["permission_mode_override"] == "read-only" for call in worker_calls)
    assert {call["model_override"] for call in worker_calls} == {
        "deepseek/deepseek-v4-flash",
        "deepseek/deepseek-v4-pro",
    }
    assert all(
        call["task_env_overrides"]["CLAW_EXECUTION_EFFORT"] == "high"
        for call in worker_calls
    )
    assert all(event.delivery_class == DELIVERY_TECHNICAL for event in events)
    assert "assembly-progress" in {event.event_id for event in events}
    assert "internal-final" not in {event.event_id for event in events}
    run_id = response.stream_metadata["her_ultra"]["run_id"]
    state = json.loads(
        (
            tmp_path / "backend_state" / "her_ultra_runs" / run_id / "state.json"
        ).read_text(encoding="utf-8")
    )
    assert state["status"] == "completed"


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
    cfg = SimpleNamespace(
        name="test",
        workspace_dir=tmp_path,
        model="deepseek/test",
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
        direct_response="draft",
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
        return _claw_result(
            "isolated answer",
            model=kwargs["model_override"],
            session_id="isolated-final",
        )

    adapter._run_task_async = run_task

    response = await adapter.generate_response(
        "Answer inside the isolated receipt session",
        request_id,
    )

    assert response.is_success is True
    assert response.text == "isolated answer"
    assert response.stream_metadata["her_session_scope"] == "isolated_resume"
    assert response.stream_metadata["her_session_id"] == "isolated-final"
    assert response.stream_metadata["her_resumed_session"] is True
    assert calls[0][1]["resume"] == "receipt-session"
    assert calls[1][1]["resume"] == "isolated-planned"
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
