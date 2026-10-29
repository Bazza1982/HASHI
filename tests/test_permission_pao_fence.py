"""Permission execution must honor the canonical PAO Run fence."""
import asyncio
from dataclasses import replace
from hashlib import sha256
import os
from threading import Event

import pytest

from orchestrator.permissions.authority import PermissionAuthority, PermissionDenied, ResourceStopReceipt
from orchestrator.permissions.execution_gate import PermissionExecutionGate
from orchestrator.permissions.models import ExecutionContext, normalize_scope
from orchestrator.permissions.pao_run_fence import PaoRunFence
from orchestrator.permissions.result_store import EncryptedResultStore
from orchestrator.permissions.waiting import PermissionWaiter
from orchestrator.permissions.windows_file_supervisor import WindowsFileSupervisor
from orchestrator.session_store import SessionStore, StaleFencingToken
from tests.test_permission_execution_gate import Issuer, find_pending


def setup_run(tmp_path):
    store = SessionStore(tmp_path / "state" / "sessions.sqlite3", instance_id="INSTANCE")
    session = store.create_session(owner_id="owner", agent_id="agent")
    accepted = store.accept_run(
        session_id=session["session_id"], owner_id="owner", agent_id="agent",
        request_id="request", text="write report", source="test", idempotency_key="request",
    )
    fence = store.mark_request_running(accepted.request_id, worker_id="INSTANCE:agent")
    context = ExecutionContext(
        instance_id="INSTANCE", owner_id="owner", agent_id="agent", task_id="task",
        run_id=accepted.run_id, session_id=session["session_id"], turn_id="turn",
        worker_generation="worker", context_generation=str(session["context_generation"]),
        execution_epoch="epoch", backend_id="herv3", executor_id="test-file-supervisor",
        policy_revision=1,
    )
    return store, session, accepted, fence, context


def make_guarded_gate(tmp_path, store, accepted, fence, context):
    issuer = Issuer()
    authority = PermissionAuthority(
        tmp_path / "ledger.sqlite", verifier=issuer, clock=lambda: 1000.0, monotonic=lambda: 1000.0,
    )
    authority.open_task(context)
    results = EncryptedResultStore(tmp_path / "results.sqlite", key=bytes(range(32)))

    def stop_receipt(ctx, claim_id):
        reference = "stop-" + claim_id
        issuer.stop_proofs[reference] = ResourceStopReceipt(
            nonce=reference, claim_id=claim_id, instance_id=ctx.instance_id, task_id=ctx.task_id,
            execution_epoch=ctx.execution_epoch, observer_id=ctx.executor_id,
            evidence_digest=sha256(claim_id.encode()).hexdigest(), expires_at=1030.0,
        )
        return reference

    supervisor = WindowsFileSupervisor(
        authority, cwd=tmp_path, read_roots=(tmp_path,), result_store=results, issue_stop=stop_receipt,
        run_guard=PaoRunFence(store, context=context, request_id=accepted.request_id,
                              fencing_token=fence, worker_id="INSTANCE:agent"),
    )
    waiter = PermissionWaiter(authority, reconcile_seconds=.02)
    ceiling = normalize_scope("file", str(tmp_path), ["create", "write"], cwd=tmp_path)
    gate = PermissionExecutionGate(
        authority, waiter, supervisor, context=context, cwd=tmp_path,
        read_roots=(tmp_path,), requestable_scopes=(ceiling,),
    )
    return issuer, authority, waiter, gate


def run_binding(context, accepted, fence):
    return {
        "run_id": context.run_id, "owner_id": context.owner_id, "agent_id": context.agent_id,
        "session_id": context.session_id, "request_id": accepted.request_id,
        "context_generation": int(context.context_generation), "fencing_token": fence,
        "worker_id": "INSTANCE:agent",
    }


@pytest.mark.parametrize(("field", "replacement"), [
    ("run_id", "another-run"), ("owner_id", "another-owner"), ("agent_id", "another-agent"),
    ("session_id", "another-session"), ("request_id", "another-request"),
    ("context_generation", 2), ("fencing_token", 2), ("worker_id", "another-worker"),
])
def test_pao_run_fence_rejects_each_mismatched_binding(tmp_path, field, replacement):
    store, _, accepted, fence, context = setup_run(tmp_path)
    binding = run_binding(context, accepted, fence)
    with store.hold_active_run(**binding):
        pass
    binding[field] = replacement
    with pytest.raises(StaleFencingToken):
        with store.hold_active_run(**binding):
            pytest.fail("mismatched Run binding was accepted")


@pytest.mark.parametrize("bad", [True, False, 0, -1, "1"])
def test_pao_run_fence_rejects_invalid_generations(tmp_path, bad):
    store, _, accepted, fence, context = setup_run(tmp_path)
    binding = run_binding(context, accepted, fence)
    binding["fencing_token"] = bad
    with pytest.raises(ValueError):
        with store.hold_active_run(**binding):
            pytest.fail("invalid Run fence was accepted")


def test_stopped_and_replaced_context_cannot_use_a_stale_guard(tmp_path):
    store, session, accepted, fence, context = setup_run(tmp_path)
    guard = PaoRunFence(store, context=context, request_id=accepted.request_id,
                        fencing_token=fence, worker_id="INSTANCE:agent")
    with pytest.raises(PermissionDenied):
        with guard(replace(context, task_id="another-task")):
            pytest.fail("another permission task was accepted")
    store.cancel_run(accepted.run_id, owner_id=context.owner_id)
    store.start_fresh_generation(session["session_id"])
    with pytest.raises(PermissionDenied):
        with guard(context):
            pytest.fail("stale Run continued after stop and fresh context")


@pytest.mark.skipif(os.name != "nt", reason="native Windows file handles required")
@pytest.mark.asyncio
async def test_run_stopped_while_permission_waits_cannot_write(tmp_path):
    store, _, accepted, fence, context = setup_run(tmp_path)
    issuer, authority, waiter, gate = make_guarded_gate(tmp_path, store, accepted, fence, context)
    task = asyncio.create_task(gate.execute(
        "operation", "file_write", {"path": "blocked.txt", "content": "must not appear"}, reason="report",
    ))
    request = await find_pending(authority, context)
    operation = authority.operation_status_for_task(context, "operation")
    store.cancel_run(accepted.run_id, owner_id="owner")
    issuer.approve(authority, context, request, operation.arguments_digest)
    waiter.notify()
    result = await asyncio.wait_for(task, 4)
    assert result.state == "permission_changed"
    assert not (tmp_path / "blocked.txt").exists()


@pytest.mark.skipif(os.name != "nt", reason="native Windows file handles required")
@pytest.mark.asyncio
async def test_pao_stop_and_inflight_file_effect_have_a_single_order(tmp_path, monkeypatch):
    store, _, accepted, fence, context = setup_run(tmp_path)
    peer = SessionStore(store.db_path, instance_id=store.instance_id)
    issuer, authority, waiter, gate = make_guarded_gate(tmp_path, store, accepted, fence, context)
    entered, release, stop_started, stop_done = Event(), Event(), Event(), Event()
    original = authority.dispatch_guarded

    def delayed_dispatch(*args, **kwargs):
        entered.set()
        if not release.wait(5):
            raise TimeoutError("test did not release file effect")
        return original(*args, **kwargs)

    def stop():
        stop_started.set()
        try:
            return peer.cancel_run(accepted.run_id, owner_id=context.owner_id)
        finally:
            stop_done.set()

    monkeypatch.setattr(authority, "dispatch_guarded", delayed_dispatch)
    arguments = {"path": "ordered.txt", "content": "one effect"}
    task = asyncio.create_task(gate.execute("operation", "file_write", arguments, reason="report"))
    request = await find_pending(authority, context)
    facts = authority.operation_status_for_task(context, "operation")
    issuer.approve(authority, context, request, facts.arguments_digest)
    waiter.notify()
    stop_task = None
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        stop_task = asyncio.create_task(asyncio.to_thread(stop))
        assert await asyncio.to_thread(stop_started.wait, 2)
        assert not await asyncio.to_thread(stop_done.wait, .15)
    finally:
        release.set()
    result = await asyncio.wait_for(task, 5)
    assert result.state == "succeeded"
    assert (tmp_path / "ordered.txt").read_text(encoding="utf-8") == "one effect"
    stopped = await asyncio.wait_for(stop_task, 5)
    assert stopped["state"] == "stopped"
    assert (await gate.execute("operation", "file_write", arguments, reason="report")).state == "result_unavailable"
    assert (tmp_path / "ordered.txt").read_text(encoding="utf-8") == "one effect"
