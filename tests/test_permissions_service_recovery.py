"""An authority restart fences old execution and preserves uncertain outcomes."""
from dataclasses import replace

import pytest

from orchestrator.permissions.authority import (
    OperationConflict,
    PermissionAuthority,
    PermissionDenied,
    ResourceStopReceipt,
)
from orchestrator.permissions.models import ExecutionContext, normalize_scope
from tests.test_permissions_authority import AuthenticatedReceiptSource, Clock, approve


@pytest.fixture
def service(tmp_path):
    clock = Clock()
    issuer = AuthenticatedReceiptSource()
    context = ExecutionContext(
        instance_id="instance", owner_id="owner", agent_id="agent", task_id="task",
        run_id="run", session_id="session", turn_id="turn", worker_generation="worker",
        context_generation="generation", execution_epoch="epoch", backend_id="herv3",
        executor_id="registry", policy_revision=1,
    )
    path = tmp_path / "permissions.sqlite"
    authority = PermissionAuthority(path, verifier=issuer, clock=clock, monotonic=clock)
    authority.open_task(context)
    scope = normalize_scope("file", str(tmp_path), ["read"], cwd=tmp_path)
    return path, authority, issuer, clock, context, scope


def _reopen(path, issuer, clock):
    return PermissionAuthority(path, verifier=issuer, clock=clock, monotonic=clock)


def _new_execution(context):
    return replace(context, worker_generation="worker-new", execution_epoch="epoch-new")


def test_restart_fences_old_service_and_waits_for_prepared_resource_cleanup(service):
    path, old, issuer, clock, context, scope = service
    approve(old, issuer, clock, context, [scope], lifetime="once",
            operation_id="operation", arguments_digest="a" * 64)
    claim = old.prepare(context, "operation", "a" * 64, [scope])

    new = _reopen(path, issuer, clock)
    with pytest.raises(PermissionDenied):
        old.start(context, claim.claim_id)
    with pytest.raises(PermissionDenied):
        new.start(context, claim.claim_id)
    assert new.resource_state(claim.claim_id) == "stopping"

    later = _new_execution(context)
    new.bind_execution(later)
    with pytest.raises(OperationConflict):
        new.prepare(later, "operation", "a" * 64, [scope])
    stop = ResourceStopReceipt(
        nonce="stop", claim_id=claim.claim_id, instance_id=context.instance_id,
        task_id=context.task_id, execution_epoch=context.execution_epoch,
        observer_id=context.executor_id, evidence_digest="b" * 64, expires_at=clock.value + 30,
    )
    new.confirm_resource_stopped(context, claim.claim_id, receipt_reference=issuer.publish_stop(stop))
    assert new.resource_state(claim.claim_id) == "stopped"
    assert new.prepare(later, "operation", "a" * 64, [scope]).state == "prepared"


def test_restart_marks_started_operation_unknown_and_never_reexecutes_it(service):
    path, old, issuer, clock, context, scope = service
    approve(old, issuer, clock, context, [scope])
    claim = old.prepare(context, "started-operation", "c" * 64, [scope])
    old.start(context, claim.claim_id)

    new = _reopen(path, issuer, clock)
    assert new.operation_status_for_task(context, "started-operation").state == "unknown"
    assert new.resource_state(claim.claim_id) == "stopping"
    with pytest.raises(PermissionDenied):
        old.finish(context, claim.claim_id, outcome="succeeded", result_digest="d" * 64)
    later = _new_execution(context)
    new.bind_execution(later)
    with pytest.raises(OperationConflict):
        new.prepare(later, "started-operation", "c" * 64, [scope])


def test_task_lease_survives_restart_only_after_a_new_execution_is_bound(service):
    path, old, issuer, clock, context, scope = service
    request, reference, lease = approve(old, issuer, clock, context, [scope])
    new = _reopen(path, issuer, clock)
    assert new.decide(reference, approved_scopes=[scope]) == lease
    assert new.decision_count(request.request_id) == 1
    with pytest.raises(PermissionDenied):
        old.evaluate(context, [scope])
    assert not new.evaluate(context, [scope])
    later = _new_execution(context)
    new.bind_execution(later)
    assert new.evaluate(later, [scope])


def test_failed_restart_does_not_displace_current_service(tmp_path):
    wall, mono = Clock(), Clock()
    issuer = AuthenticatedReceiptSource()
    context = ExecutionContext(
        instance_id="instance", owner_id="owner", agent_id="agent", task_id="task",
        run_id="run", session_id="session", turn_id="turn", worker_generation="worker",
        context_generation="generation", execution_epoch="epoch", backend_id="herv3",
        executor_id="registry", policy_revision=1,
    )
    path = tmp_path / "permissions.sqlite"
    old = PermissionAuthority(path, verifier=issuer, clock=wall, monotonic=mono)
    old.open_task(context)
    scope = normalize_scope("file", str(tmp_path), ["read"], cwd=tmp_path)
    approve(old, issuer, wall, context, [scope])
    wall.value += 10
    mono.value += 10
    assert old.evaluate(context, [scope])
    wall.value -= 5
    with pytest.raises(PermissionDenied):
        PermissionAuthority(path, verifier=issuer, clock=wall, monotonic=mono)
    wall.value += 5
    assert old.evaluate(context, [scope])


def test_incomplete_legacy_database_fails_without_committing_partial_migration(tmp_path):
    import sqlite3

    path = tmp_path / "incomplete.sqlite"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE access_requests (request_id TEXT, revision INTEGER, scopes_json TEXT, "
            "scope_digest TEXT, run_id TEXT, session_id TEXT, context_generation TEXT, "
            "execution_epoch TEXT, policy_revision INTEGER, operation_id TEXT, "
            "arguments_digest TEXT, purpose TEXT, created_at REAL)"
        )
        connection.execute("PRAGMA user_version=1")
    with pytest.raises(ValueError, match="incomplete"):
        PermissionAuthority(path, verifier=AuthenticatedReceiptSource())
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='access_request_versions'"
        ).fetchone() is None


def test_restart_racing_a_prepared_operation_has_one_safe_order(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    for index in range(8):
        clock = Clock()
        issuer = AuthenticatedReceiptSource()
        context = ExecutionContext(
            instance_id="instance", owner_id="owner", agent_id="agent", task_id="task",
            run_id="run", session_id="session", turn_id="turn", worker_generation="worker",
            context_generation="generation", execution_epoch="epoch", backend_id="herv3",
            executor_id="registry", policy_revision=1,
        )
        path = tmp_path / f"race-{index}.sqlite"
        authority = PermissionAuthority(path, verifier=issuer, clock=clock, monotonic=clock)
        authority.open_task(context)
        scope = normalize_scope("file", str(tmp_path), ["read"], cwd=tmp_path)
        approve(authority, issuer, clock, context, [scope])
        barrier = Barrier(2)

        def prepare():
            barrier.wait()
            try:
                return authority.prepare(context, "operation", "a" * 64, [scope])
            except PermissionDenied:
                return None

        def restart():
            barrier.wait()
            return _reopen(path, issuer, clock)

        with ThreadPoolExecutor(max_workers=2) as pool:
            future_claim = pool.submit(prepare)
            future_service = pool.submit(restart)
            claim = future_claim.result()
            restarted = future_service.result()

        if claim is not None:
            assert restarted.resource_state(claim.claim_id) == "stopping"
            with pytest.raises(PermissionDenied):
                restarted.start(context, claim.claim_id)
        with pytest.raises(PermissionDenied):
            authority.prepare(context, "another", "b" * 64, [scope])


def test_restart_rejects_missing_pending_request_uniqueness(service):
    import sqlite3

    path, old, issuer, clock, context, scope = service
    approve(old, issuer, clock, context, [scope])
    with sqlite3.connect(path) as connection:
        connection.execute("DROP INDEX one_pending_request")
    with pytest.raises(ValueError, match="incomplete"):
        _reopen(path, issuer, clock)
    # A failed takeover must not replace the live service generation.
    assert old.evaluate(context, [scope])


@pytest.mark.parametrize("conflicting_table", [False, True])
def test_v3_outcome_migration_is_atomic_and_preserves_decisions(service, conflicting_table):
    import sqlite3

    path, old, issuer, clock, context, scope = service
    request, reference, lease = approve(old, issuer, clock, context, [scope])
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TABLE decision_actions")
        connection.execute("DROP TABLE operation_outcome_receipts")
        connection.execute("PRAGMA user_version=3")
        if conflicting_table:
            connection.execute("CREATE TABLE operation_outcome_receipts (keep TEXT)")
            connection.execute("INSERT INTO operation_outcome_receipts VALUES('preserve')")
    if conflicting_table:
        with pytest.raises(sqlite3.OperationalError):
            _reopen(path, issuer, clock)
        assert old.evaluate(context, [scope])
    else:
        reopened = _reopen(path, issuer, clock)
        assert reopened.decide(reference, approved_scopes=[scope]) == lease
        assert reopened.decision_count(request.request_id) == 1
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == (3 if conflicting_table else 5)
        if conflicting_table:
            assert connection.execute("SELECT keep FROM operation_outcome_receipts").fetchone() == ("preserve",)


def test_restart_does_not_accept_stop_receipt_from_another_executor(service):
    path, old, issuer, clock, context, scope = service
    approve(old, issuer, clock, context, [scope], lifetime="once",
            operation_id="operation", arguments_digest="a" * 64)
    claim = old.prepare(context, "operation", "a" * 64, [scope])
    new = _reopen(path, issuer, clock)
    wrong = ResourceStopReceipt(
        nonce="stop", claim_id=claim.claim_id, instance_id=context.instance_id,
        task_id=context.task_id, execution_epoch=context.execution_epoch,
        observer_id="other-executor", evidence_digest="b" * 64, expires_at=clock.value + 30,
    )
    with pytest.raises(PermissionDenied):
        new.confirm_resource_stopped(context, claim.claim_id, receipt_reference=issuer.publish_stop(wrong))
    assert new.resource_state(claim.claim_id) == "stopping"
