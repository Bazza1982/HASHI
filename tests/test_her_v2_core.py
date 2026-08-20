from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from orchestrator.her_v2.audit import AuditPersistenceError, DurableAuditLog
from orchestrator.her_v2.config import (
    HERv2Config,
    HERv2ConfigurationError,
    ProviderProfile,
)
from orchestrator.her_v2.ledger import (
    ExecutionLedger,
    LedgerInvariantError,
    LedgerStore,
)
from orchestrator.her_v2.lifecycle import LifecycleMachine, LifecycleViolation
from orchestrator.her_v2.models import (
    Effort,
    ExecutionDisposition,
    LifecycleState,
    ReviewOutcome,
    Stage,
    TerminalState,
    TriageClassification,
)
from orchestrator.her_v2.policy import resolve_policy, terminal_for_execution
from orchestrator.her_v2.progress import ProgressTracker
from orchestrator.her_v2.structured import extract_json_object


def _profiles():
    return {
        name: {
            "engine": "provider-api",
            "model": f"model-{name}",
            "reasoning": "provider-setting",
        }
        for name in ("lightweight", "triage", "premium", "reviewer", "orchestrator")
    }


def test_material_acceptance_scenarios_have_required_traceability_metadata():
    required = {
        "test_id",
        "title",
        "design_requirement_id",
        "design_intention",
        "invariant_protected",
        "risk_or_failure_mode",
        "test_level",
        "user_request",
        "classification_input_or_result",
        "her_effort",
        "initial_conditions",
        "injected_disturbance",
        "required_behaviours",
        "prohibited_behaviours",
        "permitted_terminal_states",
        "required_terminal_state",
        "ledger_expectations",
        "logging_and_reasoning_expectations",
        "user_facing_expectations",
        "reason_this_test_matters",
    }
    path = Path(__file__).parent / "fixtures" / "her_v2_architecture_acceptance.yaml"
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    scenarios = payload["scenarios"]

    assert payload["format"] == "her-v2-architecture-acceptance-v1"
    assert len(scenarios) >= 10
    assert len({item["test_id"] for item in scenarios}) == len(scenarios)
    for scenario in scenarios:
        assert required <= scenario.keys(), scenario["test_id"]
        assert scenario["pytest_node_ids"], scenario["test_id"]
        assert scenario["required_behaviours"], scenario["test_id"]
        assert scenario["prohibited_behaviours"], scenario["test_id"]
        assert scenario["required_terminal_state"] in scenario["permitted_terminal_states"]


@pytest.mark.parametrize("source,target", sorted(LifecycleMachine.allowed_edges(), key=lambda edge: (edge[0].value, edge[1].value)))
def test_lifecycle_accepts_every_declared_edge(source, target):
    machine = LifecycleMachine(source)
    assert machine.transition(target) is target


@pytest.mark.parametrize(
    "source,target",
    [
        (LifecycleState.RECEIVED, LifecycleState.EXECUTING),
        (LifecycleState.TRIAGED, LifecycleState.REVIEWING),
        (LifecycleState.PLANNED, LifecycleState.FINALISING),
        (LifecycleState.EXECUTION_COMPLETED, LifecycleState.REPLANNING),
        (LifecycleState.COMPLETED, LifecycleState.ERROR),
    ],
)
def test_lifecycle_violation_is_not_silently_repaired(source, target):
    machine = LifecycleMachine(source)
    with pytest.raises(LifecycleViolation):
        machine.transition(target)
    expected = source if source is LifecycleState.COMPLETED else LifecycleState.ERROR
    assert machine.state is expected


def test_triage_classification_is_immutable_and_plan_replacement_is_replan_only():
    ledger = ExecutionLedger("turn-1", "request:1", "goal:1")
    ledger.record_triage(TriageClassification.COMPLEX_TASK)
    with pytest.raises(LedgerInvariantError, match="immutable"):
        ledger.record_triage(TriageClassification.SIMPLE_TASK)

    ledger.transition(LifecycleState.PLANNED)
    ledger.activate_plan("plan-v1")
    with pytest.raises(LedgerInvariantError, match="Replanning"):
        ledger.activate_plan("plan-v2", replacement=True)
    ledger.transition(LifecycleState.EXECUTING)
    ledger.transition(LifecycleState.REPLANNING)
    ledger.activate_plan("plan-v2", replacement=True)
    assert ledger.classification is TriageClassification.COMPLEX_TASK
    assert ledger.plan_id == "plan-v2"


def test_ledger_snapshot_is_minimal_and_caps_log_references():
    ledger = ExecutionLedger("turn-1", "request:1", "goal:1")
    for number in range(40):
        ledger.add_log_ref(f"log:{number}")
    assert set(ledger.to_dict()) == {
        "format",
        "turn_id",
        "request_ref",
        "goal_ref",
        "status",
        "classification",
        "plan_id",
        "last_update",
        "log_refs",
        "terminal_reason",
    }
    assert len(ledger.log_refs) == 32
    assert "prompt" not in ledger.to_dict()
    assert "reasoning" not in ledger.to_dict()


def test_restart_reconciliation_marks_incomplete_turn_error_without_resuming(tmp_path):
    store = LedgerStore(tmp_path / "ledgers")
    incomplete = ExecutionLedger("old", "request:old", "goal:old")
    incomplete.record_triage(TriageClassification.SIMPLE_TASK)
    incomplete.transition(LifecycleState.EXECUTING)
    complete = ExecutionLedger("done", "request:done", "goal:done")
    complete.record_triage(TriageClassification.DIRECT_RESPONSE)
    complete.transition(LifecycleState.FINALISING)
    complete.transition(LifecycleState.COMPLETED)
    store.save(incomplete)
    store.save(complete)

    reconciled = store.reconcile_interrupted()

    assert [item.turn_id for item in reconciled] == ["old"]
    assert store.load("old").status is LifecycleState.ERROR
    assert store.load("old").terminal_reason == "unexpected_process_interruption"
    assert store.load("done").status is LifecycleState.COMPLETED


@pytest.mark.parametrize(
    "effort,planning,replanning,review,replans,reviews",
    [
        (Effort.LOW, False, False, False, 0, 0),
        (Effort.MEDIUM, True, False, False, 0, 0),
        (Effort.HIGH, True, True, False, 50, 0),
        (Effort.XHIGH, True, True, True, 100, 1),
        (Effort.MAX, True, True, True, 200, 3),
    ],
)
def test_effort_is_orchestration_policy_not_provider_reasoning(
    effort, planning, replanning, review, replans, reviews
):
    policy = resolve_policy(effort, replan_limit=replans, review_limit=reviews)
    assert (policy.planning, policy.replanning, policy.review) == (
        planning,
        replanning,
        review,
    )
    assert policy.max_replans == replans
    assert policy.max_reviews == reviews


@pytest.mark.parametrize(
    "disposition,review,limited,expected",
    [
        (ExecutionDisposition.COMPLETED, None, False, TerminalState.COMPLETED),
        (
            ExecutionDisposition.COMPLETED_WITH_LIMITATIONS,
            None,
            False,
            TerminalState.COMPLETED_WITH_LIMITATIONS,
        ),
        (ExecutionDisposition.FAILED, None, False, TerminalState.FAILED),
        (ExecutionDisposition.ABANDONED, None, False, TerminalState.ABANDONED),
        (
            ExecutionDisposition.COMPLETED,
            ReviewOutcome.CONDITIONAL_PASS,
            False,
            TerminalState.COMPLETED_WITH_LIMITATIONS,
        ),
        (
            ExecutionDisposition.COMPLETED,
            ReviewOutcome.FAIL,
            False,
            TerminalState.COMPLETED_WITH_LIMITATIONS,
        ),
        (
            ExecutionDisposition.COMPLETED,
            ReviewOutcome.PASS,
            True,
            TerminalState.COMPLETED_WITH_LIMITATIONS,
        ),
    ],
)
def test_terminal_truth_table(disposition, review, limited, expected):
    assert (
        terminal_for_execution(
            disposition, review_outcome=review, material_limitations=limited
        )
        is expected
    )


def test_provider_profiles_are_configured_and_cannot_recurse_into_her():
    config = HERv2Config.from_mapping({"profiles": _profiles()})
    assert config.shadow_mode is False
    assert config.profile_for(Stage.TRIAGE).model == "model-triage"
    assert config.profile_for(Stage.TRIAGE).reasoning == "provider-setting"
    assert config.execution_profile_for(TriageClassification.SIMPLE_TASK).name == "lightweight"
    with pytest.raises(HERv2ConfigurationError, match="non-HER"):
        ProviderProfile("bad", "her-v2", "recursive")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("shadow_mode", "false"),
        ("shadow_mode", True),
        ("meditation_enabled", 1),
        ("audit_failure_terminal", "COMPLETED"),
    ],
)
def test_safety_configuration_rejects_ambiguous_or_unsafe_values(field, value):
    with pytest.raises(HERv2ConfigurationError):
        HERv2Config.from_mapping({"profiles": _profiles(), field: value})


def test_structured_parser_accepts_prose_wrapper_but_not_missing_object():
    assert extract_json_object('Result follows: {"classification":"DIRECT_RESPONSE"}.')[
        "classification"
    ] == "DIRECT_RESPONSE"
    with pytest.raises(Exception, match="no valid JSON"):
        extract_json_object("classification is direct")


class _MemoryWriter:
    def __init__(self, *, fail=False):
        self.fail = fail
        self.rows = []

    def append(self, record):
        if self.fail:
            raise OSError("injected write failure")
        self.rows.append(dict(record))
        return f"memory:{record['event_id']}"


def _audit_append(log):
    return log.append(
        event_id="event-1",
        turn_id="turn-1",
        request_ref="request-1",
        stage="triage",
        role="triage",
        event="completed",
        provider="provider-api",
        model="model-triage",
        payload={"api_key": "secret-value", "safe": "kept"},
    )


def test_audit_falls_back_durably_and_redacts_secrets():
    primary = _MemoryWriter(fail=True)
    fallback = _MemoryWriter()
    log = DurableAuditLog(primary_writer=primary, fallback_writer=fallback)

    assert _audit_append(log) == "memory:event-1"
    assert fallback.rows[0]["payload"] == {
        "api_key": "[REDACTED]",
        "safe": "kept",
    }
    assert _audit_append(log) == "hashi-log:deduplicated:event-1"
    assert len(fallback.rows) == 1


def test_total_audit_failure_is_a_hard_boundary():
    log = DurableAuditLog(
        primary_writer=_MemoryWriter(fail=True),
        fallback_writer=_MemoryWriter(fail=True),
    )
    with pytest.raises(AuditPersistenceError):
        _audit_append(log)


def test_fallback_replay_is_deduplicated(tmp_path):
    primary_path = tmp_path / "primary.jsonl"
    fallback_path = tmp_path / "fallback.jsonl"
    failing = _MemoryWriter(fail=True)
    log = DurableAuditLog(
        primary_path=primary_path,
        fallback_path=fallback_path,
        primary_writer=failing,
    )
    _audit_append(log)
    assert fallback_path.exists()

    healthy = DurableAuditLog(primary_path, fallback_path)
    assert healthy.replay_fallback() == 1
    assert healthy.replay_fallback() == 0
    rows = [json.loads(line) for line in primary_path.read_text().splitlines()]
    assert [row["event_id"] for row in rows] == ["event-1"]


def test_reasoning_unavailability_is_explicitly_audited():
    primary = _MemoryWriter()
    log = DurableAuditLog(primary_writer=primary, fallback_writer=_MemoryWriter())
    log.record_reasoning(
        event_id="reason-1",
        turn_id="turn-1",
        request_ref="request-1",
        stage="triage",
        role="triage",
        provider="provider-api",
        model="model",
        attempt=1,
        plan_id=None,
        trace=None,
    )
    assert primary.rows[0]["payload"] == {"availability": "unavailable"}


def test_reasoning_audit_redacts_secret_shaped_text_without_hiding_token_counts():
    primary = _MemoryWriter()
    log = DurableAuditLog(primary_writer=primary, fallback_writer=_MemoryWriter())
    log.append(
        event_id="reason-secret",
        turn_id="turn-1",
        request_ref="request-1",
        stage="execution",
        role="primary",
        event="reasoning_trace",
        payload={
            "trace": "Authorization: Bearer abcdefghijklmnop and sk-abcdefghijklmnop",
            "thinking_tokens": 42,
        },
    )
    payload = primary.rows[0]["payload"]
    assert "abcdefghijklmnop" not in payload["trace"]
    assert payload["thinking_tokens"] == 42


def test_only_new_meaningful_progress_resets_idle_clock():
    now = [0.0]
    tracker = ProgressTracker(clock=lambda: now[0])
    now[0] = 5.0
    assert tracker.record("commentary", "working") is True
    now[0] = 9.0
    assert tracker.record("commentary", "working") is False
    assert tracker.idle_for() == 4.0
    assert tracker.record("ledger", "timestamp-only", meaningful=False) is False
    now[0] = 11.0
    assert tracker.expired(6.0) is True
