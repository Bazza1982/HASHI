"""A managed operation must keep its identity from approval through dispatch."""
import asyncio
from dataclasses import replace
from hashlib import sha256

import pytest

from orchestrator.permissions.authority import DecisionReceipt, PermissionAuthority, PermissionDenied, ResourceStopReceipt
from orchestrator.permissions.execution_gate import (
    PermissionExecutionGate,
    SupervisedResult,
)
from orchestrator.permissions.models import ExecutionContext, normalize_scope, scope_digest
from orchestrator.permissions.waiting import PermissionWaiter


class Issuer:
    def __init__(self):
        self.proofs = {}
        self.stop_proofs = {}

    def verify(self, reference):
        return self.proofs.get(reference)

    def verify_stop(self, reference):
        return self.stop_proofs.get(reference)

    def confirm_stopped(self, authority, context, claim_id):
        reference = "stop-" + claim_id
        self.stop_proofs[reference] = ResourceStopReceipt(
            nonce=reference, claim_id=claim_id, instance_id=context.instance_id,
            task_id=context.task_id, execution_epoch=context.execution_epoch,
            observer_id=context.executor_id, evidence_digest=sha256(claim_id.encode()).hexdigest(),
            expires_at=1_030.0,
        )
        authority.confirm_resource_stopped(context, claim_id, receipt_reference=reference)

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

    def __init__(self, authority, root, *, confirm_stopped=None):
        self._confirm_stopped = confirm_stopped
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
            self.last_claim = claim.claim_id
            self.authority.start(context, claim.claim_id)
            target = self.root / arguments["path"]
            target.write_text(arguments["content"], encoding="utf-8")
            digest = sha256(b"written").hexdigest()
            self.authority.finish(context, claim.claim_id, outcome="succeeded", result_digest=digest)
            if getattr(self, "confirm_stop", True):
                observer = self._confirm_stopped or self.authority._verifier.confirm_stopped
                observer(self.authority, context, claim.claim_id)
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
    assert retry.state == "unknown"
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


class RecoverableFileSupervisor(TestFileSupervisor):
    """Test-only runner with a separate, persisted result copy."""

    def __init__(self, authority, root, *, confirm_stop=True):
        super().__init__(authority, root)
        self.result_path = root / "test-supervisor-result.json"
        self.execute_calls = 0
        self.recover_calls = 0
        self.confirm_stop = confirm_stop

    async def execute(self, context, operation_id, descriptor, arguments):
        import json
        from dataclasses import asdict

        self.execute_calls += 1
        receipt = await super().execute(context, operation_id, descriptor, arguments)
        self.result_path.write_text(json.dumps(asdict(receipt)), encoding="utf-8")
        if self.confirm_stop:
            self.authority._verifier.confirm_stopped(self.authority, context, self.last_claim)
        return receipt

    async def recover(self, context, operation_id, descriptor):
        import json

        self.recover_calls += 1
        return SupervisedResult(**json.loads(self.result_path.read_text(encoding="utf-8")))


async def _complete_recoverable_operation(environment, tmp_path, *, confirm_stop=True):
    authority, issuer, context, waiter, _, gate = environment
    supervisor = RecoverableFileSupervisor(authority, tmp_path, confirm_stop=confirm_stop)
    gate._supervisor = supervisor
    arguments = {"path": "recovery.txt", "content": "created once"}
    task = asyncio.create_task(gate.execute("operation", "file_write", arguments, reason="report"))
    request = await find_pending(authority, context)
    operation = authority.operation_status_for_task(context, "operation")
    issuer.approve(authority, context, request, operation.arguments_digest)
    waiter.notify()
    assert (await asyncio.wait_for(task, 1)).state == ("succeeded" if confirm_stop else "unknown")
    return authority, issuer, context, gate, supervisor, arguments, request


@pytest.mark.asyncio
async def test_completed_operation_replays_verified_result_after_worker_change(environment, tmp_path):
    authority, issuer, context, _, supervisor, arguments, request = await _complete_recoverable_operation(
        environment, tmp_path
    )
    # Preserve a visible later change: a replay must never write this path again.
    (tmp_path / "recovery.txt").write_text("changed later", encoding="utf-8")
    reopened = PermissionAuthority(authority.path, verifier=issuer, clock=lambda: 1_000.0, monotonic=lambda: 1_000.0)
    later = replace(context, execution_epoch="new-epoch", worker_generation="new-worker")
    reopened.bind_execution(later)
    recovered_supervisor = RecoverableFileSupervisor(reopened, tmp_path)
    maximum = normalize_scope("file", str(tmp_path), ["create", "write"], cwd=tmp_path)
    gate = PermissionExecutionGate(
        reopened, PermissionWaiter(reopened), recovered_supervisor, context=later,
        cwd=tmp_path, read_roots=(tmp_path,), requestable_scopes=(maximum,),
    )
    result = await gate.execute("operation", "file_write", arguments, reason="report")
    assert result.state == "succeeded" and result.payload == "written"
    assert recovered_supervisor.recover_calls == 1 and recovered_supervisor.execute_calls == 0
    assert (tmp_path / "recovery.txt").read_text(encoding="utf-8") == "changed later"
    assert reopened.decision_count(request.request_id) == 1
    changed = await gate.execute(
        "operation", "file_write", {"path": "recovery.txt", "content": "different"}, reason="report"
    )
    assert changed.state == "conflict"
    assert recovered_supervisor.recover_calls == 1 and recovered_supervisor.execute_calls == 0
    assert supervisor.execute_calls == 1


@pytest.mark.asyncio
async def test_new_worker_cannot_replay_until_the_old_execution_is_fenced(environment, tmp_path):
    authority, _, context, _, _, arguments, _ = await _complete_recoverable_operation(environment, tmp_path)
    later = replace(context, execution_epoch="new-epoch", worker_generation="new-worker")
    authority.bind_execution(later)
    supervisor = RecoverableFileSupervisor(authority, tmp_path)
    maximum = normalize_scope("file", str(tmp_path), ["create", "write"], cwd=tmp_path)
    gate = PermissionExecutionGate(
        authority, PermissionWaiter(authority), supervisor, context=later,
        cwd=tmp_path, read_roots=(tmp_path,), requestable_scopes=(maximum,),
    )
    assert (await gate.execute("operation", "file_write", arguments, reason="report")).state == "conflict"
    assert supervisor.recover_calls == 0 and supervisor.execute_calls == 0
    authority.fence_execution(context)
    assert (await gate.execute("operation", "file_write", arguments, reason="report")).state == "succeeded"
    assert supervisor.recover_calls == 1 and supervisor.execute_calls == 0


@pytest.mark.parametrize("damage", ["missing", "payload", "operation", "arguments", "digest", "outcome"])
@pytest.mark.asyncio
async def test_missing_or_tampered_result_never_reexecutes(environment, tmp_path, damage):
    import json

    _, _, _, gate, supervisor, arguments, _ = await _complete_recoverable_operation(environment, tmp_path)
    if damage == "missing":
        supervisor.result_path.unlink()
    else:
        stored = json.loads(supervisor.result_path.read_text(encoding="utf-8"))
        key = {
            "payload": "payload", "operation": "operation_id", "arguments": "arguments_digest",
            "digest": "result_digest", "outcome": "outcome",
        }[damage]
        stored[key] = "failed" if damage == "outcome" else "altered"
        supervisor.result_path.write_text(json.dumps(stored), encoding="utf-8")
    result = await gate.execute("operation", "file_write", arguments, reason="report")
    assert result.state == "result_unavailable"
    assert result.payload is None and supervisor.execute_calls == 1 and supervisor.recover_calls == 1
    assert (tmp_path / "recovery.txt").read_text(encoding="utf-8") == "created once"


@pytest.mark.asyncio
async def test_started_operation_with_lost_result_never_runs_again(environment, tmp_path, monkeypatch):
    authority, issuer, context, waiter, _, gate = environment
    supervisor = RecoverableFileSupervisor(authority, tmp_path)
    gate._supervisor = supervisor

    def lost_finish(*args, **kwargs):
        raise OSError("result storage unavailable")

    monkeypatch.setattr(authority, "finish", lost_finish)
    arguments = {"path": "uncertain.txt", "content": "written once"}
    task = asyncio.create_task(gate.execute("operation", "file_write", arguments, reason="report"))
    request = await find_pending(authority, context)
    operation = authority.operation_status_for_task(context, "operation")
    issuer.approve(authority, context, request, operation.arguments_digest)
    waiter.notify()
    assert (await asyncio.wait_for(task, 1)).state == "unknown"
    monkeypatch.setattr(supervisor, "supports", lambda _name: False)
    assert (await gate.execute("operation", "file_write", arguments, reason="report")).state == "unknown"
    assert supervisor.execute_calls == 1 and supervisor.recover_calls == 0
    assert (tmp_path / "uncertain.txt").read_text(encoding="utf-8") == "written once"


@pytest.mark.asyncio
async def test_completed_operation_stays_nonretryable_when_supervisor_is_unavailable(environment, tmp_path, monkeypatch):
    _, _, _, gate, supervisor, arguments, _ = await _complete_recoverable_operation(environment, tmp_path)
    monkeypatch.setattr(supervisor, "supports", lambda _name: False)
    result = await gate.execute("operation", "file_write", arguments, reason="report")
    assert result.state == "result_unavailable"
    assert supervisor.execute_calls == 1 and supervisor.recover_calls == 0


@pytest.mark.asyncio
async def test_stopped_task_cannot_receive_a_result_that_is_still_loading(environment, tmp_path, monkeypatch):
    authority, _, context, gate, supervisor, arguments, _ = await _complete_recoverable_operation(
        environment, tmp_path
    )
    loading, release = asyncio.Event(), asyncio.Event()
    recover = supervisor.recover

    async def slow_recovery(*args):
        loading.set()
        await release.wait()
        return await recover(*args)

    monkeypatch.setattr(supervisor, "recover", slow_recovery)
    task = asyncio.create_task(gate.execute("operation", "file_write", arguments, reason="report"))
    await asyncio.wait_for(loading.wait(), 1)
    authority.end_task(context, outcome="cancelled")
    release.set()
    result = await asyncio.wait_for(task, 1)
    assert result.state == "result_unavailable" and result.payload is None
    assert supervisor.execute_calls == 1 and supervisor.recover_calls == 1


@pytest.mark.asyncio
async def test_another_run_cannot_reuse_the_original_operation_id(environment, tmp_path):
    authority, _, context, _, _, arguments, _ = await _complete_recoverable_operation(environment, tmp_path)
    later = replace(context, run_id="other-run", execution_epoch="new-epoch", worker_generation="new-worker")
    authority.fence_execution(context)
    authority.bind_execution(later)
    supervisor = RecoverableFileSupervisor(authority, tmp_path)
    maximum = normalize_scope("file", str(tmp_path), ["create", "write"], cwd=tmp_path)
    gate = PermissionExecutionGate(
        authority, PermissionWaiter(authority), supervisor, context=later,
        cwd=tmp_path, read_roots=(tmp_path,), requestable_scopes=(maximum,),
    )
    assert (await gate.execute("operation", "file_write", arguments, reason="report")).state == "conflict"
    assert supervisor.execute_calls == 0 and supervisor.recover_calls == 0


@pytest.mark.asyncio
async def test_a_completed_failure_replays_its_original_result_without_executing_again(environment, tmp_path, monkeypatch):
    import json
    from dataclasses import asdict

    authority, issuer, context, waiter, _, gate = environment
    supervisor = RecoverableFileSupervisor(authority, tmp_path)
    gate._supervisor = supervisor

    async def failed_execution(ctx, operation_id, descriptor, arguments):
        supervisor.execute_calls += 1
        claim = authority.prepare(ctx, operation_id, descriptor.arguments_digest, descriptor.scopes)
        authority.start(ctx, claim.claim_id)
        payload = "target already exists"
        digest = sha256(payload.encode("utf-8")).hexdigest()
        authority.finish(ctx, claim.claim_id, outcome="failed", result_digest=digest)
        receipt = SupervisedResult(operation_id, descriptor.arguments_digest, "failed", digest, payload)
        supervisor.result_path.write_text(json.dumps(asdict(receipt)), encoding="utf-8")
        issuer.confirm_stopped(authority, ctx, claim.claim_id)
        return receipt

    monkeypatch.setattr(supervisor, "execute", failed_execution)
    arguments = {"path": "already.txt", "content": "do not write"}
    task = asyncio.create_task(gate.execute("operation", "file_write", arguments, reason="report"))
    request = await find_pending(authority, context)
    operation = authority.operation_status_for_task(context, "operation")
    issuer.approve(authority, context, request, operation.arguments_digest)
    waiter.notify()
    first = await asyncio.wait_for(task, 1)
    assert first.state == "failed" and first.payload == "target already exists" and not first.replayed
    again = await gate.execute("operation", "file_write", arguments, reason="report")
    assert again.state == "failed" and again.payload == first.payload and again.replayed
    assert supervisor.execute_calls == 1 and supervisor.recover_calls == 1
    assert not (tmp_path / "already.txt").exists()


@pytest.mark.asyncio
async def test_completed_result_waits_for_verified_resource_cleanup(environment, tmp_path):
    authority, issuer, context, gate, supervisor, arguments, _ = await _complete_recoverable_operation(
        environment, tmp_path, confirm_stop=False
    )
    events = authority.pending_events(context, consumer_id="cleanup-check")
    claim = next(event.reference_id for event in events if event.event == "claim_prepared")
    assert authority.resource_state(claim) == "unknown"
    result = await gate.execute("operation", "file_write", arguments, reason="report")
    assert result.state == "result_unavailable" and result.payload is None
    assert supervisor.execute_calls == 1 and supervisor.recover_calls == 0
    issuer.confirm_stopped(authority, context, claim)
    result = await gate.execute("operation", "file_write", arguments, reason="report")
    assert result.state == "succeeded" and result.replayed
    assert supervisor.execute_calls == 1 and supervisor.recover_calls == 1


@pytest.mark.asyncio
async def test_prestart_failure_is_terminal_replayable_and_does_not_consume_once(environment, tmp_path):
    from orchestrator.permissions.descriptors import describe_tool
    from orchestrator.permissions.result_store import EncryptedResultStore

    authority, issuer, context, _, supervisor, gate = environment
    arguments = {"path": "missing/report.txt", "content": "save"}
    descriptor = describe_tool("file_write", arguments, cwd=tmp_path, read_roots=(tmp_path,))
    pending = authority.request_access(context, descriptor.scopes, purpose="Save report",
                                       operation_id="prestart", arguments_digest=descriptor.arguments_digest)
    lease = issuer.approve(authority, context, pending, descriptor.arguments_digest)
    results = EncryptedResultStore(tmp_path / "results.db", key=bytes(range(32)))
    calls = []

    async def fail_before_start(ctx, operation_id, plan, frozen):
        calls.append(operation_id)
        receipt = results.put(ctx, operation_id, plan, outcome="failed", payload="File was not opened; no action started.")
        authority.reject_before_start(ctx, operation_id, plan.arguments_digest, plan.scopes,
                                      result_digest=receipt.result_digest)
        return receipt

    async def recover(ctx, operation_id, plan):
        return results.get(ctx, operation_id, plan)

    supervisor.execute, supervisor.recover = fail_before_start, recover
    first = await gate.execute("prestart", "file_write", arguments, reason="Save")
    second = await gate.execute("prestart", "file_write", arguments, reason="Save")
    assert first.state == second.state == "failed"
    assert second.replayed and first.payload == second.payload and calls == ["prestart"]
    assert authority.get_lease(lease.lease_id).status == "active"
    with authority._transaction() as db:
        assert db.execute("SELECT count(*) FROM execution_claims").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_prestart_failure_cannot_replace_a_prepared_or_started_claim(environment, tmp_path):
    from orchestrator.permissions.descriptors import describe_tool
    from orchestrator.permissions.authority import OperationConflict

    authority, issuer, context, _, _, _ = environment
    descriptor = describe_tool("file_write", {"path": "report.txt", "content": "save"}, cwd=tmp_path, read_roots=(tmp_path,))
    request = authority.request_access(context, descriptor.scopes, purpose="Save",
                                       operation_id="started", arguments_digest=descriptor.arguments_digest)
    issuer.approve(authority, context, request, descriptor.arguments_digest)
    claim = authority.prepare(context, "started", descriptor.arguments_digest, descriptor.scopes)
    for started in (False, True):
        if started:
            authority.start(context, claim.claim_id)
        with pytest.raises(OperationConflict):
            authority.reject_before_start(context, "started", descriptor.arguments_digest, descriptor.scopes,
                                          result_digest=sha256(b"failed").hexdigest())
    assert authority.operation_state("started") == "started"
