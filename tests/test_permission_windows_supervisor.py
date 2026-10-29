"""Native Windows supervised file operations in isolated temporary directories."""
import asyncio
from dataclasses import replace
from hashlib import sha256
import os
from threading import Event

import pytest

from orchestrator.permissions.authority import PermissionAuthority, ResourceStopReceipt
from orchestrator.permissions.execution_gate import PermissionExecutionGate
from orchestrator.permissions.models import ExecutionContext, normalize_scope
from orchestrator.permissions.pao_run_fence import PaoRunFence
from orchestrator.permissions.result_store import EncryptedResultStore
from orchestrator.permissions.waiting import PermissionWaiter
from orchestrator.permissions.windows_file_supervisor import WindowsFileSupervisor
from orchestrator.session_store import SessionStore
from tests.test_permission_execution_gate import Issuer, find_pending

pytestmark = pytest.mark.skipif(os.name != "nt", reason="native Windows file handles required")


@pytest.fixture
def environment(tmp_path):
    pao_store = SessionStore(tmp_path / "pao" / "sessions.sqlite3", instance_id="INSTANCE")
    session = pao_store.create_session(owner_id="owner", agent_id="agent")
    accepted = pao_store.accept_run(
        session_id=session["session_id"], owner_id="owner", agent_id="agent", request_id="request",
        text="write report", source="test", idempotency_key="request",
    )
    fence = pao_store.mark_request_running(accepted.request_id, worker_id="INSTANCE:agent")
    context = ExecutionContext(
        instance_id="INSTANCE", owner_id="owner", agent_id="agent", task_id="task",
        run_id=accepted.run_id, session_id=session["session_id"], turn_id="turn", worker_generation="worker",
        context_generation=str(session["context_generation"]), execution_epoch="epoch", backend_id="herv3",
        executor_id="test-file-supervisor", policy_revision=1,
    )
    issuer = Issuer()
    authority = PermissionAuthority(
        tmp_path / "ledger.sqlite", verifier=issuer, clock=lambda: 1000.0, monotonic=lambda: 1000.0,
    )
    authority.open_task(context)
    key = bytes(range(32))
    store = EncryptedResultStore(tmp_path / "results.sqlite", key=key)

    def issue_stop(ctx, claim_id):
        reference = "stop-" + claim_id
        issuer.stop_proofs[reference] = ResourceStopReceipt(
            nonce=reference, claim_id=claim_id, instance_id=ctx.instance_id, task_id=ctx.task_id,
            execution_epoch=ctx.execution_epoch, observer_id=ctx.executor_id,
            evidence_digest=sha256(claim_id.encode()).hexdigest(), expires_at=1030.0,
        )
        return reference

    supervisor = WindowsFileSupervisor(
        authority, cwd=tmp_path, read_roots=(tmp_path,), result_store=store, issue_stop=issue_stop,
        run_guard=PaoRunFence(pao_store, context=context, request_id=accepted.request_id,
                              fencing_token=fence, worker_id="INSTANCE:agent"),
    )
    return authority, issuer, context, key, store, issue_stop, supervisor


def make_gate(environment, tmp_path, actions):
    authority, _, context, _, _, _, supervisor = environment
    ceiling = normalize_scope("file", str(tmp_path), actions, cwd=tmp_path)
    waiter = PermissionWaiter(authority, reconcile_seconds=0.02)
    gate = PermissionExecutionGate(
        authority, waiter, supervisor, context=context, cwd=tmp_path,
        read_roots=(tmp_path,), requestable_scopes=(ceiling,),
    )
    return gate, waiter, ceiling


async def approve_pending(authority, issuer, context, waiter):
    request = await find_pending(authority, context)
    facts = authority.operation_status_for_task(context, request.operation_id)
    lease = issuer.approve(authority, context, request, facts.arguments_digest)
    waiter.notify()
    return request, lease


@pytest.mark.asyncio
async def test_create_then_recover_after_service_restart_without_overwriting(environment, tmp_path):
    authority, issuer, context, key, store, issue_stop, _ = environment
    gate, waiter, ceiling = make_gate(environment, tmp_path, ["create", "write"])
    arguments = {"path": "report.txt", "content": "created once"}
    operation = asyncio.create_task(gate.execute("operation", "file_write", arguments, reason="report"))
    request, _ = await approve_pending(authority, issuer, context, waiter)
    first = await asyncio.wait_for(operation, 3)
    assert first.state == "succeeded" and not first.replayed
    assert (tmp_path / "report.txt").read_text(encoding="utf-8") == "created once"
    assert authority.operation_status_for_task(context, "operation").resource_state == "stopped"
    assert b"created once" not in store.path.read_bytes()
    (tmp_path / "report.txt").write_text("changed later", encoding="utf-8")

    reopened = PermissionAuthority(
        authority.path, verifier=issuer, clock=lambda: 1000.0, monotonic=lambda: 1000.0,
    )
    later = replace(context, worker_generation="new-worker", execution_epoch="new-epoch")
    reopened.bind_execution(later)
    supervisor = WindowsFileSupervisor(
        reopened, cwd=tmp_path, read_roots=(tmp_path,),
        result_store=EncryptedResultStore(store.path, key=key), issue_stop=issue_stop,
        run_guard=PaoRunFence(
            SessionStore(tmp_path / "pao" / "sessions.sqlite3", instance_id="INSTANCE"),
            context=later, request_id="request", fencing_token=1, worker_id="INSTANCE:agent",
        ),
    )
    resumed = PermissionExecutionGate(
        reopened, PermissionWaiter(reopened), supervisor, context=later,
        cwd=tmp_path, read_roots=(tmp_path,), requestable_scopes=(ceiling,),
    )
    result = await resumed.execute("operation", "file_write", arguments, reason="report")
    assert result.state == "succeeded" and result.replayed and result.payload == first.payload
    assert (tmp_path / "report.txt").read_text(encoding="utf-8") == "changed later"
    assert reopened.decision_count(request.request_id) == 1


@pytest.mark.asyncio
async def test_authorized_read_uses_the_bound_file_and_requested_lines(environment, tmp_path):
    authority, issuer, context, _, _, _, _ = environment
    (tmp_path / "report.txt").write_text(chr(10).join(("first", "second", "third", "")), encoding="utf-8")
    gate, waiter, _ = make_gate(environment, tmp_path, ["read"])
    task = asyncio.create_task(gate.execute(
        "operation", "file_read", {"path": "report.txt", "offset": 2, "limit": 1}, reason="report",
    ))
    await approve_pending(authority, issuer, context, waiter)
    result = await asyncio.wait_for(task, 3)
    assert result.state == "succeeded"
    assert "second" in result.payload and "first" not in result.payload and "third" not in result.payload
    assert authority.operation_status_for_task(context, "operation").resource_state == "stopped"


@pytest.mark.asyncio
async def test_existing_file_is_never_overwritten_and_outcome_is_not_retried(environment, tmp_path):
    authority, issuer, context, _, _, _, _ = environment
    target = tmp_path / "report.txt"
    target.write_text("existing", encoding="utf-8")
    gate, waiter, _ = make_gate(environment, tmp_path, ["create", "write"])
    arguments = {"path": "report.txt", "content": "replacement"}
    task = asyncio.create_task(gate.execute("operation", "file_write", arguments, reason="report"))
    await approve_pending(authority, issuer, context, waiter)
    result = await asyncio.wait_for(task, 3)
    assert result.state == "unknown"
    assert target.read_text(encoding="utf-8") == "existing"
    assert (await gate.execute("operation", "file_write", arguments, reason="report")).state == "unknown"
    assert not list(tmp_path.glob(".hashi-permission-*.tmp"))


@pytest.mark.asyncio
async def test_revocation_between_start_and_dispatch_prevents_create(environment, tmp_path, monkeypatch):
    authority, issuer, context, _, _, _, _ = environment
    gate, waiter, _ = make_gate(environment, tmp_path, ["create", "write"])
    original_start, lease = authority.start, []

    def start_then_revoke(ctx, claim_id):
        result = original_start(ctx, claim_id)
        authority.revoke(ctx, lease[0].lease_id)
        return result

    monkeypatch.setattr(authority, "start", start_then_revoke)
    task = asyncio.create_task(gate.execute(
        "operation", "file_write", {"path": "blocked.txt", "content": "never write"}, reason="report",
    ))
    _, approved = await approve_pending(authority, issuer, context, waiter)
    lease.append(approved)
    result = await asyncio.wait_for(task, 3)
    assert result.state == "unknown"
    assert authority.get_lease(approved.lease_id).status == "revoked"
    assert not (tmp_path / "blocked.txt").exists()


@pytest.mark.asyncio
async def test_unverified_cleanup_never_becomes_a_replayable_success(environment, tmp_path):
    authority, issuer, context, _, _, _, supervisor = environment
    supervisor._issue_stop = lambda _ctx, _claim: "unverified"
    gate, waiter, _ = make_gate(environment, tmp_path, ["create", "write"])
    arguments = {"path": "report.txt", "content": "written once"}
    task = asyncio.create_task(gate.execute("operation", "file_write", arguments, reason="report"))
    await approve_pending(authority, issuer, context, waiter)
    result = await asyncio.wait_for(task, 3)
    assert result.state == "unknown"
    assert (tmp_path / "report.txt").read_text(encoding="utf-8") == "written once"
    assert authority.operation_status_for_task(context, "operation").resource_state == "unknown"
    assert (await gate.execute("operation", "file_write", arguments, reason="report")).state == "result_unavailable"


@pytest.mark.asyncio
async def test_cancel_waits_for_the_inflight_file_operation_to_settle(environment, tmp_path, monkeypatch):
    import orchestrator.permissions.windows_file_supervisor as module

    authority, issuer, context, _, _, _, _ = environment
    gate, waiter, _ = make_gate(environment, tmp_path, ["create", "write"])
    entered, release = Event(), Event()
    real_open = module.open_bound_create

    class DelayedBinding:
        def __init__(self, binding):
            self.binding = binding

        def __enter__(self):
            self.binding.__enter__()
            return self

        def __exit__(self, *args):
            return self.binding.__exit__(*args)

        def commit(self, data):
            entered.set()
            if not release.wait(5):
                raise TimeoutError("test did not release the operation")
            self.binding.commit(data)

    monkeypatch.setattr(module, "open_bound_create", lambda scope: DelayedBinding(real_open(scope)))
    arguments = {"path": "report.txt", "content": "written once"}
    task = asyncio.create_task(gate.execute("operation", "file_write", arguments, reason="report"))
    await approve_pending(authority, issuer, context, waiter)
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 3)
    status = authority.operation_status_for_task(context, "operation")
    assert status.state == "succeeded" and status.resource_state == "stopped"
    assert (tmp_path / "report.txt").read_text(encoding="utf-8") == "written once"


@pytest.mark.asyncio
async def test_failure_saving_result_cannot_repeat_a_completed_file_effect(environment, tmp_path, monkeypatch):
    authority, issuer, context, _, store, _, _ = environment
    gate, waiter, _ = make_gate(environment, tmp_path, ["create", "write"])

    def unavailable(*args, **kwargs):
        raise OSError("test result store unavailable")

    monkeypatch.setattr(store, "put", unavailable)
    arguments = {"path": "report.txt", "content": "written once"}
    task = asyncio.create_task(gate.execute("operation", "file_write", arguments, reason="report"))
    await approve_pending(authority, issuer, context, waiter)
    assert (await asyncio.wait_for(task, 3)).state == "unknown"
    assert (tmp_path / "report.txt").read_text(encoding="utf-8") == "written once"
    assert authority.operation_status_for_task(context, "operation").state == "started"
    assert (await gate.execute("operation", "file_write", arguments, reason="report")).state == "unknown"


@pytest.mark.asyncio
async def test_supervisor_rejects_changed_arguments_before_binding_a_file(environment, tmp_path):
    from orchestrator.permissions.authority import PermissionDenied
    from orchestrator.permissions.descriptors import describe_tool

    authority, issuer, context, _, _, _, supervisor = environment
    original = {"path": "approved.txt", "content": "approved"}
    descriptor = describe_tool("file_write", original, cwd=tmp_path, read_roots=(tmp_path,))
    request = authority.request_access(
        context, descriptor.scopes, purpose="report", operation_id="operation",
        arguments_digest=descriptor.arguments_digest,
    )
    issuer.approve(authority, context, request, descriptor.arguments_digest)
    changed = {"path": "approved.txt", "content": "different"}
    with pytest.raises(PermissionDenied):
        await supervisor.execute(context, "operation", descriptor, changed)
    assert not (tmp_path / "approved.txt").exists()
    assert authority.operation_status_for_task(context, "operation").state == "waiting"
    result = await supervisor.execute(context, "operation", descriptor, original)
    assert result.outcome == "succeeded"
    assert (tmp_path / "approved.txt").read_text(encoding="utf-8") == "approved"


@pytest.mark.asyncio
async def test_replaced_directory_cannot_redirect_the_approved_operation(environment, tmp_path, monkeypatch):
    import orchestrator.permissions.windows_file_supervisor as module

    authority, issuer, context, _, _, _, _ = environment
    project = tmp_path / "project"
    outside = tmp_path / "outside"
    project.mkdir()
    outside.mkdir()
    entered, release = Event(), Event()
    real_open = module.open_bound_create

    def delayed_open(scope):
        entered.set()
        if not release.wait(5):
            raise TimeoutError("test did not release file binding")
        return real_open(scope)

    monkeypatch.setattr(module, "open_bound_create", delayed_open)
    gate, waiter, _ = make_gate(environment, tmp_path, ["create", "write"])
    task = asyncio.create_task(gate.execute(
        "operation", "file_write", {"path": "project/report.txt", "content": "approved"}, reason="report",
    ))
    _, lease = await approve_pending(authority, issuer, context, waiter)
    assert await asyncio.to_thread(entered.wait, 2)
    original = tmp_path / "original-project"
    os.replace(project, original)
    try:
        os.symlink(outside, project, target_is_directory=True)
    except OSError:
        os.replace(original, project)
        authority.revoke(context, lease.lease_id)
        release.set()
        await asyncio.wait_for(task, 3)
        pytest.skip("Windows account cannot create a disposable test symlink")
    finally:
        release.set()
    result = await asyncio.wait_for(task, 3)
    assert result.state == "unknown"
    assert not (outside / "report.txt").exists()
    assert not (original / "report.txt").exists()


@pytest.mark.asyncio
async def test_approval_rejects_a_scope_whose_directory_was_replaced(environment, tmp_path):
    from contextlib import suppress
    from orchestrator.permissions.authority import InvalidDecision

    authority, issuer, context, _, _, _, _ = environment
    project, outside = tmp_path / "project", tmp_path / "outside"
    project.mkdir()
    outside.mkdir()
    gate, _, _ = make_gate(environment, tmp_path, ["create", "write"])
    task = asyncio.create_task(gate.execute(
        "operation", "file_write", {"path": "project/report.txt", "content": "approved"}, reason="report",
    ))
    request = await find_pending(authority, context)
    facts = authority.operation_status_for_task(context, "operation")
    original = tmp_path / "original-project"
    os.replace(project, original)
    try:
        os.symlink(outside, project, target_is_directory=True)
    except OSError:
        os.replace(original, project)
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        pytest.skip("Windows account cannot create a disposable test symlink")
    try:
        with pytest.raises(InvalidDecision):
            issuer.approve(authority, context, request, facts.arguments_digest)
        assert authority.decision_count(request.request_id) == 0
        assert not (outside / "report.txt").exists()
        assert not (original / "report.txt").exists()
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_cancel_before_file_start_prevents_the_effect(environment, tmp_path, monkeypatch):
    import orchestrator.permissions.windows_file_supervisor as module

    authority, issuer, context, _, _, _, _ = environment
    gate, waiter, _ = make_gate(environment, tmp_path, ["create", "write"])
    entered, release = Event(), Event()
    real_open = module.open_bound_create

    def delayed_open(scope):
        entered.set()
        if not release.wait(5):
            raise TimeoutError("test did not release file binding")
        return real_open(scope)

    monkeypatch.setattr(module, "open_bound_create", delayed_open)
    task = asyncio.create_task(gate.execute(
        "operation", "file_write", {"path": "cancelled.txt", "content": "must not appear"}, reason="report",
    ))
    await approve_pending(authority, issuer, context, waiter)
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        await asyncio.sleep(0)
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 3)
    assert not (tmp_path / "cancelled.txt").exists()
    assert authority.operation_status_for_task(context, "operation").state == "waiting"


@pytest.mark.parametrize("cleanup", ["verified", "unverified", "revoked"])
@pytest.mark.asyncio
async def test_cancel_after_prepare_releases_once_only_after_verified_cleanup(environment, tmp_path, monkeypatch, cleanup):
    authority, issuer, context, _, _, _, supervisor = environment
    if cleanup == "unverified":
        supervisor._issue_stop = lambda _ctx, _claim: "unverified"
    gate, waiter, _ = make_gate(environment, tmp_path, ["create", "write"])
    entered, release = Event(), Event()
    real_prepare = authority.prepare
    claims = []

    def delayed_prepare(*args):
        claim = real_prepare(*args)
        claims.append(claim.claim_id)
        entered.set()
        if not release.wait(5):
            raise TimeoutError("test did not release the prepared claim")
        return claim

    monkeypatch.setattr(authority, "prepare", delayed_prepare)
    task = asyncio.create_task(gate.execute(
        "operation", "file_write", {"path": "cancelled.txt", "content": "must not appear"}, reason="report",
    ))
    _, lease = await approve_pending(authority, issuer, context, waiter)
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        await asyncio.sleep(0)
        if cleanup == "revoked":
            authority.revoke(context, lease.lease_id)
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 3)
    assert not (tmp_path / "cancelled.txt").exists()
    status = authority.operation_status_for_task(context, "operation")
    if cleanup == "unverified":
        assert status.state == "prepared"
        assert authority.resource_state(claims[0]) == "stopping"
        assert authority.get_lease(lease.lease_id).status == "reserved"
    else:
        assert status.state == "waiting"
        assert authority.resource_state(claims[0]) == "stopped"
        assert authority.get_lease(lease.lease_id).status == ("revoked" if cleanup == "revoked" else "active")


def test_supervisor_rejects_missing_or_unverified_pao_run_guard(environment, tmp_path):
    authority, _, _, _, results, issue_stop, _ = environment
    for guard in (None, lambda _context: None):
        with pytest.raises(ValueError):
            WindowsFileSupervisor(
                authority, cwd=tmp_path, read_roots=(tmp_path,), result_store=results,
                issue_stop=issue_stop, run_guard=guard,
            )
