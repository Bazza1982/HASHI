from __future__ import annotations

import asyncio
import json

import pytest

from orchestrator.her_v2.checkpoint import (
    CHECKPOINT_ELAPSED_THRESHOLD_S,
    CHECKPOINT_RESULT_THRESHOLD,
    CheckpointInterruption,
    CheckpointInfrastructureInterruption,
    HighRiskCheckpointCoordinator,
)
from orchestrator.her_v2.audit import DurableAuditLog
from orchestrator.her_v2.audit import AuditPersistenceError
from orchestrator.her_v2.config import HERv2Config
from orchestrator.her_v2.commentary import RecordingCommentaryPort
from orchestrator.her_v2.interfaces import (
    ProviderFailureCode,
    RecordingDelivery,
    StageInvocationError,
    StructuredOutputError,
)
from orchestrator.her_v2.ledger import (
    ExecutionLedger,
    LedgerInvariantError,
    LedgerStore,
)
from orchestrator.her_v2.models import (
    CheckpointDecision,
    CheckpointFinding,
    CheckpointPolicy,
    Stage,
    StageResponse,
    ToolEvidenceReceipt,
    ToolReceiptStatus,
    TriageClassification,
)
from orchestrator.her_v2.runtime import HERv2Runtime
from orchestrator.her_v2.structured import parse_checkpoint, parse_triage


class ControlledClock:
    def __init__(self, value: float = 0.0) -> None:
        self.value = float(value)

    def __call__(self) -> float:
        return self.value


def _receipt(
    index: int,
    *,
    status: ToolReceiptStatus = ToolReceiptStatus.SUCCESS,
    completed: bool = True,
    details: dict | None = None,
) -> ToolEvidenceReceipt:
    return ToolEvidenceReceipt(
        evidence_ref=f"receipt:{index}",
        stage=Stage.EXECUTION,
        invocation_id="turn:execution:1",
        attempt=1,
        tool_call_id=f"call-{index}",
        tool_name="test_tool",
        status=status,
        read_only=False,
        completed=completed,
        output_sha256=f"sha256-{index}",
        details=details or {},
    )


async def _continue(_snapshot):
    return CheckpointFinding(CheckpointDecision.CONTINUE, "Safe to continue.")


def test_work_triage_requires_explicit_checkpoint_policy_and_high_risk_reason():
    with pytest.raises(StructuredOutputError, match="checkpoint_policy"):
        parse_triage(StageResponse(data={"classification": "COMPLEX_TASK"}))

    with pytest.raises(StructuredOutputError, match="checkpoint_reason"):
        parse_triage(
            StageResponse(
                data={
                    "classification": "SIMPLE_TASK",
                    "checkpoint_policy": "HIGH_RISK",
                }
            )
        )

    decision = parse_triage(
        StageResponse(
            data={
                "classification": "HIGH_VOLUME_TASK",
                "checkpoint_policy": "HIGH_RISK",
                "checkpoint_reason": "Production data may be irreversibly changed.",
            }
        )
    )
    assert decision.checkpoint_policy is CheckpointPolicy.HIGH_RISK
    assert decision.checkpoint_reason.startswith("Production data")


def test_non_work_triage_cannot_install_execution_checkpoint_policy():
    with pytest.raises(StructuredOutputError, match="non-work"):
        parse_triage(
            StageResponse(
                data={
                    "classification": "DIRECT_RESPONSE",
                    "checkpoint_policy": "HIGH_RISK",
                    "checkpoint_reason": "not executable",
                }
            )
        )

    decision = parse_triage(
        StageResponse(
            data={
                "classification": "DIRECT_RESPONSE",
                "checkpoint_policy": None,
                "checkpoint_reason": None,
            }
        )
    )
    assert decision.checkpoint_policy is None


def test_checkpoint_response_contract_is_strict():
    finding = parse_checkpoint(
        StageResponse(
            data={
                "decision": "USER_INPUT_REQUIRED",
                "summary": "Authority is missing.",
                "question": "May the production record be deleted?",
            }
        )
    )
    assert finding.decision is CheckpointDecision.USER_INPUT_REQUIRED

    with pytest.raises(StructuredOutputError, match="concrete question"):
        parse_checkpoint(
            StageResponse(
                data={
                    "decision": "USER_INPUT_REQUIRED",
                    "summary": "Authority is missing.",
                }
            )
        )

    with pytest.raises(StructuredOutputError, match="Only Checkpoint"):
        parse_checkpoint(
            StageResponse(
                data={
                    "decision": "CONTINUE",
                    "summary": "Safe.",
                    "question": "Should not be present?",
                }
            )
        )


def test_ledger_records_checkpoint_policy_atomically_and_immutably():
    ledger = ExecutionLedger("turn", "request", "goal")
    ledger.record_triage(
        TriageClassification.COMPLEX_TASK,
        checkpoint_policy=CheckpointPolicy.HIGH_RISK,
        checkpoint_reason="Production access controls may change.",
    )
    assert ledger.to_dict()["checkpoint_policy"] == "HIGH_RISK"
    assert ledger.to_dict()["checkpoint_reason"] == (
        "Production access controls may change."
    )
    ledger.assert_checkpoint_policy(
        CheckpointPolicy.HIGH_RISK,
        "Production access controls may change.",
    )
    with pytest.raises(LedgerInvariantError, match="immutable checkpoint"):
        ledger.assert_checkpoint_policy(CheckpointPolicy.STANDARD, "")

    with pytest.raises(LedgerInvariantError, match="unsupported HER v2 ledger format"):
        ExecutionLedger.from_dict(
            {
                "format": "her-v2-ledger-unknown",
                "turn_id": "turn",
                "request_ref": "request",
                "goal_ref": "goal",
                "status": "EXECUTING",
                "classification": "COMPLEX_TASK",
            }
        )


@pytest.mark.asyncio
async def test_not_due_at_nine_results_and_299_999_seconds():
    clock = ControlledClock()
    snapshots = []

    async def evaluator(snapshot):
        snapshots.append(snapshot)
        return await _continue(snapshot)

    coordinator = HighRiskCheckpointCoordinator(
        cycle_id="cycle-1", evaluator=evaluator, clock=clock
    )
    for index in range(1, CHECKPOINT_RESULT_THRESHOLD):
        admission = await coordinator.before_tool(
            tool_name="test_tool", arguments={"index": index}, tool_call_id=str(index)
        )
        await coordinator.after_tool(admission, _receipt(index))

    clock.value = CHECKPOINT_ELAPSED_THRESHOLD_S - 0.001
    admission = await coordinator.before_tool(
        tool_name="test_tool", arguments={}, tool_call_id="pending"
    )
    assert snapshots == []
    await coordinator.abandon_tool(admission)
    await coordinator.close()


@pytest.mark.asyncio
async def test_tenth_result_runs_one_checkpoint_before_result_boundary_releases():
    clock = ControlledClock()
    snapshots = []
    release = asyncio.Event()
    evaluator_started = asyncio.Event()

    async def evaluator(snapshot):
        snapshots.append(snapshot)
        evaluator_started.set()
        await release.wait()
        return CheckpointFinding(CheckpointDecision.CONTINUE, "Continue.")

    coordinator = HighRiskCheckpointCoordinator(
        cycle_id="cycle-count", evaluator=evaluator, clock=clock
    )
    for index in range(1, CHECKPOINT_RESULT_THRESHOLD):
        admission = await coordinator.before_tool(
            tool_name="test_tool", arguments={}, tool_call_id=str(index)
        )
        await coordinator.after_tool(admission, _receipt(index))

    tenth = await coordinator.before_tool(
        tool_name="test_tool", arguments={}, tool_call_id="10"
    )
    boundary = asyncio.create_task(coordinator.after_tool(tenth, _receipt(10)))
    await evaluator_started.wait()
    assert not boundary.done()
    assert snapshots[0].trigger_reasons == ("completed_result_count",)
    assert snapshots[0].completed_result_count == 10
    release.set()
    await boundary
    assert coordinator.checkpoint_count == 1
    assert coordinator.completed_result_count == 0
    await coordinator.close()


@pytest.mark.asyncio
async def test_exact_300_seconds_runs_checkpoint_before_new_tool_admission():
    clock = ControlledClock()
    snapshots = []

    async def evaluator(snapshot):
        snapshots.append(snapshot)
        return await _continue(snapshot)

    coordinator = HighRiskCheckpointCoordinator(
        cycle_id="cycle-time", evaluator=evaluator, clock=clock
    )
    clock.value = CHECKPOINT_ELAPSED_THRESHOLD_S
    admission = await coordinator.before_tool(
        tool_name="file_write", arguments={"path": "secret"}, tool_call_id="next"
    )
    assert len(snapshots) == 1
    assert snapshots[0].trigger_reasons == ("elapsed_time",)
    assert snapshots[0].prospective_action == {
        "tool_name": "file_write",
        "tool_call_id": "next",
        "argument_keys": ["path"],
        "arguments_sha256": snapshots[0].prospective_action["arguments_sha256"],
    }
    assert "secret" not in repr(snapshots[0].prospective_action)
    await coordinator.abandon_tool(admission)
    await coordinator.close()


@pytest.mark.asyncio
async def test_count_and_time_due_coalesce_and_continue_does_not_catch_up():
    clock = ControlledClock()
    snapshots = []

    async def evaluator(snapshot):
        snapshots.append(snapshot)
        return await _continue(snapshot)

    coordinator = HighRiskCheckpointCoordinator(
        cycle_id="cycle-coalesce", evaluator=evaluator, clock=clock
    )
    for index in range(1, 11):
        await coordinator.record_immediate_result(_receipt(index))
    clock.value = 900.0
    admission = await coordinator.before_tool(
        tool_name="test_tool", arguments={}, tool_call_id="next"
    )
    assert snapshots[0].trigger_reasons == (
        "completed_result_count",
        "elapsed_time",
    )
    await coordinator.abandon_tool(admission)

    second = await coordinator.before_tool(
        tool_name="test_tool", arguments={}, tool_call_id="same-time"
    )
    assert len(snapshots) == 1
    await coordinator.abandon_tool(second)
    await coordinator.close()


@pytest.mark.asyncio
async def test_immediate_denial_during_assessment_carries_into_fresh_window():
    snapshots = []
    evaluator_started = asyncio.Event()
    release_first = asyncio.Event()

    async def evaluator(snapshot):
        snapshots.append(snapshot)
        if len(snapshots) == 1:
            evaluator_started.set()
            await release_first.wait()
        return await _continue(snapshot)

    coordinator = HighRiskCheckpointCoordinator(
        cycle_id="cycle-concurrent-denial",
        evaluator=evaluator,
        clock=ControlledClock(),
    )
    for index in range(1, 10):
        await coordinator.record_immediate_result(_receipt(index))

    tenth = await coordinator.before_tool(
        tool_name="test_tool", arguments={}, tool_call_id="10"
    )
    boundary = asyncio.create_task(coordinator.after_tool(tenth, _receipt(10)))
    await evaluator_started.wait()
    await coordinator.record_immediate_result(
        _receipt(
            11,
            status=ToolReceiptStatus.FAILED,
            details={"control_disposition": "approval_required"},
        )
    )
    release_first.set()
    await boundary

    assert coordinator.completed_result_count == 1
    for index in range(12, 21):
        admission = await coordinator.before_tool(
            tool_name="test_tool", arguments={}, tool_call_id=str(index)
        )
        await coordinator.after_tool(admission, _receipt(index))

    assert len(snapshots) == 2
    assert snapshots[1].completed_result_count == 10
    assert snapshots[1].receipt_summaries[0]["evidence_ref"] == "receipt:11"
    assert (
        snapshots[1].receipt_summaries[0]["control_disposition"] == "approval_required"
    )
    await coordinator.close()


@pytest.mark.asyncio
async def test_terminal_checkpoint_still_retains_concurrent_immediate_denial_receipt():
    async def evaluator(_snapshot):
        return CheckpointFinding(CheckpointDecision.HALT, "Unsafe to continue.")

    clock = ControlledClock()
    coordinator = HighRiskCheckpointCoordinator(
        cycle_id="cycle-terminal-denial",
        evaluator=evaluator,
        clock=clock,
    )
    clock.value = CHECKPOINT_ELAPSED_THRESHOLD_S
    with pytest.raises(CheckpointInterruption):
        await coordinator.before_tool(
            tool_name="file_write", arguments={}, tool_call_id="blocked"
        )

    late_denial = _receipt(
        1,
        status=ToolReceiptStatus.FAILED,
        details={"control_disposition": "denied"},
    )
    with pytest.raises(CheckpointInterruption):
        await coordinator.record_immediate_result(late_denial)
    assert late_denial in coordinator.receipts
    await coordinator.close()


@pytest.mark.asyncio
async def test_completed_errors_and_denials_count_but_incomplete_and_duplicates_do_not():
    coordinator = HighRiskCheckpointCoordinator(
        cycle_id="cycle-status", evaluator=_continue, clock=ControlledClock()
    )
    await coordinator.record_immediate_result(
        _receipt(1, status=ToolReceiptStatus.FAILED)
    )
    await coordinator.record_immediate_result(
        _receipt(2, status=ToolReceiptStatus.CANCELLED, completed=False)
    )
    await coordinator.record_immediate_result(
        _receipt(1, status=ToolReceiptStatus.FAILED)
    )
    assert coordinator.completed_result_count == 1
    await coordinator.close()


@pytest.mark.asyncio
async def test_checkpoint_snapshot_metadata_is_bounded_and_excludes_raw_details():
    snapshots = []

    async def evaluator(snapshot):
        snapshots.append(snapshot)
        return await _continue(snapshot)

    coordinator = HighRiskCheckpointCoordinator(
        cycle_id="cycle-bounded",
        evaluator=evaluator,
        clock=ControlledClock(),
    )
    for index in range(1, 11):
        await coordinator.record_immediate_result(
            _receipt(
                index,
                details={"raw_output": "TOP_SECRET_VALUE"} if index == 1 else {},
            )
        )
    admission = await coordinator.before_tool(
        tool_name="x" * 1_000,
        arguments={"k" * 1_000: "TOP_SECRET_ARGUMENT"},
        tool_call_id="c" * 1_000,
    )

    snapshot = snapshots[0]
    assert "TOP_SECRET" not in repr(snapshot.as_payload())
    assert len(snapshot.prospective_action["tool_name"]) == 256
    assert len(snapshot.prospective_action["tool_call_id"]) == 256
    assert len(snapshot.prospective_action["argument_keys"][0]) == 256
    assert all(
        len(str(value)) <= 256
        for summary in snapshot.receipt_summaries
        for value in summary.values()
        if isinstance(value, str)
    )
    await coordinator.abandon_tool(admission)
    await coordinator.close()


@pytest.mark.asyncio
async def test_active_tool_crossing_five_minutes_is_not_cancelled_before_safe_boundary():
    clock = ControlledClock()
    snapshots = []

    async def evaluator(snapshot):
        snapshots.append(snapshot)
        return await _continue(snapshot)

    coordinator = HighRiskCheckpointCoordinator(
        cycle_id="cycle-active", evaluator=evaluator, clock=clock
    )
    admission = await coordinator.before_tool(
        tool_name="long_tool", arguments={}, tool_call_id="long"
    )
    clock.value = 301.0
    await coordinator.after_tool(admission, _receipt(1))
    assert len(snapshots) == 1
    assert snapshots[0].trigger_reasons == ("elapsed_time",)
    assert snapshots[0].completed_result_count == 1
    await coordinator.close()


@pytest.mark.asyncio
async def test_parallel_results_elect_exactly_one_checkpoint_leader():
    clock = ControlledClock()
    calls = 0
    release = asyncio.Event()
    evaluator_started = asyncio.Event()

    async def evaluator(snapshot):
        nonlocal calls
        calls += 1
        evaluator_started.set()
        await release.wait()
        return await _continue(snapshot)

    coordinator = HighRiskCheckpointCoordinator(
        cycle_id="cycle-parallel", evaluator=evaluator, clock=clock
    )
    for index in range(1, 9):
        admission = await coordinator.before_tool(
            tool_name="test_tool", arguments={}, tool_call_id=str(index)
        )
        await coordinator.after_tool(admission, _receipt(index))

    ninth = await coordinator.before_tool(
        tool_name="test_tool", arguments={}, tool_call_id="9"
    )
    tenth = await coordinator.before_tool(
        tool_name="test_tool", arguments={}, tool_call_id="10"
    )
    first = asyncio.create_task(coordinator.after_tool(ninth, _receipt(9)))
    second = asyncio.create_task(coordinator.after_tool(tenth, _receipt(10)))
    await evaluator_started.wait()
    assert calls == 1
    assert not first.done() or not second.done()
    release.set()
    await asyncio.gather(first, second)
    assert calls == 1
    await coordinator.close()


@pytest.mark.asyncio
async def test_user_input_checkpoint_uses_typed_non_exception_control_path():
    async def evaluator(_snapshot):
        return CheckpointFinding(
            CheckpointDecision.USER_INPUT_REQUIRED,
            "Production authority is missing.",
            "May the production record be deleted?",
        )

    clock = ControlledClock()
    coordinator = HighRiskCheckpointCoordinator(
        cycle_id="cycle-input", evaluator=evaluator, clock=clock
    )
    clock.value = 300.0
    with pytest.raises(CheckpointInterruption) as raised:
        await coordinator.before_tool(
            tool_name="delete", arguments={}, tool_call_id="pending"
        )
    assert raised.value.finding.decision is CheckpointDecision.USER_INPUT_REQUIRED
    assert raised.value.receipts == ()
    await coordinator.close()


@pytest.mark.asyncio
async def test_close_cancels_checkpoint_evaluator_and_all_waiters_without_release():
    clock = ControlledClock()
    evaluator_started = asyncio.Event()

    async def evaluator(_snapshot):
        evaluator_started.set()
        await asyncio.Event().wait()
        raise AssertionError("cancelled evaluator must not release admission")

    coordinator = HighRiskCheckpointCoordinator(
        cycle_id="cycle-cancel", evaluator=evaluator, clock=clock
    )
    clock.value = 300.0
    waiter = asyncio.create_task(
        coordinator.before_tool(
            tool_name="test_tool", arguments={}, tool_call_id="waiting"
        )
    )
    await evaluator_started.wait()
    await coordinator.close()

    with pytest.raises(asyncio.CancelledError):
        await waiter


@pytest.mark.asyncio
async def test_audit_persistence_failure_preempts_checkpoint_wait():
    clock = ControlledClock()

    def failing_observer(_event, _payload):
        raise AuditPersistenceError("checkpoint audit unavailable")

    coordinator = HighRiskCheckpointCoordinator(
        cycle_id="cycle-audit-failure",
        evaluator=_continue,
        observer=failing_observer,
        clock=clock,
    )
    clock.value = 300.0
    with pytest.raises(CheckpointInfrastructureInterruption) as raised:
        await coordinator.before_tool(
            tool_name="test_tool", arguments={}, tool_call_id="waiting"
        )
    assert isinstance(raised.value.cause, AuditPersistenceError)
    await coordinator.close()


def _runtime_config() -> HERv2Config:
    return HERv2Config.from_mapping(
        {
            "profiles": {
                name: {
                    "engine": "fake-api",
                    "model": f"model-{name}",
                    "reasoning": f"reasoning-{name}",
                }
                for name in (
                    "lightweight",
                    "triage",
                    "premium",
                    "reviewer",
                    "orchestrator",
                )
            },
            "user_idle_timeout_s": 10,
        }
    )


class CheckpointJourneyProvider:
    def __init__(
        self,
        *,
        checkpoint_policy: str,
        tool_results: int = 0,
        checkpoint_decision: str = "CONTINUE",
        checkpoint_question: str = "",
        checkpoint_failure: StageInvocationError | None = None,
        clock: ControlledClock | None = None,
        advance_tool_free_to: float | None = None,
    ) -> None:
        self.checkpoint_policy = checkpoint_policy
        self.tool_results = tool_results
        self.checkpoint_decision = checkpoint_decision
        self.checkpoint_question = checkpoint_question
        self.checkpoint_failure = checkpoint_failure
        self.clock = clock
        self.advance_tool_free_to = advance_tool_free_to
        self.requests = []

    async def invoke(self, profile, request):
        del profile
        self.requests.append(request)
        if request.stage is Stage.IMMEDIATE_RESPONSE:
            return StageResponse(data={"message": "Working."})
        if request.stage is Stage.TRIAGE:
            return StageResponse(
                data={
                    "classification": "COMPLEX_TASK",
                    "checkpoint_policy": self.checkpoint_policy,
                    "checkpoint_reason": (
                        "Production data may be irreversibly changed."
                        if self.checkpoint_policy == "HIGH_RISK"
                        else None
                    ),
                }
            )
        if request.stage is Stage.CHECKPOINT:
            assert request.allow_tools is False
            assert request.allow_side_effects is False
            assert request.checkpoint_coordinator is None
            if self.checkpoint_failure is not None:
                raise self.checkpoint_failure
            return StageResponse(
                data={
                    "decision": self.checkpoint_decision,
                    "summary": (
                        "Existing authority and evidence remain aligned."
                        if self.checkpoint_decision == "CONTINUE"
                        else "Further production work needs a control decision."
                    ),
                    "question": self.checkpoint_question or None,
                }
            )
        if request.stage is Stage.EXECUTION:
            if self.advance_tool_free_to is not None and self.clock is not None:
                self.clock.value = self.advance_tool_free_to
            receipts = []
            coordinator = request.checkpoint_coordinator
            if self.checkpoint_policy == "HIGH_RISK":
                assert coordinator is not None
            else:
                assert coordinator is None
            for index in range(1, self.tool_results + 1):
                assert coordinator is not None
                admission = await coordinator.before_tool(
                    tool_name="test_tool",
                    arguments={"index": index},
                    tool_call_id=f"call-{index}",
                )
                receipt = _receipt(index)
                receipts.append(receipt)
                await coordinator.after_tool(admission, receipt)
            return StageResponse(
                data={
                    "disposition": "COMPLETED",
                    "summary": "Execution completed.",
                },
                evidence_refs=tuple(item.evidence_ref for item in receipts),
                tool_receipts=tuple(receipts),
            )
        if request.stage is Stage.FINALISATION:
            execution = request.context.get("parsed_execution_result") or {}
            return StageResponse(
                data={
                    "report": (
                        execution.get("clarification")
                        or execution.get("summary")
                        or "Reported."
                    )
                }
            )
        raise AssertionError(f"unexpected stage: {request.stage.value}")


def _journey_runtime(tmp_path, provider, *, clock=None, commentary=None):
    root = tmp_path / "checkpoint-runtime"
    return HERv2Runtime(
        config=_runtime_config(),
        provider=provider,
        ledger_store=LedgerStore(root / "ledgers"),
        audit_log=DurableAuditLog(root / "audit.jsonl", root / "audit-fallback.jsonl"),
        delivery=RecordingDelivery(),
        commentary=commentary,
        checkpoint_clock=clock or ControlledClock(),
    )


@pytest.mark.asyncio
async def test_runtime_standard_risk_never_installs_periodic_checkpoint(tmp_path):
    provider = CheckpointJourneyProvider(
        checkpoint_policy="STANDARD",
        tool_results=0,
    )
    result = await _journey_runtime(tmp_path, provider).run_turn(
        "Inspect the normal workspace", "standard", effort="low"
    )
    assert result.terminal_state.value == "COMPLETED"
    assert result.checkpoint_count == 0
    assert all(request.stage is not Stage.CHECKPOINT for request in provider.requests)
    execution_request = next(
        request for request in provider.requests if request.stage is Stage.EXECUTION
    )
    assert execution_request.checkpoint_coordinator is None


@pytest.mark.asyncio
async def test_runtime_repairs_missing_work_risk_policy_without_defaulting(tmp_path):
    class RepairingTriageProvider(CheckpointJourneyProvider):
        def __init__(self):
            super().__init__(checkpoint_policy="STANDARD")
            self.triage_calls = 0

        async def invoke(self, profile, request):
            if request.stage is Stage.TRIAGE:
                self.requests.append(request)
                self.triage_calls += 1
                if self.triage_calls == 1:
                    return StageResponse(data={"classification": "COMPLEX_TASK"})
                assert "previous_structure_error" in request.context
                return StageResponse(
                    data={
                        "classification": "COMPLEX_TASK",
                        "checkpoint_policy": "STANDARD",
                    }
                )
            return await super().invoke(profile, request)

    provider = RepairingTriageProvider()
    result = await _journey_runtime(tmp_path, provider).run_turn(
        "Inspect the normal workspace", "repair-risk", effort="low"
    )

    assert result.terminal_state.value == "COMPLETED"
    assert result.ledger["checkpoint_policy"] == "STANDARD"
    assert provider.triage_calls == 2


@pytest.mark.asyncio
async def test_runtime_high_risk_short_completion_has_no_synthetic_checkpoint(tmp_path):
    provider = CheckpointJourneyProvider(
        checkpoint_policy="HIGH_RISK",
        tool_results=9,
    )
    result = await _journey_runtime(tmp_path, provider).run_turn(
        "Change production records", "short-high-risk", effort="low"
    )
    assert result.terminal_state.value == "COMPLETED"
    assert result.checkpoint_count == 0
    assert sum(request.stage is Stage.CHECKPOINT for request in provider.requests) == 0


@pytest.mark.asyncio
async def test_runtime_tenth_result_checkpoints_then_continues_without_loop_cap(
    tmp_path,
):
    provider = CheckpointJourneyProvider(
        checkpoint_policy="HIGH_RISK",
        tool_results=10,
    )
    runtime = _journey_runtime(tmp_path, provider)
    original_evaluator = runtime._evaluate_checkpoint
    progress_boundaries = []

    async def observe_progress(state, classification, snapshot):
        before = state.progress.last_progress_at
        finding = await original_evaluator(state, classification, snapshot)
        progress_boundaries.append((before, state.progress.last_progress_at))
        return finding

    runtime._evaluate_checkpoint = observe_progress
    result = await runtime.run_turn(
        "Change production records", "continue-high-risk", effort="low"
    )
    assert result.terminal_state.value == "COMPLETED"
    assert result.checkpoint_count == 1
    checkpoint_request = next(
        request for request in provider.requests if request.stage is Stage.CHECKPOINT
    )
    assert checkpoint_request.context["completed_result_count"] == 10
    assert checkpoint_request.context["trigger_reasons"] == ["completed_result_count"]
    assert checkpoint_request.role == "checkpoint_evaluator"
    assert checkpoint_request.allow_tools is False
    rows = [
        json.loads(line)
        for line in (tmp_path / "checkpoint-runtime" / "audit.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    checkpoint_control_events = [
        row["event"]
        for row in rows
        if row["event"]
        in {"checkpoint_due", "checkpoint_started", "checkpoint_completed"}
    ]
    assert checkpoint_control_events == [
        "checkpoint_due",
        "checkpoint_started",
        "checkpoint_completed",
    ]
    completed = next(row for row in rows if row["event"] == "checkpoint_completed")
    assert completed["payload"]["decision"] == "CONTINUE"
    assert completed["payload"]["completed_result_count"] == 10
    assert len(progress_boundaries) == 1
    assert progress_boundaries[0][1] == progress_boundaries[0][0]
    assert all(record.kind != "commentary" for record in result.delivery_records)


@pytest.mark.asyncio
async def test_provider_recovery_keeps_one_live_checkpoint_window(tmp_path):
    class RecoveringExecutionProvider(CheckpointJourneyProvider):
        def __init__(self):
            super().__init__(checkpoint_policy="HIGH_RISK")
            self.execution_coordinators = []

        async def invoke(self, profile, request):
            if request.stage is not Stage.EXECUTION:
                return await super().invoke(profile, request)
            del profile
            self.requests.append(request)
            coordinator = request.checkpoint_coordinator
            assert coordinator is not None
            self.execution_coordinators.append(coordinator)
            start = 1 if request.attempt == 1 else 6
            receipts = []
            for index in range(start, start + 5):
                request.provider_activity_callback(
                    {
                        "kind": "file_read",
                        "content": f"read-{index}",
                        "tool_name": "file_read",
                        "tool_read_only": True,
                    }
                )
                admission = await coordinator.before_tool(
                    tool_name="file_read",
                    arguments={"path": f"evidence-{index}"},
                    tool_call_id=f"call-{index}",
                )
                receipt = ToolEvidenceReceipt(
                    evidence_ref=f"retry-receipt:{request.attempt}:{index}",
                    stage=Stage.EXECUTION,
                    invocation_id=request.invocation_id,
                    attempt=request.attempt,
                    tool_call_id=f"call-{index}",
                    tool_name="file_read",
                    status=ToolReceiptStatus.SUCCESS,
                    read_only=True,
                    completed=True,
                    output_sha256=f"sha256-{index}",
                )
                receipts.append(receipt)
                await coordinator.after_tool(admission, receipt)
                request.provider_activity_callback(
                    {
                        "kind": "tool_end",
                        "content": f"read-{index}-complete",
                        "tool_name": "file_read",
                        "tool_read_only": True,
                    }
                )
            if request.attempt == 1:
                raise StageInvocationError(
                    "connection reset after completed read-only tools",
                    code=ProviderFailureCode.PROVIDER_CONNECTION_FAILED,
                    retryable=True,
                    human_description="The provider connection was reset.",
                )
            return StageResponse(
                data={
                    "disposition": "COMPLETED",
                    "summary": "Recovered execution completed.",
                },
                evidence_refs=tuple(item.evidence_ref for item in receipts),
                tool_receipts=tuple(receipts),
            )

    provider = RecoveringExecutionProvider()
    result = await _journey_runtime(tmp_path, provider).run_turn(
        "Read high-risk production evidence",
        "checkpoint-provider-recovery",
        effort="low",
    )

    assert result.terminal_state.value == "COMPLETED"
    assert len(provider.execution_coordinators) == 2
    assert provider.execution_coordinators[0] is provider.execution_coordinators[1]
    assert result.checkpoint_count == 1
    assert len(result.evidence_refs) == 10
    checkpoint_request = next(
        request for request in provider.requests if request.stage is Stage.CHECKPOINT
    )
    assert checkpoint_request.context["completed_result_count"] == 10


@pytest.mark.asyncio
async def test_runtime_tool_free_execution_beyond_300_seconds_gets_no_final_checkpoint(
    tmp_path,
):
    clock = ControlledClock()
    provider = CheckpointJourneyProvider(
        checkpoint_policy="HIGH_RISK",
        clock=clock,
        advance_tool_free_to=601.0,
    )
    result = await _journey_runtime(tmp_path, provider, clock=clock).run_turn(
        "Run a long production analysis", "tool-free-long", effort="low"
    )
    assert result.terminal_state.value == "COMPLETED"
    assert result.checkpoint_count == 0
    assert all(request.stage is not Stage.CHECKPOINT for request in provider.requests)


@pytest.mark.asyncio
async def test_runtime_time_due_checkpoint_runs_before_next_tool_admission(tmp_path):
    clock = ControlledClock()
    provider = CheckpointJourneyProvider(
        checkpoint_policy="HIGH_RISK",
        tool_results=1,
        clock=clock,
        advance_tool_free_to=300.0,
    )
    result = await _journey_runtime(tmp_path, provider, clock=clock).run_turn(
        "Change a production record", "time-due", effort="low"
    )
    assert result.terminal_state.value == "COMPLETED"
    assert result.checkpoint_count == 1
    checkpoint_request = next(
        request for request in provider.requests if request.stage is Stage.CHECKPOINT
    )
    assert checkpoint_request.context["completed_result_count"] == 0
    assert checkpoint_request.context["trigger_reasons"] == ["elapsed_time"]
    assert checkpoint_request.context["prospective_action"]["tool_name"] == (
        "test_tool"
    )
    assert checkpoint_request.context["prospective_action"]["argument_keys"] == [
        "index"
    ]
    assert "arguments" not in checkpoint_request.context["prospective_action"]


@pytest.mark.asyncio
async def test_runtime_checkpoint_user_input_stops_tools_and_delivers_one_question(
    tmp_path,
):
    question = "May the production record be deleted?"
    provider = CheckpointJourneyProvider(
        checkpoint_policy="HIGH_RISK",
        tool_results=11,
        checkpoint_decision="USER_INPUT_REQUIRED",
        checkpoint_question=question,
    )
    result = await _journey_runtime(tmp_path, provider).run_turn(
        "Delete production records", "checkpoint-input", effort="low"
    )
    assert result.terminal_state.value == "PENDING_USER_INPUT"
    assert result.checkpoint_count == 1
    assert result.text == question
    assert len(result.delivery_records) == 2  # acknowledgement plus clarification
    assert result.delivery_records[-1].kind == "clarification"
    assert result.delivery_records[-1].text == question
    assert result.evidence_refs == tuple(f"receipt:{index}" for index in range(1, 11))
    rows = [
        json.loads(line)
        for line in (tmp_path / "checkpoint-runtime" / "audit.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    interrupted = [
        row for row in rows if row["event"] == "checkpoint_interrupted_execution"
    ]
    assert len(interrupted) == 1
    assert interrupted[0]["payload"]["decision"] == "USER_INPUT_REQUIRED"
    assert interrupted[0]["payload"]["execution_replayed"] is False


@pytest.mark.asyncio
async def test_runtime_unavailable_checkpoint_fails_closed_with_partial_evidence(
    tmp_path,
):
    provider = CheckpointJourneyProvider(
        checkpoint_policy="HIGH_RISK",
        tool_results=11,
        checkpoint_failure=StageInvocationError(
            "checkpoint provider unavailable",
            retryable=False,
            human_description="Checkpoint provider unavailable.",
        ),
    )
    result = await _journey_runtime(tmp_path, provider).run_turn(
        "Change production records", "checkpoint-unavailable", effort="low"
    )
    assert result.terminal_state.value == "FAILED"
    assert result.checkpoint_count == 1
    assert len(result.evidence_refs) == 10
    assert any(
        "Checkpoint evaluator unavailable" in item for item in result.limitations
    )
    assert "further tool admission stopped" in result.text.lower()


@pytest.mark.asyncio
async def test_runtime_stop_cancels_checkpoint_evaluator_without_late_message(tmp_path):
    clock = ControlledClock()
    evaluator_started = asyncio.Event()

    class BlockingCheckpointProvider(CheckpointJourneyProvider):
        async def invoke(self, profile, request):
            if request.stage is Stage.CHECKPOINT:
                self.requests.append(request)
                evaluator_started.set()
                await asyncio.Event().wait()
                raise AssertionError("stopped checkpoint must not complete")
            if request.stage is Stage.EXECUTION:
                self.requests.append(request)
                clock.value = 300.0
                coordinator = request.checkpoint_coordinator
                assert coordinator is not None
                await coordinator.before_tool(
                    tool_name="test_tool",
                    arguments={},
                    tool_call_id="blocked-before-admission",
                )
                raise AssertionError("stopped admission must not resume")
            return await super().invoke(profile, request)

    provider = BlockingCheckpointProvider(checkpoint_policy="HIGH_RISK")
    runtime = _journey_runtime(tmp_path, provider, clock=clock)
    turn = asyncio.create_task(
        runtime.run_turn(
            "Change production records",
            "checkpoint-stop",
            effort="low",
            turn_id="checkpoint-stop-turn",
        )
    )
    await asyncio.wait_for(evaluator_started.wait(), timeout=1)
    assert await runtime.stop_turn("checkpoint-stop-turn", reason="USER_STOP") is True
    result = await asyncio.wait_for(turn, timeout=1)

    assert result.terminal_state.value == "STOPPED"
    assert all(record.kind != "clarification" for record in result.delivery_records)
    await asyncio.sleep(0)
    assert sum(request.stage is Stage.CHECKPOINT for request in provider.requests) == 1


def _review_response(request, outcome: str) -> StageResponse:
    prefix = request.invocation_id
    receipts = (
        ToolEvidenceReceipt(
            f"{prefix}:before",
            Stage.REVIEW,
            prefix,
            request.attempt,
            "before",
            "workspace_inspect",
            ToolReceiptStatus.SUCCESS,
            True,
            True,
            "before",
            {"operation": "snapshot", "snapshot_sha256": "stable"},
        ),
        ToolEvidenceReceipt(
            f"{prefix}:inspection",
            Stage.REVIEW,
            prefix,
            request.attempt,
            "inspection",
            "workspace_inspect",
            ToolReceiptStatus.SUCCESS,
            True,
            True,
            "inspection",
            {"operation": "diff", "exit_code": 0},
        ),
        ToolEvidenceReceipt(
            f"{prefix}:after",
            Stage.REVIEW,
            prefix,
            request.attempt,
            "after",
            "workspace_inspect",
            ToolReceiptStatus.SUCCESS,
            True,
            True,
            "after",
            {"operation": "snapshot", "snapshot_sha256": "stable"},
        ),
    )
    return StageResponse(
        data={
            "outcome": outcome,
            "summary": "Review result.",
            "findings": ["Remediate the production approach."]
            if outcome == "FAIL"
            else [],
            "evidence_refs": [receipts[1].evidence_ref],
        },
        provider_attempt=request.attempt,
        evidence_refs=tuple(item.evidence_ref for item in receipts),
        tool_receipts=receipts,
    )


def _verification_response(request, *, commentary: str = "") -> StageResponse:
    prefix = request.invocation_id
    receipts = (
        ToolEvidenceReceipt(
            f"{prefix}:before",
            Stage.VERIFICATION,
            prefix,
            request.attempt,
            "before",
            "workspace_inspect",
            ToolReceiptStatus.SUCCESS,
            True,
            True,
            "before",
            {"operation": "snapshot", "snapshot_sha256": "stable"},
        ),
        ToolEvidenceReceipt(
            f"{prefix}:check",
            Stage.VERIFICATION,
            prefix,
            request.attempt,
            "check",
            "workspace_inspect",
            ToolReceiptStatus.SUCCESS,
            True,
            True,
            "check",
            {"operation": "diff", "exit_code": 0},
        ),
        ToolEvidenceReceipt(
            f"{prefix}:after",
            Stage.VERIFICATION,
            prefix,
            request.attempt,
            "after",
            "workspace_inspect",
            ToolReceiptStatus.SUCCESS,
            True,
            True,
            "after",
            {"operation": "snapshot", "snapshot_sha256": "stable"},
        ),
    )
    data = {
        "outcome": "VERIFIED",
        "summary": "The latest state is verified.",
        "checks": [
            {
                "claim": "The latest state is correct",
                "verifiability": "VERIFIABLE",
                "result": "VERIFIED",
                "method": "workspace_diff",
                "evidence_refs": [receipts[1].evidence_ref],
                "observed": "The current workspace evidence passed.",
            }
        ],
        "evidence_refs": [receipts[1].evidence_ref],
    }
    if commentary:
        data["commentary"] = commentary
    return StageResponse(
        data=data,
        provider_attempt=request.attempt,
        evidence_refs=tuple(item.evidence_ref for item in receipts),
        tool_receipts=receipts,
    )


@pytest.mark.asyncio
async def test_review_remediation_starts_fresh_checkpoint_cycle_and_totals_both(
    tmp_path,
):
    class RemediationProvider(CheckpointJourneyProvider):
        def __init__(self):
            super().__init__(checkpoint_policy="HIGH_RISK", tool_results=10)
            self.review_calls = 0

        async def invoke(self, profile, request):
            if request.stage is Stage.PLANNING:
                self.requests.append(request)
                return StageResponse(data={"plan": ["Initial production approach"]})
            if request.stage is Stage.REVIEW:
                self.requests.append(request)
                self.review_calls += 1
                return _review_response(
                    request, "FAIL" if self.review_calls == 1 else "PASS"
                )
            if request.stage is Stage.REPLANNING:
                self.requests.append(request)
                return StageResponse(data={"plan": ["Remediated production approach"]})
            return await super().invoke(profile, request)

    provider = RemediationProvider()
    result = await _journey_runtime(tmp_path, provider).run_turn(
        "Change production records and review the result",
        "fresh-remediation-cycle",
        effort="xhigh",
    )

    assert result.terminal_state.value == "COMPLETED"
    assert result.review_count == 2
    assert result.replan_count == 1
    assert result.checkpoint_count == 2
    checkpoint_requests = [
        request for request in provider.requests if request.stage is Stage.CHECKPOINT
    ]
    assert len(checkpoint_requests) == 2
    assert [
        request.context["completed_result_count"] for request in checkpoint_requests
    ] == [10, 10]
    assert {
        request.context["execution_cycle_id"] for request in checkpoint_requests
    } == {
        f"{result.turn_id}:execution-cycle:1",
        f"{result.turn_id}:execution-cycle:2",
    }


@pytest.mark.asyncio
async def test_short_assured_high_risk_work_keeps_review_verification_and_commentary(
    tmp_path,
):
    verification_update = "The current production state is independently verified."

    class AssuredProvider(CheckpointJourneyProvider):
        async def invoke(self, profile, request):
            if request.stage is Stage.PLANNING:
                self.requests.append(request)
                return StageResponse(data={"plan": ["Complete the short change"]})
            if request.stage is Stage.REVIEW:
                self.requests.append(request)
                return _review_response(request, "PASS")
            if request.stage is Stage.VERIFICATION:
                self.requests.append(request)
                return _verification_response(request, commentary=verification_update)
            return await super().invoke(profile, request)

    provider = AssuredProvider(
        checkpoint_policy="HIGH_RISK",
        tool_results=0,
    )
    commentary = RecordingCommentaryPort()
    result = await _journey_runtime(tmp_path, provider, commentary=commentary).run_turn(
        "Make and assure one short production change",
        "short-assured-high-risk",
        effort="max",
    )

    assert result.terminal_state.value == "COMPLETED"
    assert result.checkpoint_count == 0
    assert result.review_count == 1
    assert result.verification_count == 1
    assert [(item.stage, item.text) for item in commentary.records] == [
        (Stage.VERIFICATION, verification_update)
    ]
