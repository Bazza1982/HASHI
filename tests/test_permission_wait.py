"""Waiting for access preserves the operation without polling the model."""
import asyncio
from dataclasses import replace

import pytest

from orchestrator.permissions.authority import (
    DecisionReceipt,
    InvalidDecision,
    OperationConflict,
    PermissionAuthority,
    PermissionDenied,
)
from orchestrator.permissions.models import ExecutionContext, normalize_scope, scope_digest
from orchestrator.permissions.waiting import PermissionWaiter


class Issuer:
    def __init__(self):
        self.proofs = {}

    def verify(self, reference):
        return self.proofs.get(reference)

    def approve(self, authority, context, request, scopes, *, key):
        proof = DecisionReceipt(
            nonce="nonce-" + key, request_id=request.request_id,
            request_revision=request.revision, requested_digest=request.scope_digest,
            approved_digest=scope_digest(scopes), instance_id=context.instance_id,
            task_id=context.task_id, run_id=context.run_id, execution_epoch=context.execution_epoch,
            operation_id=request.operation_id, arguments_digest="a" * 64,
            policy_revision=context.policy_revision, owner_id=context.owner_id,
            agent_id=context.agent_id, session_id=context.session_id,
            context_generation=context.context_generation, principal_id=context.owner_id,
            choice="approve", lifetime="task", duration_seconds=120,
            idempotency_key="approval-" + key, expires_at=1_030.0,
        )
        self.proofs[key] = proof
        return authority.decide(key, approved_scopes=scopes)


@pytest.fixture
def environment(tmp_path):
    context = ExecutionContext(
        instance_id="i", owner_id="owner", agent_id="agent", task_id="task",
        run_id="run", session_id="session", turn_id="turn", worker_generation="worker",
        context_generation="generation", execution_epoch="epoch",
        backend_id="herv3", executor_id="registry", policy_revision=1,
    )
    issuer = Issuer()
    authority = PermissionAuthority(tmp_path / "ledger.sqlite", verifier=issuer,
                                    clock=lambda: 1_000.0, monotonic=lambda: 1_000.0)
    authority.open_task(context)
    scope = normalize_scope("file", str(tmp_path), ["write"], cwd=tmp_path)
    request = authority.request_access(
        context, [scope], purpose="write", operation_id="op", arguments_digest="a" * 64,
    )
    return authority, issuer, context, scope, request


@pytest.mark.asyncio
async def test_approval_resumes_same_operation_and_lost_signal_is_reconciled(environment):
    authority, issuer, context, scope, request = environment
    waiter = PermissionWaiter(authority, reconcile_seconds=0.02)
    waiting = asyncio.create_task(waiter.wait(
        context, request.request_id, "op", "a" * 64, [scope], reason="write",
    ))
    await asyncio.sleep(0.05)
    assert not waiting.done()
    issuer.approve(authority, context, request, [scope], key="approve")
    # No notification: bounded reconciliation observes durable state.
    result = await asyncio.wait_for(waiting, 1)
    assert result.state == "ready"
    assert result.operation_id == "op"
    assert result.request_id == request.request_id
    claim = authority.prepare(context, result.operation_id, "a" * 64, [scope])
    assert authority.start(context, claim.claim_id).operation_id == "op"


@pytest.mark.asyncio
async def test_approval_while_paused_does_not_resume_until_task_resumes(environment):
    authority, issuer, context, scope, request = environment
    authority.pause_task(context)
    waiter = PermissionWaiter(authority, reconcile_seconds=0.02)
    waiting = asyncio.create_task(waiter.wait(
        context, request.request_id, "op", "a" * 64, [scope], reason="write",
    ))
    issuer.approve(authority, context, request, [scope], key="paused")
    waiter.notify()
    await asyncio.sleep(0.05)
    assert not waiting.done()
    authority.resume_task(context)
    waiter.notify()
    result = await asyncio.wait_for(waiting, 1)
    assert result.state == "ready"


@pytest.mark.asyncio
async def test_stopped_task_and_fenced_epoch_do_not_resume(environment):
    authority, issuer, context, scope, request = environment
    waiter = PermissionWaiter(authority, reconcile_seconds=0.02)
    waiting = asyncio.create_task(waiter.wait(
        context, request.request_id, "op", "a" * 64, [scope], reason="write",
    ))
    authority.end_task(context, outcome="cancelled")
    with pytest.raises(InvalidDecision):
        issuer.approve(authority, context, request, [scope], key="late")
    waiter.notify()
    result = await asyncio.wait_for(waiting, 1)
    assert result.state == "cancelled"


@pytest.mark.asyncio
async def test_old_execution_wait_is_fenced_when_backend_changes(environment):
    authority, _, context, scope, request = environment
    newer = replace(context, execution_epoch="new-epoch", worker_generation="new-worker")
    authority.bind_execution(newer)
    waiter = PermissionWaiter(authority, reconcile_seconds=0.02)
    waiting = asyncio.create_task(waiter.wait(
        context, request.request_id, "op", "a" * 64, [scope], reason="write",
    ))
    authority.fence_execution(context)
    waiter.notify()
    result = await asyncio.wait_for(waiting, 1)
    assert result.state == "fenced"
    rebound = authority.request_access(
        newer, [scope], purpose="write", operation_id="op", arguments_digest="a" * 64,
    )
    assert rebound.operation_id == "op"
    assert rebound.revision == request.revision + 1


@pytest.mark.asyncio
async def test_partial_decision_waits_for_remaining_scope(environment, tmp_path):
    authority, issuer, context, _, _ = environment
    full = normalize_scope("file", str(tmp_path), ["read", "write"], cwd=tmp_path)
    read = normalize_scope("file", str(tmp_path), ["read"], cwd=tmp_path)
    write = normalize_scope("file", str(tmp_path), ["write"], cwd=tmp_path)
    request = authority.request_access(
        context, [full], purpose="both", operation_id="op2", arguments_digest="a" * 64,
    )
    narrowed = authority.narrow_request(context, request.request_id, 1, [read])
    waiter = PermissionWaiter(authority, reconcile_seconds=0.02)
    waiting = asyncio.create_task(waiter.wait(
        context, narrowed.request_id, "op2", "a" * 64, [full], reason="both",
    ))
    issuer.approve(authority, context, narrowed, [read], key="partial")

    async def next_request():
        while True:
            for candidate in authority.list_requests(context):
                if candidate.operation_id == "op2" and candidate.state == "pending":
                    return candidate
            await asyncio.sleep(0.01)

    remaining = await asyncio.wait_for(next_request(), 1)
    assert remaining.scopes == (write,)
    assert not waiting.done()
    issuer.approve(authority, context, remaining, [write], key="remaining")
    result = await asyncio.wait_for(waiting, 1)
    assert result.state == "ready"
    assert result.request_id == remaining.request_id
    assert result.operation_id == "op2"


@pytest.mark.asyncio
async def test_expired_request_reissues_without_changing_operation(environment, monkeypatch):
    authority, _, context, scope, request = environment
    monkeypatch.setattr(authority, "_clock", lambda: 87_400.0)
    monkeypatch.setattr(authority, "_monotonic", lambda: 87_400.0)
    waiter = PermissionWaiter(authority, reconcile_seconds=0.02)
    result = await waiter.wait(
        context, request.request_id, "op", "a" * 64, [scope],
        reason="write", timeout_seconds=0.05,
    )
    assert result.state == "waiting"
    assert result.request_id != request.request_id
    assert result.operation_id == "op"
    assert authority.get_request(context, request.request_id).state == "expired"
    assert authority.get_request(context, result.request_id).state == "pending"
    assert authority.operation_state("op") == "waiting"


@pytest.mark.asyncio
async def test_wait_rejects_changed_operation_arguments(environment):
    authority, _, context, scope, request = environment
    waiter = PermissionWaiter(authority, reconcile_seconds=0.02)
    with pytest.raises(OperationConflict):
        await waiter.wait(context, request.request_id, "op", "b" * 64, [scope], reason="write")
    assert authority.operation_state("op") == "waiting"


@pytest.mark.asyncio
async def test_wait_timeout_keeps_request_pending(environment):
    authority, _, context, scope, request = environment
    waiter = PermissionWaiter(authority, reconcile_seconds=0.02)
    result = await waiter.wait(
        context, request.request_id, "op", "a" * 64, [scope],
        reason="write", timeout_seconds=0.05,
    )
    assert result.state == "waiting"
    assert authority.get_request(context, request.request_id).state == "pending"
    assert authority.operation_state("op") == "waiting"


@pytest.mark.asyncio
async def test_reconciliation_does_not_retry_unrelated_authority_denial(environment, tmp_path, monkeypatch):
    authority, issuer, context, _, _ = environment
    full = normalize_scope("file", str(tmp_path), ["read", "write"], cwd=tmp_path)
    read = normalize_scope("file", str(tmp_path), ["read"], cwd=tmp_path)
    request = authority.request_access(
        context, [full], purpose="both", operation_id="op2", arguments_digest="a" * 64,
    )
    narrowed = authority.narrow_request(context, request.request_id, 1, [read])
    issuer.approve(authority, context, narrowed, [read], key="partial-error")
    calls = []

    def unavailable(*args, **kwargs):
        calls.append(1)
        raise PermissionDenied("storage unavailable")

    monkeypatch.setattr(authority, "request_access", unavailable)
    waiter = PermissionWaiter(authority, reconcile_seconds=0.02)
    with pytest.raises(PermissionDenied, match="storage unavailable"):
        await asyncio.wait_for(
            waiter.wait(context, narrowed.request_id, "op2", "a" * 64, [full], reason="both"),
            1,
        )
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_revocation_during_resource_queue_prevents_real_file_write(environment, tmp_path):
    authority, issuer, context, scope, request = environment
    waiter = PermissionWaiter(authority, reconcile_seconds=0.02)
    lease = issuer.approve(authority, context, request, [scope], key="queued")
    result = await waiter.wait(context, request.request_id, "op", "a" * 64, [scope], reason="write")
    assert result.state == "ready"
    resource = asyncio.Lock()
    await resource.acquire()
    target = tmp_path / "side-effect.txt"

    async def dispatch_after_queue():
        async with resource:
            claim = authority.prepare(context, "op", "a" * 64, [scope])
            authority.start(context, claim.claim_id)
            target.write_text("must never be written", encoding="utf-8")

    operation = asyncio.create_task(dispatch_after_queue())
    await asyncio.sleep(0)
    authority.revoke(context, lease.lease_id)
    resource.release()
    with pytest.raises(PermissionDenied):
        await operation
    assert not target.exists()
