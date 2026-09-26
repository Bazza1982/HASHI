from __future__ import annotations

import asyncio

import pytest

from orchestrator.her_v2.audit import AuditPersistenceError
from orchestrator.her_v2.checkpoint import (
    CHECKPOINT_ELAPSED_THRESHOLD_S,
    CHECKPOINT_RESULT_THRESHOLD,
    CheckpointInfrastructureInterruption,
    CompulsoryReplanCoordinator,
    ReplanCompletionInterruption,
    ReplanDirective,
)
from orchestrator.her_v2.config import HERv2Config, HERv2ConfigurationError
from orchestrator.her_v2.interfaces import StructuredOutputError
from orchestrator.her_v2.models import (
    ReplanningOutcome,
    Stage,
    StageResponse,
    ToolEvidenceReceipt,
    ToolReceiptStatus,
)
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
