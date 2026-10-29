"""PAO permission ledger: transactional decisions and use boundaries."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier

import pytest

from orchestrator.permissions.authority import (
    DecisionReceipt,
    ResourceStopReceipt,
    InvalidDecision,
    OperationConflict,
    PermissionAuthority,
    PermissionDenied,
    RevisionConflict,
)
from orchestrator.permissions.models import ExecutionContext, normalize_scope, scope_digest


class Clock:
    def __init__(self):
        self.value = 1_000.0

    def __call__(self):
        return self.value


class AuthenticatedReceiptSource:
    """Test transport: only references issued here are considered verified."""
    def __init__(self):
        self.receipts = {}
        self.stop_receipts = {}

    def verify(self, reference):
        return self.receipts.get(reference)

    def publish(self, receipt):
        self.receipts[receipt.nonce] = receipt
        return receipt.nonce

    def verify_stop(self, reference):
        return self.stop_receipts.get(reference)

    def publish_stop(self, receipt):
        self.stop_receipts[receipt.nonce] = receipt
        return receipt.nonce


@pytest.fixture
def environment(tmp_path):
    clock = Clock()
    issuer = AuthenticatedReceiptSource()
    context = ExecutionContext(
        instance_id="instance", owner_id="owner", agent_id="agent",
        task_id="task-a", run_id="run-a", session_id="session",
        turn_id="turn", worker_generation="worker-a",
        context_generation="context-a", execution_epoch="epoch-a",
        backend_id="herv3", executor_id="registry", policy_revision=1,
    )
    authority = PermissionAuthority(tmp_path / "permissions.sqlite", verifier=issuer, clock=clock, monotonic=clock)
    authority.open_task(context)
    root = tmp_path / "project"
    root.mkdir()
    scope = normalize_scope("file", str(root), ["read", "write"], cwd=tmp_path)
    return authority, issuer, clock, context, scope


def approve(authority, issuer, clock, context, scopes, *, lifetime="task",
            operation_id=None, arguments_digest=None, duration=100):
    request = authority.request_access(
        context, scopes, purpose="work", operation_id=operation_id,
        arguments_digest=arguments_digest,
    )
    receipt = DecisionReceipt(
        nonce="receipt-" + request.request_id,
        request_id=request.request_id,
        request_revision=request.revision,
        requested_digest=request.scope_digest,
        approved_digest=scope_digest(scopes),
        instance_id=context.instance_id,
        task_id=context.task_id,
        run_id=context.run_id,
        execution_epoch=context.execution_epoch,
        operation_id=operation_id,
        arguments_digest=arguments_digest,
        policy_revision=context.policy_revision,
        owner_id=context.owner_id,
        agent_id=context.agent_id,
        session_id=context.session_id,
        context_generation=context.context_generation,
        principal_id=context.owner_id,
        choice="approve",
        lifetime=lifetime,
        duration_seconds=duration,
        idempotency_key="decision-" + request.request_id,
        expires_at=clock.value + 30,
    )
    reference = issuer.publish(receipt)
    return request, reference, authority.decide(reference, approved_scopes=scopes)


def test_only_verified_exact_decisions_grant_and_replay_is_idempotent(environment):
    authority, issuer, clock, context, scope = environment
    request = authority.request_access(context, [scope], purpose="work")
    with pytest.raises(InvalidDecision):
        authority.decide("ordinary-ask-user-answer", approved_scopes=[scope])
    assert not authority.evaluate(context, [scope])

    receipt = DecisionReceipt(
        nonce="nonce", request_id=request.request_id, request_revision=request.revision,
        requested_digest=request.scope_digest, approved_digest=scope_digest([scope]),
        instance_id=context.instance_id, task_id=context.task_id, run_id=context.run_id,
        execution_epoch=context.execution_epoch,
        operation_id=None, arguments_digest=None, policy_revision=context.policy_revision,
        owner_id=context.owner_id, agent_id=context.agent_id, session_id=context.session_id,
        context_generation=context.context_generation, principal_id=context.owner_id,
        choice="approve", lifetime="task", duration_seconds=120,
        idempotency_key="decision-key", expires_at=clock.value + 30,
    )
    ref = issuer.publish(receipt)
    lease = authority.decide(ref, approved_scopes=[scope])
    replay = authority.decide(ref, approved_scopes=[scope])
    assert replay == lease
    assert authority.evaluate(context, [scope])
    assert authority.decision_count(request.request_id) == 1


def test_receipt_revision_scope_owner_and_expiry_are_bound(environment):
    authority, issuer, clock, context, scope = environment
    request = authority.request_access(context, [scope], purpose="work")
    base = DecisionReceipt(
        nonce="nonce", request_id=request.request_id, request_revision=request.revision,
        requested_digest=request.scope_digest, approved_digest=scope_digest([scope]),
        instance_id=context.instance_id, task_id=context.task_id, run_id=context.run_id,
        execution_epoch=context.execution_epoch,
        operation_id=None, arguments_digest=None, policy_revision=context.policy_revision,
        owner_id=context.owner_id, agent_id=context.agent_id, session_id=context.session_id,
        context_generation=context.context_generation, principal_id=context.owner_id,
        choice="approve", lifetime="task", duration_seconds=120,
        idempotency_key="decision-key", expires_at=clock.value + 30,
    )
    for index, forged in enumerate((
        replace(base, nonce="wrong-rev", request_revision=2),
        replace(base, nonce="wrong-owner", principal_id="attacker"),
        replace(base, nonce="wrong-instance", instance_id="another-instance"),
        replace(base, nonce="wrong-task", task_id="another-task"),
        replace(base, nonce="wrong-operation", operation_id="forged"),
        replace(base, nonce="wrong-epoch", execution_epoch="another-epoch"),
        replace(base, nonce="wrong-policy", policy_revision=2),
        replace(base, nonce="wrong-scope", approved_digest="0" * 64),
        replace(base, nonce="expired", expires_at=clock.value),
    )):
        with pytest.raises((InvalidDecision, RevisionConflict)):
            authority.decide(issuer.publish(forged), approved_scopes=[scope])
    assert authority.decision_count(request.request_id) == 0
    assert not authority.evaluate(context, [scope])


def test_task_lease_is_isolated_and_does_not_end_with_one_run(environment):
    authority, issuer, clock, context, scope = environment
    _, _, lease = approve(authority, issuer, clock, context, [scope])
    next_run = replace(context, run_id="run-b", execution_epoch="epoch-b")
    authority.bind_execution(next_run)
    assert authority.evaluate(next_run, [scope])

    other = replace(context, task_id="task-b", run_id="run-c", execution_epoch="epoch-c")
    authority.open_task(other)
    assert not authority.evaluate(other, [scope])
    authority.end_task(context, outcome="completed")
    assert not authority.evaluate(next_run, [scope])
    assert authority.get_lease(lease.lease_id).status == "revoked"


def test_revocation_after_queue_prevents_start(environment):
    authority, issuer, clock, context, scope = environment
    _, _, lease = approve(authority, issuer, clock, context, [scope])
    claim = authority.prepare(context, "operation", "a" * 64, [scope])
    authority.revoke(context, lease.lease_id)
    with pytest.raises(PermissionDenied):
        authority.start(context, claim.claim_id)
    assert authority.operation_state("operation") != "started"


def test_once_grant_has_one_winner_and_one_real_side_effect(environment, tmp_path):
    authority, issuer, clock, context, scope = environment
    approve(authority, issuer, clock, context, [scope], lifetime="once",
            operation_id="op-once", arguments_digest="a" * 64)
    barrier = Barrier(2)
    sink = tmp_path / "sink.txt"

    def execute():
        barrier.wait()
        try:
            claim = authority.prepare(context, "op-once", "a" * 64, [scope])
            authority.start(context, claim.claim_id)
        except (PermissionDenied, OperationConflict):
            return "denied"
        with sink.open("a", encoding="utf-8") as stream:
            stream.write("executed\\n")
        authority.finish(context, claim.claim_id, outcome="succeeded", result_digest="b" * 64)
        return "executed"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: execute(), range(2)))
    assert sorted(results) == ["denied", "executed"]
    assert sink.read_text(encoding="utf-8") == "executed\\n"
    with pytest.raises((PermissionDenied, OperationConflict)):
        authority.prepare(context, "different-op", "a" * 64, [scope])


def test_expiry_and_clock_rollback_fail_closed(environment):
    authority, issuer, clock, context, scope = environment
    approve(authority, issuer, clock, context, [scope], duration=60)
    clock.value += 59
    assert authority.evaluate(context, [scope])
    clock.value += 1
    assert not authority.evaluate(context, [scope])
    clock.value -= 30
    with pytest.raises(PermissionDenied):
        authority.evaluate(context, [scope])


@pytest.mark.parametrize("failing_access", [False, True])
def test_monotonic_expiry_cannot_revive_after_reopen(tmp_path, failing_access):
    wall, monotonic = Clock(), Clock()
    issuer = AuthenticatedReceiptSource()
    context = ExecutionContext(
        instance_id="instance", owner_id="owner", agent_id="agent",
        task_id="clock-task", run_id="clock-run", session_id="session",
        turn_id="turn", worker_generation="worker",
        context_generation="generation", execution_epoch="epoch",
        backend_id="herv3", executor_id="registry", policy_revision=1,
    )
    path = tmp_path / "clock.sqlite"
    authority = PermissionAuthority(path, verifier=issuer, clock=wall, monotonic=monotonic)
    authority.open_task(context)
    scope = normalize_scope("file", str(tmp_path), ["read"], cwd=tmp_path)
    approve(authority, issuer, wall, context, [scope], duration=60)
    monotonic.value += 61
    if failing_access:
        # An operation that raises must still preserve observed time progress.
        with pytest.raises(PermissionDenied):
            authority.prepare(context, "expired-op", "a" * 64, [scope])
    else:
        assert not authority.evaluate(context, [scope])
    reopened = PermissionAuthority(path, verifier=issuer, clock=wall, monotonic=monotonic)
    later = replace(context, worker_generation="worker-new", execution_epoch="epoch-new")
    reopened.bind_execution(later)
    assert not reopened.evaluate(later, [scope]), "an expired permission became valid after restart"

    # Once re-anchored, monotonic time must continue advancing even if the
    # wall clock stays frozen, including after a rejected transaction.
    first = reopened.request_access(later, [scope], purpose="new request")
    assert first.expires_at == 1_061.0 + 24 * 60 * 60
    monotonic.value += 10
    other_scope = normalize_scope("network", "https://example.com", ["fetch"], cwd=tmp_path)
    second = reopened.request_access(later, [other_scope], purpose="other request")
    assert second.expires_at == 1_071.0 + 24 * 60 * 60


def test_unknown_side_effect_cannot_be_automatically_repeated(environment):
    authority, issuer, clock, context, scope = environment
    approve(authority, issuer, clock, context, [scope])
    claim = authority.prepare(context, "uncertain", "a" * 64, [scope])
    authority.start(context, claim.claim_id)
    authority.finish(context, claim.claim_id, outcome="unknown", result_digest=None)
    assert authority.operation_state("uncertain") == "unknown"
    with pytest.raises(OperationConflict):
        authority.prepare(context, "uncertain", "a" * 64, [scope])


def test_decisions_and_leases_survive_authority_reopen(environment, tmp_path):
    authority, issuer, clock, context, scope = environment
    request, ref, lease = approve(authority, issuer, clock, context, [scope])
    reopened = PermissionAuthority(tmp_path / "permissions.sqlite", verifier=issuer, clock=clock, monotonic=clock)
    assert reopened.decide(ref, approved_scopes=[scope]) == lease
    assert not reopened.evaluate(context, [scope])
    later = replace(context, worker_generation="worker-new", execution_epoch="epoch-new")
    reopened.bind_execution(later)
    assert reopened.evaluate(later, [scope])
    assert reopened.decision_count(request.request_id) == 1


def test_existing_task_grant_does_not_create_another_request(environment):
    authority, issuer, clock, context, scope = environment
    request, _, lease = approve(authority, issuer, clock, context, [scope])
    again = authority.request_access(context, [scope], purpose="same work")
    assert again.state == "granted_existing"
    assert again.request_id == ""
    assert authority.decision_count(request.request_id) == 1
    assert authority.get_lease(lease.lease_id).status == "active"


def test_same_denial_is_not_repeated_as_a_new_request(environment):
    authority, issuer, clock, context, scope = environment
    request = authority.request_access(context, [scope], purpose="work")
    receipt = DecisionReceipt(
        nonce="deny", request_id=request.request_id, request_revision=request.revision,
        requested_digest=request.scope_digest, approved_digest=scope_digest([scope]),
        instance_id=context.instance_id, task_id=context.task_id, run_id=context.run_id,
        execution_epoch=context.execution_epoch,
        operation_id=None, arguments_digest=None, policy_revision=context.policy_revision,
        owner_id=context.owner_id, agent_id=context.agent_id, session_id=context.session_id,
        context_generation=context.context_generation, principal_id=context.owner_id,
        choice="deny", lifetime="task", duration_seconds=120,
        idempotency_key="deny-key", expires_at=clock.value + 30,
    )
    assert authority.decide(issuer.publish(receipt), approved_scopes=[scope]) is None
    repeated = authority.request_access(context, [scope], purpose="same request")
    assert repeated.request_id == request.request_id
    assert repeated.state == "denied"
    assert not authority.evaluate(context, [scope])


def test_approval_while_paused_does_not_restart_work(environment):
    authority, issuer, clock, context, scope = environment
    request = authority.request_access(context, [scope], purpose="work")
    authority.pause_task(context)
    receipt = DecisionReceipt(
        nonce="paused", request_id=request.request_id, request_revision=request.revision,
        requested_digest=request.scope_digest, approved_digest=scope_digest([scope]),
        instance_id=context.instance_id, task_id=context.task_id, run_id=context.run_id,
        execution_epoch=context.execution_epoch, operation_id=None, arguments_digest=None,
        policy_revision=context.policy_revision, owner_id=context.owner_id,
        agent_id=context.agent_id, session_id=context.session_id,
        context_generation=context.context_generation, principal_id=context.owner_id,
        choice="approve", lifetime="task", duration_seconds=120,
        idempotency_key="paused-decision", expires_at=clock.value + 30,
    )
    lease = authority.decide(issuer.publish(receipt), approved_scopes=[scope])
    assert lease.status == "active"
    assert not authority.evaluate(context, [scope])
    with pytest.raises(PermissionDenied):
        authority.prepare(context, "paused-op", "a" * 64, [scope])
    authority.resume_task(context)
    assert authority.evaluate(context, [scope])


def test_fencing_old_epoch_does_not_revoke_new_epoch_or_task_lease(environment):
    authority, issuer, clock, context, scope = environment
    _, _, lease = approve(authority, issuer, clock, context, [scope])
    newer = replace(context, execution_epoch="epoch-new", worker_generation="worker-new")
    authority.bind_execution(newer)
    authority.fence_execution(context)
    assert not authority.evaluate(context, [scope])
    assert authority.evaluate(newer, [scope])
    assert authority.get_lease(lease.lease_id).status == "active"


def test_revocation_and_resource_cleanup_are_separate_facts(environment):
    authority, issuer, clock, context, scope = environment
    _, _, lease = approve(authority, issuer, clock, context, [scope])
    claim = authority.prepare(context, "running-op", "a" * 64, [scope])
    authority.start(context, claim.claim_id)
    authority.revoke(context, lease.lease_id)
    assert authority.get_lease(lease.lease_id).status == "revoked"
    assert authority.resource_state(claim.claim_id) == "stopping"
    with pytest.raises(PermissionDenied):
        authority.confirm_resource_stopped(context, claim.claim_id, receipt_reference="unverified")
    receipt = ResourceStopReceipt(
        nonce="stop-nonce", claim_id=claim.claim_id, instance_id=context.instance_id,
        task_id=context.task_id, execution_epoch=context.execution_epoch,
        observer_id="supervisor", evidence_digest="c" * 64, expires_at=clock.value + 30,
    )
    authority.confirm_resource_stopped(context, claim.claim_id, receipt_reference=issuer.publish_stop(receipt))
    assert authority.resource_state(claim.claim_id) == "stopped"


def test_revoking_a_prepared_claim_schedules_cleanup_before_retry(environment):
    authority, issuer, clock, context, scope = environment
    _, _, lease = approve(authority, issuer, clock, context, [scope])
    claim = authority.prepare(context, "prepared-op", "a" * 64, [scope])
    authority.revoke(context, lease.lease_id)
    assert authority.resource_state(claim.claim_id) == "stopping"
    with pytest.raises((PermissionDenied, OperationConflict)):
        authority.start(context, claim.claim_id)


def test_task_end_also_schedules_cleanup_of_prepared_resources(environment):
    authority, issuer, clock, context, scope = environment
    approve(authority, issuer, clock, context, [scope])
    claim = authority.prepare(context, "prepared-op", "a" * 64, [scope])
    authority.end_task(context, outcome="cancelled")
    assert authority.resource_state(claim.claim_id) == "stopping"


def test_fenced_prepared_once_operation_waits_for_confirmed_cleanup(environment):
    authority, issuer, clock, context, scope = environment
    approve(authority, issuer, clock, context, [scope], lifetime="once",
            operation_id="once-op", arguments_digest="a" * 64)
    claim = authority.prepare(context, "once-op", "a" * 64, [scope])
    newer = replace(context, execution_epoch="epoch-new", worker_generation="worker-new")
    authority.bind_execution(newer)
    authority.fence_execution(context)
    with pytest.raises((PermissionDenied, OperationConflict)):
        authority.prepare(newer, "once-op", "a" * 64, [scope])
    receipt = ResourceStopReceipt(
        nonce="fenced-cleanup", claim_id=claim.claim_id, instance_id=context.instance_id,
        task_id=context.task_id, execution_epoch=context.execution_epoch,
        observer_id="supervisor", evidence_digest="c" * 64, expires_at=clock.value + 30,
    )
    authority.confirm_resource_stopped(context, claim.claim_id, receipt_reference=issuer.publish_stop(receipt))
    resumed = authority.prepare(newer, "once-op", "a" * 64, [scope])
    assert authority.start(newer, resumed.claim_id).state == "started"


def test_run_end_revokes_run_grants_without_ending_task_grants(environment, tmp_path):
    authority, issuer, clock, context, scope = environment
    read_scope = normalize_scope("file", scope.resource, ["read"], cwd=tmp_path)
    write_scope = normalize_scope("file", scope.resource, ["write"], cwd=tmp_path)
    _, _, task_lease = approve(authority, issuer, clock, context, [read_scope])
    _, _, run_lease = approve(authority, issuer, clock, context, [write_scope], lifetime="run")
    authority.end_run(context)
    later = replace(context, run_id="run-later", execution_epoch="epoch-later")
    authority.bind_execution(later)
    assert authority.evaluate(later, [read_scope])
    assert not authority.evaluate(later, [write_scope])
    assert authority.get_lease(task_lease.lease_id).status == "active"
    assert authority.get_lease(run_lease.lease_id).status == "revoked"


def test_once_grant_cannot_be_used_in_another_run(environment):
    authority, issuer, clock, context, scope = environment
    approve(authority, issuer, clock, context, [scope], lifetime="once",
            operation_id="once-op", arguments_digest="a" * 64)
    later = replace(context, run_id="run-later", execution_epoch="epoch-later")
    authority.bind_execution(later)
    assert not authority.evaluate(later, [scope], operation_id="once-op")


def test_outbox_redelivers_until_ack_and_cursor_survives_reopen(environment, tmp_path):
    authority, issuer, clock, context, scope = environment
    approve(authority, issuer, clock, context, [scope])
    first = authority.pending_events(context, consumer_id="frontend")
    assert [event.event for event in first] == [
        "task_opened", "request_created", "request_decided", "lease_created"
    ]
    assert authority.pending_events(context, consumer_id="frontend") == first
    assert "work" not in repr(first)
    last = first[-1].sequence
    authority.ack_events(context, consumer_id="frontend", expected=0, through=last)
    reopened = PermissionAuthority(tmp_path / "permissions.sqlite", verifier=issuer,
                                   clock=clock, monotonic=clock)
    pending = reopened.pending_events(context, consumer_id="frontend")
    assert [event.event for event in pending] == ["execution_fenced"]
    assert reopened.ack_events(context, consumer_id="frontend", expected=0, through=last) == last
    assert reopened.pending_events(context, consumer_id="frontend") == pending


def test_outbox_cursor_rejects_skipped_or_stale_acknowledgements(environment):
    authority, issuer, clock, context, scope = environment
    events = authority.pending_events(context, consumer_id="frontend")
    last = events[-1].sequence
    with pytest.raises(RevisionConflict):
        authority.ack_events(context, consumer_id="frontend", expected=last, through=last)
    with pytest.raises(ValueError):
        authority.ack_events(context, consumer_id="frontend", expected=0, through=last + 100)
    assert authority.pending_events(context, consumer_id="frontend") == events


def test_approved_subset_never_grants_the_unapproved_action(environment, tmp_path):
    authority, issuer, clock, context, scope = environment
    request = authority.request_access(context, [scope], purpose="work")
    read_scope = normalize_scope("file", scope.resource, ["read"], cwd=tmp_path)
    write_scope = normalize_scope("file", scope.resource, ["write"], cwd=tmp_path)
    receipt = DecisionReceipt(
        nonce="subset", request_id=request.request_id, request_revision=request.revision,
        requested_digest=request.scope_digest, approved_digest=scope_digest([read_scope]),
        instance_id=context.instance_id, task_id=context.task_id, run_id=context.run_id,
        execution_epoch=context.execution_epoch, operation_id=None, arguments_digest=None,
        policy_revision=context.policy_revision, owner_id=context.owner_id,
        agent_id=context.agent_id, session_id=context.session_id,
        context_generation=context.context_generation, principal_id=context.owner_id,
        choice="approve", lifetime="task", duration_seconds=120,
        idempotency_key="subset-key", expires_at=clock.value + 30,
    )
    authority.decide(issuer.publish(receipt), approved_scopes=[read_scope])
    assert authority.evaluate(context, [read_scope])
    assert not authority.evaluate(context, [write_scope])
