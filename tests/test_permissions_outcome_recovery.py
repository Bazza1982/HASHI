"""Unknown operations need verified, claim-bound outcome evidence."""
from dataclasses import replace

import pytest

from orchestrator.permissions.authority import (
    OperationConflict,
    OperationOutcomeReceipt,
    PermissionAuthority,
    PermissionDenied,
)
from orchestrator.permissions.models import ExecutionContext, normalize_scope
from tests.test_permissions_authority import AuthenticatedReceiptSource, Clock, approve


class ReceiptSource(AuthenticatedReceiptSource):
    def __init__(self):
        super().__init__()
        self.outcomes = {}

    def verify_outcome(self, reference):
        return self.outcomes.get(reference)

    def publish_outcome(self, reference, receipt):
        self.outcomes[reference] = receipt
        return reference


@pytest.fixture
def interrupted(tmp_path):
    clock = Clock()
    issuer = ReceiptSource()
    context = ExecutionContext(
        instance_id="instance", owner_id="owner", agent_id="agent", task_id="task",
        run_id="run", session_id="session", turn_id="turn", worker_generation="worker",
        context_generation="generation", execution_epoch="epoch", backend_id="herv3",
        executor_id="registry", policy_revision=1,
    )
    path = tmp_path / "permissions.sqlite"
    original = PermissionAuthority(path, verifier=issuer, clock=clock, monotonic=clock)
    original.open_task(context)
    scope = normalize_scope("file", str(tmp_path), ["read"], cwd=tmp_path)
    approve(original, issuer, clock, context, [scope])
    claim = original.prepare(context, "operation", "a" * 64, [scope])
    original.start(context, claim.claim_id)
    recovered = PermissionAuthority(path, verifier=issuer, clock=clock, monotonic=clock)
    receipt = OperationOutcomeReceipt(
        nonce="outcome-nonce", claim_id=claim.claim_id, operation_id="operation",
        arguments_digest="a" * 64, instance_id=context.instance_id, task_id=context.task_id,
        execution_epoch=context.execution_epoch, observer_id=context.executor_id,
        outcome="succeeded", result_digest="b" * 64, expires_at=clock.value + 30,
    )
    return original, recovered, issuer, clock, context, scope, claim, receipt


def test_verified_outcome_resolves_unknown_without_repeating_operation(interrupted):
    old, authority, issuer, _, context, scope, claim, receipt = interrupted
    assert authority.operation_status_for_task(context, "operation").state == "unknown"
    with pytest.raises(PermissionDenied):
        authority.reconcile_outcome(context, claim.claim_id, receipt_reference="ordinary-answer")
    assert authority.operation_status_for_task(context, "operation").state == "unknown"

    reference = issuer.publish_outcome("proof", receipt)
    with pytest.raises(PermissionDenied):
        old.reconcile_outcome(context, claim.claim_id, receipt_reference=reference)
    authority.reconcile_outcome(context, claim.claim_id, receipt_reference=reference)
    authority.reconcile_outcome(context, claim.claim_id, receipt_reference=reference)
    status = authority.operation_status_for_task(context, "operation")
    assert status.state == "succeeded"
    assert status.result_digest == "b" * 64
    assert authority.resource_state(claim.claim_id) == "stopping"

    later = replace(context, worker_generation="worker-new", execution_epoch="epoch-new")
    authority.bind_execution(later)
    with pytest.raises(OperationConflict):
        authority.prepare(later, "operation", "a" * 64, [scope])


@pytest.mark.parametrize("changes", [
    {"claim_id": "other-claim"},
    {"operation_id": "other-operation"},
    {"arguments_digest": "c" * 64},
    {"instance_id": "other-instance"},
    {"task_id": "other-task"},
    {"execution_epoch": "other-epoch"},
    {"observer_id": "other-observer"},
    {"result_digest": "not-a-digest"},
    {"outcome": "unknown"},
    {"expires_at": 1000.0},
])
def test_receipt_must_bind_the_exact_interrupted_operation(interrupted, changes):
    _, authority, issuer, _, context, _, claim, receipt = interrupted
    reference = issuer.publish_outcome("proof", replace(receipt, **changes))
    with pytest.raises(PermissionDenied):
        authority.reconcile_outcome(context, claim.claim_id, receipt_reference=reference)
    assert authority.operation_status_for_task(context, "operation").state == "unknown"


def test_outcome_receipt_cannot_be_reused_for_a_conflicting_result(interrupted):
    _, authority, issuer, _, context, _, claim, receipt = interrupted
    authority.reconcile_outcome(
        context, claim.claim_id, receipt_reference=issuer.publish_outcome("first", receipt),
    )
    conflict = replace(receipt, result_digest="c" * 64)
    with pytest.raises(PermissionDenied):
        authority.reconcile_outcome(
            context, claim.claim_id, receipt_reference=issuer.publish_outcome("conflict", conflict),
        )
    assert authority.operation_status_for_task(context, "operation").result_digest == "b" * 64
