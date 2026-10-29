"""A managed operation must keep its identity from approval through dispatch."""
import asyncio
from dataclasses import replace
from hashlib import sha256

import pytest

from orchestrator.permissions.authority import DecisionReceipt, PermissionAuthority, PermissionDenied
from orchestrator.permissions.execution_gate import (
    PermissionExecutionGate,
    SupervisedResult,
)
from orchestrator.permissions.models import ExecutionContext, normalize_scope, scope_digest
from orchestrator.permissions.waiting import PermissionWaiter


class Issuer:
    def __init__(self):
        self.proofs = {}

    def verify(self, reference):
        return self.proofs.get(reference)

    def approve(self, authority, context, request, arguments_digest):
        proof = DecisionReceipt(
            nonce="nonce", request_id=request.request_id, request_revision=request.revision,
            requested_digest=request.scope_digest, approved_digest=scope_digest(request.scopes),
            instance_id=context.instance_id, task_id=context.task_id, run_id=context.run_id,
            execution_epoch=context.execution_epoch, operation_id=request.operation_id,
            arguments_digest=arguments_digest, policy_revision=context.policy_revision,
            owner_id=context.owner_id, agent_id=context.agent_id, session_id=context.session_id,
            context_generation=context.context_generation, principal_id=context.owner_id,
            choice="approve", lifetime="once", duration_seconds=120,
            idempotency_key="one-approval", expires_at=1_030.0,
        )
        self.proofs["proof"] = proof
        return authority.decide("proof", approved_scopes=request.scopes)


class TestFileSupervisor:
    """Test-only in-process runner; it is not an OS security boundary."""

    __test__ = False

    def __init__(self, authority, root):
        self.authority = authority
        self.root = root
        self.resource = asyncio.Lock()
        self.queued = asyncio.Event()

    def supports(self, name):
        return name == "file_write"

    async def execute(self, context, operation_id, descriptor, arguments):
        self.queued.set()
        async with self.resource:
            claim = self.authority.prepare(
                context, operation_id, descriptor.arguments_digest, descriptor.scopes,
            )
            self.authority.start(context, claim.claim_id)
            target = self.root / arguments["path"]
            target.write_text(arguments["content"], encoding="utf-8")
            digest = sha256(b"written").hexdigest()
            self.authority.finish(context, claim.claim_id, outcome="succeeded", result_digest=digest)
            return SupervisedResult(operation_id, descriptor.arguments_digest, "succeeded", digest, "written")


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
    maximum = normalize_scope("file", str(tmp_path), ["create", "write"], cwd=tmp_path)
    waiter = PermissionWaiter(authority, reconcile_seconds=0.02)
    supervisor = TestFileSupervisor(authority, tmp_path)
    gate = PermissionExecutionGate(
        authority, waiter, supervisor, context=context, cwd=tmp_path,
        read_roots=(tmp_path,), requestable_scopes=(maximum,),
    )
    return authority, issuer, context, waiter, supervisor, gate


def test_gate_rejects_waiter_from_a_different_authority(environment, tmp_path):
    authority, issuer, context, _, supervisor, _ = environment
    other = PermissionAuthority(tmp_path / "other.sqlite", verifier=issuer,
                                clock=lambda: 1_000.0, monotonic=lambda: 1_000.0)
    ceiling = normalize_scope("file", str(tmp_path), ["create", "write"], cwd=tmp_path)
    with pytest.raises(ValueError, match="matching"):
        PermissionExecutionGate(
            authority, PermissionWaiter(other), supervisor, context=context, cwd=tmp_path,
            read_roots=(tmp_path,), requestable_scopes=(ceiling,),
        )


async def find_pending(authority, context):
    async def poll():
        while True:
            pending = [request for request in authority.list_requests(context)
                       if request.state == "pending"]
            if pending:
                return pending[0]
            await asyncio.sleep(0.01)
    return await asyncio.wait_for(poll(), 1)


@pytest.mark.asyncio
async def test_approval_resumes_original_immutable_file_operation(environment, tmp_path):
    authority, issuer, context, waiter, _, gate = environment
    arguments = {"path": "out.txt", "content": "original"}
    task = asyncio.create_task(gate.execute("operation", "file_write", arguments, reason="write report"))
    request = await find_pending(authority, context)
    operation = authority.operation_status_for_task(context, "operation")
    arguments["content"] = "changed after request"
    issuer.approve(authority, context, request, operation.arguments_digest)
    waiter.notify()
    result = await asyncio.wait_for(task, 1)
    assert result.state == "succeeded"
    assert result.operation_id == "operation"
    assert (tmp_path / "out.txt").read_text(encoding="utf-8") == "original"


@pytest.mark.asyncio
async def test_revoking_while_queued_never_dispatches_file_write(environment, tmp_path):
    authority, issuer, context, waiter, supervisor, gate = environment
    await supervisor.resource.acquire()
    task = asyncio.create_task(gate.execute(
        "operation", "file_write", {"path": "out.txt", "content": "content"}, reason="report",
    ))
    request = await find_pending(authority, context)
    operation = authority.operation_status_for_task(context, "operation")
    lease = issuer.approve(authority, context, request, operation.arguments_digest)
    waiter.notify()
    await asyncio.wait_for(supervisor.queued.wait(), 1)
    authority.revoke(context, lease.lease_id)
    supervisor.resource.release()
    result = await asyncio.wait_for(task, 1)
    assert result.state == "permission_changed"
    assert not (tmp_path / "out.txt").exists()


@pytest.mark.asyncio
async def test_runner_error_after_start_is_unknown_and_cannot_repeat(environment, tmp_path, monkeypatch):
    authority, issuer, context, waiter, supervisor, gate = environment
    calls = []

    async def execute_then_lose_receipt(ctx, operation_id, descriptor, arguments):
        calls.append(1)
        claim = authority.prepare(ctx, operation_id, descriptor.arguments_digest, descriptor.scopes)
        authority.start(ctx, claim.claim_id)
        (tmp_path / arguments["path"]).write_text(arguments["content"], encoding="utf-8")
        raise PermissionDenied("lost runner receipt")

    monkeypatch.setattr(supervisor, "execute", execute_then_lose_receipt)
    arguments = {"path": "uncertain.txt", "content": "written"}
    task = asyncio.create_task(gate.execute("operation", "file_write", arguments, reason="write"))
    request = await find_pending(authority, context)
    operation = authority.operation_status_for_task(context, "operation")
    issuer.approve(authority, context, request, operation.arguments_digest)
    waiter.notify()
    result = await asyncio.wait_for(task, 1)
    assert result.state == "unknown"
    assert authority.operation_state("operation") == "started"
    assert (tmp_path / "uncertain.txt").read_text(encoding="utf-8") == "written"
    retry = await gate.execute("operation", "file_write", arguments, reason="write")
    assert retry.state == "conflict"
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_unavailable_authority_or_supervisor_cannot_fall_back(environment, tmp_path, monkeypatch):
    authority, _, context, _, supervisor, gate = environment

    def unavailable(_name):
        raise OSError("supervisor unavailable")

    monkeypatch.setattr(supervisor, "supports", unavailable)
    result = await gate.execute(
        "operation", "file_write", {"path": "out.txt", "content": "content"}, reason="write"
    )
    assert result.state == "unavailable"
    assert authority.list_requests(context) == ()
    assert not (tmp_path / "out.txt").exists()

    monkeypatch.setattr(supervisor, "supports", lambda _name: True)

    def unavailable_authority(_context):
        raise OSError("authority unavailable")

    monkeypatch.setattr(authority, "execution_status", unavailable_authority)
    result = await gate.execute(
        "operation", "file_write", {"path": "out.txt", "content": "content"}, reason="write"
    )
    assert result.state == "unavailable"
    assert authority.list_requests(context) == ()
    assert not (tmp_path / "out.txt").exists()


@pytest.mark.asyncio
async def test_unknown_or_unsupervised_tool_never_dispatches(environment, tmp_path):
    authority, _, context, _, _, gate = environment
    result = await gate.execute("operation", "unreviewed_tool", {"path": "x"}, reason="work")
    assert result.state == "unavailable"
    assert authority.list_requests(context) == ()
    assert not (tmp_path / "x").exists()


@pytest.mark.asyncio
async def test_runner_cannot_replace_payload_after_the_result_was_recorded(environment, tmp_path, monkeypatch):
    authority, issuer, context, waiter, supervisor, gate = environment
    original_execute = supervisor.execute

    async def forged_payload(*args):
        receipt = await original_execute(*args)
        return replace(receipt, payload="unrecorded result")

    monkeypatch.setattr(supervisor, "execute", forged_payload)
    task = asyncio.create_task(gate.execute(
        "operation", "file_write", {"path": "out.txt", "content": "original"}, reason="report",
    ))
    request = await find_pending(authority, context)
    operation = authority.operation_status_for_task(context, "operation")
    issuer.approve(authority, context, request, operation.arguments_digest)
    waiter.notify()
    result = await asyncio.wait_for(task, 1)
    assert result.state == "unknown"
    assert result.payload is None
