from __future__ import annotations

import asyncio
import json
from collections import deque

import pytest

from orchestrator.her_v2.audit import AuditPersistenceError, DurableAuditLog
from orchestrator.her_v2.checkpoint import (
    CHECKPOINT_ELAPSED_THRESHOLD_S,
    CHECKPOINT_RESULT_THRESHOLD,
    CheckpointInfrastructureInterruption,
    CompulsoryReplanCoordinator,
    ReplanCompletionInterruption,
    ReplanDirective,
)
from orchestrator.her_v2.commentary import RecordingCommentaryPort
from orchestrator.her_v2.config import HERv2Config, HERv2ConfigurationError
from orchestrator.her_v2.interfaces import RecordingDelivery, StructuredOutputError
from orchestrator.her_v2.ledger import LedgerStore
from orchestrator.her_v2.models import (
    ReplanningOutcome,
    Stage,
    StageRequest,
    StageResponse,
    ToolEvidenceReceipt,
    ToolReceiptStatus,
)
from orchestrator.her_v2.runtime import HERv2Runtime
from orchestrator.her_v2.structured import parse_replanning


def test_optional_checkpoint_assessor_stage_is_removed():
    with pytest.raises(ValueError, match="checkpoint"):
        Stage("checkpoint")


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
    read_only: bool = False,
    details: dict | None = None,
    invocation_id: str = "turn:execution:1",
) -> ToolEvidenceReceipt:
    return ToolEvidenceReceipt(
        evidence_ref=f"receipt:{index}",
        stage=Stage.EXECUTION,
        invocation_id=invocation_id,
        attempt=1,
        tool_call_id=f"call-{index}",
        tool_name="test_tool",
        status=status,
        read_only=read_only,
        completed=completed,
        output_sha256=f"sha256-{index}",
        details=details or {},
    )


def _replan_outcome(
    *,
    percent: int = 50,
    changed: bool = False,
    commentary: str = "",
) -> ReplanningOutcome:
    return ReplanningOutcome(
        plan={
            "plan": ["Inspect", "Implement", "Verify"],
            "success_criteria": ["The authorised result is verified"],
        },
        completion_percent=percent,
        completion_basis="Current receipts show bounded progress against the goal.",
        plan_changed=changed,
        change_reason=("New evidence invalidated the old route." if changed else ""),
        next_step=(
            "Enter Review or Finalisation."
            if percent == 100
            else "Continue the remaining authorised work."
        ),
        commentary=commentary,
    )


def _directive(snapshot, *, percent: int = 50) -> ReplanDirective:
    return ReplanDirective(
        checkpoint_id=snapshot.checkpoint_id,
        outcome=_replan_outcome(percent=percent),
        active_plan_id=f"{snapshot.cycle_id}:plan:next",
    )


async def _continue_replan(snapshot):
    return _directive(snapshot)


def _valid_replan_data(**overrides):
    data = {
        "plan": ["Inspect", "Implement", "Verify"],
        "success_criteria": ["The authorised result is verified"],
        "completion_percent": 60,
        "completion_basis": "Six of ten acceptance facts are established.",
        "plan_changed": False,
        "change_reason": None,
        "next_step": "Continue the remaining authorised work.",
        "commentary": (
            "Progress is 60%. The plan is unchanged. "
            "Next: Continue the remaining authorised work."
        ),
    }
    data.update(overrides)
    return data


def test_replanning_three_question_contract_is_strict_and_commentary_can_fallback():
    result = parse_replanning(StageResponse(data=_valid_replan_data()))
    assert result.completion_percent == 60
    assert result.plan_changed is False

    fallback = parse_replanning(StageResponse(data=_valid_replan_data(commentary=None)))
    assert fallback.commentary == ""

    invalid_cases = [
        ({"completion_percent": True}, "integer"),
        ({"completion_percent": -1}, "0 through 100"),
        ({"completion_percent": 101}, "0 through 100"),
        ({"completion_basis": ""}, "completion_basis"),
        ({"plan_changed": "false"}, "boolean"),
        ({"plan_changed": True, "change_reason": None}, "change_reason"),
        ({"plan_changed": False, "change_reason": "invented"}, "must not"),
        ({"next_step": ""}, "next_step"),
        ({"success_criteria": []}, "success_criteria"),
        ({"goal": "replacement goal"}, "cannot replace"),
        (
            {
                "sub_agents": [
                    {
                        "id": "worker",
                        "task": "Inspect",
                        "profile": "lightweight",
                        "tools": [],
                        "allow_side_effects": False,
                    }
                ],
                "parallel_groups": [["missing"]],
            },
            "unknown sub-agent assignment IDs",
        ),
        (
            {
                "sub_agents": [
                    {
                        "id": "worker",
                        "task": "Inspect",
                        "profile": "lightweight",
                        "tools": [],
                        "allow_side_effects": False,
                    }
                ],
                "parallel_groups": [["worker"], ["worker"]],
            },
            "only one parallel group",
        ),
    ]
    for override, message in invalid_cases:
        with pytest.raises(StructuredOutputError, match=message):
            parse_replanning(StageResponse(data=_valid_replan_data(**override)))


def test_legacy_replan_count_limits_are_rejected_configuration():
    raw = _runtime_mapping()
    raw["replan_limits"] = {"high": 1}
    with pytest.raises(HERv2ConfigurationError, match="replan_limits"):
        HERv2Config.from_mapping(raw)


@pytest.mark.asyncio
async def test_not_due_at_nine_results_and_299_999_seconds():
    clock = ControlledClock()
    calls = 0

    async def evaluator(snapshot):
        nonlocal calls
        calls += 1
        return _directive(snapshot)

    coordinator = CompulsoryReplanCoordinator(
        cycle_id="cycle-not-due", evaluator=evaluator, clock=clock
    )
    for index in range(1, CHECKPOINT_RESULT_THRESHOLD):
        admission = await coordinator.before_tool(
            tool_name="test_tool", arguments={}, tool_call_id=str(index)
        )
        assert admission.admitted
        assert await coordinator.after_tool(admission, _receipt(index)) is None

    clock.value = CHECKPOINT_ELAPSED_THRESHOLD_S - 0.001
    admission = await coordinator.before_tool(
        tool_name="test_tool", arguments={}, tool_call_id="pending"
    )
    assert admission.admitted
    assert calls == 0
    await coordinator.abandon_tool(admission)
    await coordinator.close()


@pytest.mark.asyncio
async def test_tenth_result_forces_one_replan_and_resets_the_window():
    snapshots = []

    async def evaluator(snapshot):
        snapshots.append(snapshot)
        return _directive(snapshot)

    coordinator = CompulsoryReplanCoordinator(
        cycle_id="cycle-count", evaluator=evaluator, clock=ControlledClock()
    )
    directive = None
    for index in range(1, 11):
        admission = await coordinator.before_tool(
            tool_name="test_tool", arguments={}, tool_call_id=str(index)
        )
        directive = await coordinator.after_tool(
            admission,
            _receipt(index),
            result_summary=f"result-{index}",
        )

    assert isinstance(directive, ReplanDirective)
    assert len(snapshots) == 1
    assert snapshots[0].trigger_reasons == ("completed_result_count",)
    assert snapshots[0].completed_result_count == 10
    assert snapshots[0].boundary_kind == "completed_tool_result"
    assert coordinator.completed_result_count == 0

    eleventh = await coordinator.before_tool(
        tool_name="test_tool", arguments={}, tool_call_id="11"
    )
    assert eleventh.admitted
    await coordinator.abandon_tool(eleventh)
    await coordinator.close()


@pytest.mark.asyncio
async def test_exact_300_seconds_forces_replan_before_tool_admission():
    clock = ControlledClock()
    snapshots = []

    async def evaluator(snapshot):
        snapshots.append(snapshot)
        return _directive(snapshot)

    coordinator = CompulsoryReplanCoordinator(
        cycle_id="cycle-time", evaluator=evaluator, clock=clock
    )
    clock.value = CHECKPOINT_ELAPSED_THRESHOLD_S
    admission = await coordinator.before_tool(
        tool_name="file_write",
        arguments={"path": "secret"},
        tool_call_id="next",
    )

    assert admission.admitted is False
    assert admission.directive is not None
    assert snapshots[0].trigger_reasons == ("elapsed_time",)
    assert snapshots[0].prospective_action["tool_name"] == "file_write"
    assert "secret" not in repr(snapshots[0].prospective_action)
    await coordinator.close()


@pytest.mark.asyncio
async def test_count_and_time_due_coalesce_without_catch_up():
    clock = ControlledClock()
    snapshots = []

    async def evaluator(snapshot):
        snapshots.append(snapshot)
        return _directive(snapshot)

    coordinator = CompulsoryReplanCoordinator(
        cycle_id="cycle-coalesce", evaluator=evaluator, clock=clock
    )
    for index in range(1, 11):
        await coordinator.record_immediate_result(_receipt(index))
    clock.value = 900.0
    first = await coordinator.before_tool(
        tool_name="test_tool", arguments={}, tool_call_id="first"
    )
    assert first.admitted is False
    assert snapshots[0].trigger_reasons == (
        "completed_result_count",
        "elapsed_time",
    )

    second = await coordinator.before_tool(
        tool_name="test_tool", arguments={}, tool_call_id="second"
    )
    assert second.admitted
    assert len(snapshots) == 1
    await coordinator.abandon_tool(second)
    await coordinator.close()


@pytest.mark.asyncio
async def test_completed_errors_and_denials_count_but_incomplete_and_duplicates_do_not():
    coordinator = CompulsoryReplanCoordinator(
        cycle_id="cycle-status",
        evaluator=_continue_replan,
        clock=ControlledClock(),
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
async def test_replan_gets_bounded_latest_evidence_but_audit_payload_excludes_raw_output():
    snapshots = []
    audit_payloads = []

    async def evaluator(snapshot):
        snapshots.append(snapshot)
        return _directive(snapshot)

    coordinator = CompulsoryReplanCoordinator(
        cycle_id="cycle-evidence",
        evaluator=evaluator,
        observer=lambda _event, payload: audit_payloads.append(dict(payload)),
        clock=ControlledClock(),
    )
    for index in range(1, 11):
        await coordinator.record_immediate_result(
            _receipt(index),
            result_summary=("TOP_SECRET_VALUE" if index == 10 else f"result-{index}"),
        )
    admission = await coordinator.before_tool(
        tool_name="next", arguments={}, tool_call_id="next"
    )
    assert admission.admitted is False
    assert "TOP_SECRET_VALUE" in repr(snapshots[0].replan_payload())
    assert "TOP_SECRET_VALUE" not in repr(audit_payloads)
    await coordinator.close()


@pytest.mark.asyncio
async def test_active_tool_crossing_five_minutes_finishes_before_replan():
    clock = ControlledClock()
    evaluator_started = asyncio.Event()

    async def evaluator(snapshot):
        evaluator_started.set()
        assert snapshot.completed_result_count == 1
        return _directive(snapshot)

    coordinator = CompulsoryReplanCoordinator(
        cycle_id="cycle-active", evaluator=evaluator, clock=clock
    )
    admission = await coordinator.before_tool(
        tool_name="long_tool", arguments={}, tool_call_id="long"
    )
    assert admission.admitted
    clock.value = 301.0
    directive = await coordinator.after_tool(admission, _receipt(1))
    assert evaluator_started.is_set()
    assert directive is not None
    await coordinator.close()


@pytest.mark.asyncio
async def test_parallel_results_elect_one_replan_and_all_waiters_receive_directive():
    calls = 0
    release = asyncio.Event()
    started = asyncio.Event()

    async def evaluator(snapshot):
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return _directive(snapshot)

    coordinator = CompulsoryReplanCoordinator(
        cycle_id="cycle-parallel", evaluator=evaluator, clock=ControlledClock()
    )
    for index in range(1, 9):
        await coordinator.record_immediate_result(_receipt(index))
    ninth = await coordinator.before_tool(
        tool_name="test_tool", arguments={}, tool_call_id="9"
    )
    tenth = await coordinator.before_tool(
        tool_name="test_tool", arguments={}, tool_call_id="10"
    )
    first = asyncio.create_task(coordinator.after_tool(ninth, _receipt(9)))
    second = asyncio.create_task(coordinator.after_tool(tenth, _receipt(10)))
    await started.wait()
    assert calls == 1
    release.set()
    directives = await asyncio.gather(first, second)
    assert sum(item is not None for item in directives) >= 1
    assert calls == 1
    await coordinator.close()


@pytest.mark.asyncio
async def test_completion_percent_100_uses_typed_execution_stop():
    async def evaluator(snapshot):
        return _directive(snapshot, percent=100)

    clock = ControlledClock()
    coordinator = CompulsoryReplanCoordinator(
        cycle_id="cycle-complete", evaluator=evaluator, clock=clock
    )
    clock.value = 300.0
    with pytest.raises(ReplanCompletionInterruption) as raised:
        await coordinator.before_tool(
            tool_name="unneeded", arguments={}, tool_call_id="blocked"
        )
    assert raised.value.directive.outcome.completion_percent == 100
    await coordinator.close()


@pytest.mark.asyncio
async def test_close_cancels_replanner_and_waiters_without_late_release():
    started = asyncio.Event()

    async def evaluator(_snapshot):
        started.set()
        await asyncio.Event().wait()
        raise AssertionError("closed Replanner must not complete")

    clock = ControlledClock()
    coordinator = CompulsoryReplanCoordinator(
        cycle_id="cycle-close",
        evaluator=evaluator,
        clock=clock,
    )
    clock.value = 300.0
    waiter = asyncio.create_task(
        coordinator.before_tool(
            tool_name="test_tool", arguments={}, tool_call_id="waiting"
        )
    )
    await started.wait()
    await coordinator.close()
    with pytest.raises(asyncio.CancelledError):
        await waiter


@pytest.mark.asyncio
async def test_audit_persistence_failure_preempts_replan_wait():
    def failing_observer(_event, _payload):
        raise AuditPersistenceError("replan audit unavailable")

    clock = ControlledClock()
    coordinator = CompulsoryReplanCoordinator(
        cycle_id="cycle-audit",
        evaluator=_continue_replan,
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


@pytest.mark.asyncio
async def test_compulsory_replan_has_no_count_ceiling():
    coordinator = CompulsoryReplanCoordinator(
        cycle_id="cycle-unbounded",
        evaluator=_continue_replan,
        clock=ControlledClock(),
    )
    receipt_index = 0
    for cycle in range(205):
        for _ in range(10):
            receipt_index += 1
            await coordinator.record_immediate_result(_receipt(receipt_index))
        admission = await coordinator.before_tool(
            tool_name="control_boundary",
            arguments={"cycle": cycle},
            tool_call_id=f"boundary-{cycle}",
        )
        assert admission.admitted is False
    assert coordinator.checkpoint_count == 205
    await coordinator.close()


def _runtime_mapping():
    return {
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


def _runtime_config() -> HERv2Config:
    return HERv2Config.from_mapping(_runtime_mapping())


def _review_pass(request) -> StageResponse:
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
            f"{prefix}:check",
            Stage.REVIEW,
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
            "outcome": "PASS",
            "summary": "The latest result passes independent Review.",
            "findings": [],
            "evidence_refs": [receipts[1].evidence_ref],
        },
        provider_attempt=request.attempt,
        evidence_refs=tuple(item.evidence_ref for item in receipts),
        tool_receipts=receipts,
    )


class ReplanJourneyProvider:
    def __init__(
        self,
        *,
        tool_results: int = 0,
        replans: list[dict] | None = None,
        clock: ControlledClock | None = None,
        advance_before_first_tool_to: float | None = None,
        advance_at_completion: list[float | None] | None = None,
        review: bool = False,
    ) -> None:
        self.tool_results = tool_results
        self.replans = deque(replans or [])
        self.clock = clock
        self.advance_before_first_tool_to = advance_before_first_tool_to
        self.advance_at_completion = deque(advance_at_completion or [])
        self.review = review
        self.requests = []
        self.execution_calls = 0
        self.executed_tools = 0
        self.control_directives = []
        self._receipt_serial = 0
        self._advanced_before_tool = False

    def tool_catalogue(self, *, allow_side_effects, delegated_tools=None):
        del allow_side_effects
        if delegated_tools is not None and "test_tool" not in delegated_tools:
            return ()
        return (
            {
                "type": "function",
                "function": {"name": "test_tool"},
                "hashi_read_only": True,
            },
        )

    async def invoke(self, profile, request):
        del profile
        self.requests.append(request)
        if request.stage is Stage.IMMEDIATE_RESPONSE:
            return StageResponse(data={"message": "Working."})
        if request.stage is Stage.TRIAGE:
            return StageResponse(
                data={
                    "classification": "COMPLEX_TASK",
                    "real_goal": request.goal,
                    "selected_strategy_cards": ["SIMPLE_QA"],
                    "relevant_habits": [],
                    "execution_brief": {
                        "strategy": "Execute and verify the authorised task.",
                        "stages": ["Execute", "Verify"],
                        "dependencies": [],
                        "verification": ["Verify the final state"],
                        "success_criteria": ["The authorised goal is complete"],
                        "replan_conditions": ["Evidence invalidates the approach"],
                    },
                    "clarification": None,
                }
            )
        if request.stage is Stage.PLANNING:
            return StageResponse(
                data={
                    "plan": ["Inspect", "Implement", "Verify"],
                    "success_criteria": ["The authorised result is verified"],
                }
            )
        if request.stage is Stage.REPLANNING:
            if not self.replans:
                raise AssertionError("unexpected compulsory Replanning invocation")
            return StageResponse(data=self.replans.popleft())
        if request.stage is Stage.EXECUTION:
            self.execution_calls += 1
            coordinator = request.checkpoint_coordinator
            eligible = request.effort.value in {"high", "xhigh", "max"}
            assert (coordinator is not None) is eligible
            receipts = []
            completed_this_call = 0
            while completed_this_call < self.tool_results:
                if (
                    coordinator is not None
                    and self.clock is not None
                    and self.advance_before_first_tool_to is not None
                    and not self._advanced_before_tool
                ):
                    self.clock.value = self.advance_before_first_tool_to
                    self._advanced_before_tool = True
                if coordinator is None:
                    admission = None
                else:
                    admission = await coordinator.before_tool(
                        tool_name="test_tool",
                        arguments={"index": self._receipt_serial + 1},
                        tool_call_id=f"call-{self._receipt_serial + 1}",
                    )
                    if not admission.admitted:
                        self.control_directives.append(admission.directive)
                        continue
                self._receipt_serial += 1
                self.executed_tools += 1
                completed_this_call += 1
                receipt = _receipt(
                    self._receipt_serial,
                    invocation_id=request.invocation_id,
                )
                receipts.append(receipt)
                if coordinator is not None:
                    directive = await coordinator.after_tool(
                        admission,
                        receipt,
                        result_summary=f"result-{self._receipt_serial}",
                    )
                    if directive is not None:
                        self.control_directives.append(directive)
            if self.advance_at_completion:
                target = self.advance_at_completion.popleft()
                if target is not None and self.clock is not None:
                    self.clock.value = target
            return StageResponse(
                data={
                    "disposition": "COMPLETED",
                    "summary": "Execution candidate completed.",
                    "work_performed": ["Completed the authorised execution work."],
                    "verification": ["Checked the execution candidate."],
                },
                evidence_refs=tuple(item.evidence_ref for item in receipts),
                tool_receipts=tuple(receipts),
            )
        if request.stage is Stage.REVIEW and self.review:
            return _review_pass(request)
        if request.stage is Stage.FINALISATION:
            execution = request.context.get("parsed_execution_result") or {}
            return StageResponse(
                data={
                    "execution_result": execution or None,
                    "final_message": execution.get("summary") or "Reported.",
                }
            )
        raise AssertionError(f"unexpected stage: {request.stage.value}")


def _journey_runtime(tmp_path, provider, *, clock=None, commentary=None):
    root = tmp_path / "replan-runtime"
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
@pytest.mark.parametrize("effort", ["low", "medium"])
async def test_low_and_medium_never_install_compulsory_replan(tmp_path, effort):
    provider = ReplanJourneyProvider(tool_results=10)
    result = await _journey_runtime(tmp_path, provider).run_turn(
        "Complete the authorised task", f"ineligible-{effort}", effort=effort
    )
    assert result.terminal_state.value == "COMPLETED"
    assert result.replan_count == 0
    assert result.checkpoint_count == 0
    execution = next(
        item for item in provider.requests if item.stage is Stage.EXECUTION
    )
    assert execution.checkpoint_coordinator is None


@pytest.mark.asyncio
@pytest.mark.parametrize("effort", ["high", "xhigh", "max"])
async def test_high_and_above_install_by_effort(tmp_path, effort):
    provider = ReplanJourneyProvider(tool_results=0, review=True)
    result = await _journey_runtime(tmp_path, provider).run_turn(
        "Complete the authorised task", f"eligible-{effort}", effort=effort
    )
    assert result.terminal_state.value == "COMPLETED"
    execution = next(
        item for item in provider.requests if item.stage is Stage.EXECUTION
    )
    assert execution.checkpoint_coordinator is not None
    assert result.replan_count == 0


@pytest.mark.asyncio
async def test_tenth_result_forces_replan_without_churning_unchanged_plan(
    tmp_path,
):
    replan = _valid_replan_data()
    provider = ReplanJourneyProvider(
        tool_results=10,
        replans=[replan],
    )
    commentary = RecordingCommentaryPort()
    result = await _journey_runtime(tmp_path, provider, commentary=commentary).run_turn(
        "Complete all acceptance criteria", "count-replan", effort="high"
    )

    assert result.terminal_state.value == "COMPLETED"
    assert result.replan_count == 1
    assert result.checkpoint_count == 1
    assert result.ledger["plan_id"].endswith(":plan:v1")
    assert provider.execution_calls == 1
    assert provider.executed_tools == 10
    assert len(provider.control_directives) == 1
    assert [(item.stage, item.text) for item in commentary.records] == [
        (Stage.REPLANNING, replan["commentary"])
    ]
    replan_request = next(
        item for item in provider.requests if item.stage is Stage.REPLANNING
    )
    forbidden_limits = {
        "deadline_s",
        "max_loops",
        "max_replans",
        "max_tokens",
        "max_turns",
        "replan_limit",
        "replan_limits",
        "time_budget_s",
        "timeout_s",
        "token_budget",
    }
    assert forbidden_limits.isdisjoint(replan_request.context)
    assert forbidden_limits.isdisjoint(StageRequest.__dataclass_fields__)
    assert set(replan_request.context) == {
        "active_plan",
        "available_execution_tools",
        "execution_allow_side_effects",
        "plan_edit_history",
        "real_goal",
        "relevant_habits",
        "workflow_state_and_evidence",
    }
    assert replan_request.context["available_execution_tools"] == [
        {
            "type": "function",
            "function": {"name": "test_tool"},
            "hashi_read_only": True,
        }
    ]
    workflow_evidence = replan_request.context["workflow_state_and_evidence"]
    assert forbidden_limits.isdisjoint(workflow_evidence)
    assert workflow_evidence["replan_trigger"]["cadence_triggered"] is True
    assert workflow_evidence["workflow_counters"]["completed_replans"] == 0

    rows = [
        json.loads(line)
        for line in (tmp_path / "replan-runtime" / "audit.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    transitions = [
        (row["payload"]["from"], row["payload"]["to"])
        for row in rows
        if row["event"] == "transition"
    ]
    assert ("EXECUTING", "REPLANNING") in transitions
    assert ("REPLANNING", "EXECUTING") in transitions
    assert [
        row["event"]
        for row in rows
        if row["event"] in {"replan_due", "replan_started", "replan_completed"}
    ] == ["replan_due", "replan_started", "replan_completed"]


@pytest.mark.asyncio
async def test_time_due_replan_rejects_pending_tool_then_retries_under_new_window(
    tmp_path,
):
    clock = ControlledClock()
    provider = ReplanJourneyProvider(
        tool_results=1,
        replans=[_valid_replan_data()],
        clock=clock,
        advance_before_first_tool_to=300.0,
    )
    result = await _journey_runtime(tmp_path, provider, clock=clock).run_turn(
        "Complete the timed task", "time-replan", effort="high"
    )
    assert result.terminal_state.value == "COMPLETED"
    assert result.replan_count == 1
    assert provider.executed_tools == 1
    assert provider.control_directives[0].outcome.completion_percent == 60


@pytest.mark.asyncio
async def test_tool_free_completion_boundary_cannot_bypass_due_replan(tmp_path):
    clock = ControlledClock()
    provider = ReplanJourneyProvider(
        tool_results=0,
        replans=[_valid_replan_data()],
        clock=clock,
        advance_at_completion=[300.0, None],
    )
    result = await _journey_runtime(tmp_path, provider, clock=clock).run_turn(
        "Complete the long analysis", "completion-boundary", effort="high"
    )
    assert result.terminal_state.value == "COMPLETED"
    assert result.replan_count == 1
    assert result.checkpoint_count == 1
    assert provider.execution_calls == 2
    second_execution = [
        item for item in provider.requests if item.stage is Stage.EXECUTION
    ][1]
    continuation = second_execution.context["replan_continuation"]
    assert continuation["completion_percent"] == 60
    assert (
        second_execution.context["continuation_rules"][
            "never_repeat_completed_side_effects_because_of_replanning"
        ]
        is True
    )


@pytest.mark.asyncio
async def test_replan_100_stops_more_tools_and_routes_directly_to_review(tmp_path):
    complete = _valid_replan_data(
        completion_percent=100,
        completion_basis="All authorised acceptance criteria are now satisfied.",
        next_step="Enter Review.",
        commentary="Progress is 100%. The plan is unchanged. Next: Enter Review.",
    )
    provider = ReplanJourneyProvider(
        tool_results=11,
        replans=[complete],
        review=True,
    )
    commentary = RecordingCommentaryPort()
    result = await _journey_runtime(tmp_path, provider, commentary=commentary).run_turn(
        "Complete and review the task", "complete-replan", effort="xhigh"
    )

    assert result.terminal_state.value == "COMPLETED"
    assert provider.executed_tools == 10
    assert provider.execution_calls == 1
    assert result.replan_count == 1
    assert result.review_count == 1
    assert any(item.stage is Stage.REVIEW for item in provider.requests)
    assert commentary.records[0].text.startswith("Progress is 100%")


@pytest.mark.asyncio
async def test_missing_model_commentary_uses_verified_deterministic_fallback_once(
    tmp_path,
):
    replan = _valid_replan_data(commentary=None)
    provider = ReplanJourneyProvider(tool_results=10, replans=[replan])
    commentary = RecordingCommentaryPort()
    result = await _journey_runtime(tmp_path, provider, commentary=commentary).run_turn(
        "Complete the task", "commentary-fallback", effort="high"
    )

    assert result.terminal_state.value == "COMPLETED"
    assert len(commentary.records) == 1
    message = commentary.records[0]
    assert "60%" in message.text
    assert "plan is unchanged" in message.text.lower()
    assert replan["next_step"] in message.text
    assert message.required_facts == (
        "60%",
        replan["next_step"],
        "plan is unchanged",
    )
    assert message.event_id.endswith(":checkpoint:1:commentary")


@pytest.mark.asyncio
async def test_replan_commentary_is_not_rejected_for_paraphrasing_structured_facts(
    tmp_path,
):
    replan = _valid_replan_data(
        plan=["Inspect", "Use the supported route", "Verify"],
        plan_changed=True,
        change_reason="The original route is no longer supported.",
        commentary=(
            "Progress is 60%. The original route is no longer supported. "
            "Next: Continue the remaining authorised work."
        ),
    )
    provider = ReplanJourneyProvider(tool_results=10, replans=[replan])
    commentary = RecordingCommentaryPort()

    result = await _journey_runtime(tmp_path, provider, commentary=commentary).run_turn(
        "Complete the task", "changed-status-fallback", effort="high"
    )

    assert result.terminal_state.value == "COMPLETED"
    assert len(commentary.records) == 1
    message = commentary.records[0]
    assert message.text == replan["commentary"]
    assert message.required_facts == (
        "60%",
        replan["next_step"],
        "plan changed",
        replan["change_reason"],
    )


@pytest.mark.asyncio
async def test_oversized_replan_update_uses_bounded_fallback_without_stopping_workflow(
    tmp_path,
):
    replan = _valid_replan_data(
        next_step="Continue safely from evidence. " * 1_000,
        commentary="Unbounded presentation text. " * 1_000,
    )
    provider = ReplanJourneyProvider(tool_results=10, replans=[replan])
    commentary = RecordingCommentaryPort()

    result = await _journey_runtime(tmp_path, provider, commentary=commentary).run_turn(
        "Complete the task", "bounded-replan-update", effort="high"
    )

    assert result.terminal_state.value == "COMPLETED"
    assert result.replan_count == 1
    assert len(commentary.records) == 1
    message = commentary.records[0]
    assert len(message.text) <= 4_000
    assert all(fact in message.text for fact in message.required_facts)


@pytest.mark.asyncio
async def test_replanning_provider_crossing_historical_clock_boundaries_is_not_timed_out(
    tmp_path,
):
    clock = ControlledClock()

    class SlowClockReplanProvider(ReplanJourneyProvider):
        async def invoke(self, profile, request):
            if request.stage is Stage.REPLANNING:
                clock.value = 1_200.0
            return await super().invoke(profile, request)

    provider = SlowClockReplanProvider(
        tool_results=10,
        replans=[_valid_replan_data()],
        clock=clock,
    )
    result = await _journey_runtime(tmp_path, provider, clock=clock).run_turn(
        "Complete without hidden Replan deadlines", "no-replan-timeout", effort="high"
    )
    assert result.terminal_state.value == "COMPLETED"
    assert result.replan_count == 1
