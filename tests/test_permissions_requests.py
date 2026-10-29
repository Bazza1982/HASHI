"""Permission request snapshots, withdrawal and recovery, using a real ledger."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import sqlite3
from threading import Barrier

import pytest

from orchestrator.permissions.authority import (
    DecisionReceipt,
    InvalidDecision,
    OperationConflict,
    PermissionAuthority,
    PermissionDenied,
    RevisionConflict,
)
from orchestrator.permissions.models import ExecutionContext, normalize_scope, scope_digest


class ReceiptSource:
    def __init__(self):
        self.values = {}

    def verify(self, reference):
        return self.values.get(reference)

    def issue(self, request, context, scopes, *, nonce):
        receipt = DecisionReceipt(
            nonce=nonce, request_id=request.request_id, request_revision=request.revision,
            requested_digest=request.scope_digest, approved_digest=scope_digest(scopes),
            instance_id=context.instance_id, task_id=context.task_id, run_id=context.run_id,
            execution_epoch=context.execution_epoch, operation_id=request.operation_id,
            arguments_digest="a" * 64 if request.operation_id else None,
            policy_revision=context.policy_revision, owner_id=context.owner_id,
            agent_id=context.agent_id, session_id=context.session_id,
            context_generation=context.context_generation, principal_id=context.owner_id,
            choice="approve", lifetime="task", duration_seconds=60,
            idempotency_key="decision-" + nonce, expires_at=1_030.0,
        )
        self.values[nonce] = receipt
        return nonce


@pytest.fixture
def environment(tmp_path):
    issuer = ReceiptSource()
    clock = [1_000.0]
    context = ExecutionContext(
        instance_id="instance", owner_id="owner", agent_id="agent",
        task_id="task-a", run_id="run-a", session_id="session",
        turn_id="turn", worker_generation="worker",
        context_generation="generation", execution_epoch="epoch-a",
        backend_id="herv3", executor_id="registry", policy_revision=1,
    )
    authority = PermissionAuthority(
        tmp_path / "permissions.sqlite", verifier=issuer,
        clock=lambda: clock[0], monotonic=lambda: clock[0],
    )
    authority.open_task(context)
    root = tmp_path / "project"
    root.mkdir()
    scope = normalize_scope("file", str(root), ["read", "write"], cwd=tmp_path)
    read = normalize_scope("file", str(root), ["read"], cwd=tmp_path)
    return authority, issuer, clock, context, scope, read


def test_narrow_preserves_old_snapshot_and_rejects_old_decision(environment):
    authority, issuer, _, context, scope, read = environment
    request = authority.request_access(
        context, [scope], purpose="project", operation_id="op", arguments_digest="a" * 64,
    )
    stale = issuer.issue(request, context, [scope], nonce="stale")
    narrowed = authority.narrow_request(context, request.request_id, request.revision, [read])
    assert narrowed.revision == 2
    assert narrowed.scopes == (read,)
    assert authority.get_request(context, request.request_id) == narrowed
    assert authority.get_request_version(context, request.request_id, 1).scopes == (scope,)
    assert authority.get_request_version(context, request.request_id, 2).scopes == (read,)
    assert not authority.get_request_version(context, request.request_id, 1).is_current_revision
    assert authority.get_request_version(context, request.request_id, 2).is_current_revision
    with pytest.raises(RevisionConflict):
        authority.decide(stale, approved_scopes=[scope])
    assert not authority.evaluate(context, [read])
    fresh = issuer.issue(narrowed, context, [read], nonce="fresh")
    authority.decide(fresh, approved_scopes=[read])
    assert authority.evaluate(context, [read])
    assert not authority.evaluate(context, [scope])
    with pytest.raises(PermissionDenied):
        authority.prepare(context, "op", "a" * 64, [scope])


def test_partial_approval_asks_only_for_remaining_actions(environment, tmp_path):
    authority, issuer, _, context, scope, read = environment
    request = authority.request_access(
        context, [scope], purpose="project", operation_id="op", arguments_digest="a" * 64,
    )
    narrowed = authority.narrow_request(context, request.request_id, 1, [read])
    # Retrying the same operation while a narrowed preview is pending does
    # not create a second request or enlarge what the user is seeing.
    repeated = authority.request_access(
        context, [scope], purpose="retry", operation_id="op", arguments_digest="a" * 64,
    )
    assert repeated == narrowed
    authority.decide(issuer.issue(narrowed, context, [read], nonce="read"), approved_scopes=[read])
    remaining = authority.request_access(
        context, [scope], purpose="remaining", operation_id="op", arguments_digest="a" * 64,
    )
    write = normalize_scope("file", scope.resource, ["write"], cwd=tmp_path)
    assert remaining.scopes == (write,)
    authority.decide(issuer.issue(remaining, context, [write], nonce="write"), approved_scopes=[write])
    assert authority.evaluate(context, [scope])
    claim = authority.prepare(context, "op", "a" * 64, [scope])
    assert authority.start(context, claim.claim_id).operation_id == "op"


def test_execution_rebind_invalidates_old_action_and_keeps_original_operation(environment):
    authority, issuer, _, context, scope, _ = environment
    request = authority.request_access(
        context, [scope], purpose="project", operation_id="op", arguments_digest="a" * 64,
    )
    stale = issuer.issue(request, context, [scope], nonce="old-epoch")
    newer = replace(context, execution_epoch="epoch-b", worker_generation="worker-b",
                    context_generation="generation-b")
    authority.bind_execution(newer)
    with pytest.raises(OperationConflict):
        authority.request_access(newer, [scope], purpose="same", operation_id="op",
                                 arguments_digest="a" * 64)
    authority.fence_execution(context)
    rebound = authority.request_access(
        newer, [scope], purpose="same", operation_id="op", arguments_digest="a" * 64,
    )
    assert rebound.request_id == request.request_id
    assert rebound.revision == request.revision + 1
    assert rebound.expires_at == request.expires_at
    with sqlite3.connect(authority.path) as connection:
        snapshots = connection.execute(
            "SELECT revision,execution_epoch,context_generation,operation_id,arguments_digest "
            "FROM access_request_versions WHERE request_id=? ORDER BY revision",
            (request.request_id,),
        ).fetchall()
    assert snapshots == [
        (1, context.execution_epoch, context.context_generation, "op", "a" * 64),
        (2, newer.execution_epoch, newer.context_generation, "op", "a" * 64),
    ]
    with pytest.raises(RevisionConflict):
        authority.decide(stale, approved_scopes=[scope])
    reference = issuer.issue(rebound, newer, [scope], nonce="new-epoch")
    authority.decide(reference, approved_scopes=[scope])
    assert not authority.evaluate(context, [scope])
    assert authority.evaluate(newer, [scope])
    claim = authority.prepare(newer, "op", "a" * 64, [scope])
    assert authority.start(newer, claim.claim_id).operation_id == "op"


def test_pending_request_closes_when_scope_is_granted_elsewhere(environment):
    authority, issuer, _, context, scope, _ = environment
    pending = authority.request_access(
        context, [scope], purpose="project", operation_id="op", arguments_digest="a" * 64,
    )
    stale = issuer.issue(pending, context, [scope], nonce="pending")
    proactive = authority.request_access(context, [scope], purpose="proactive")
    authority.decide(
        issuer.issue(proactive, context, [scope], nonce="proactive"), approved_scopes=[scope]
    )
    assert authority.request_access(
        context, [scope], purpose="same", operation_id="op", arguments_digest="a" * 64,
    ).state == "granted_existing"
    assert authority.get_request(context, pending.request_id).state == "withdrawn"
    with pytest.raises(InvalidDecision):
        authority.decide(stale, approved_scopes=[scope])
    assert authority.start(context, authority.prepare(context, "op", "a" * 64, [scope]).claim_id)


def test_task_lease_cannot_move_pending_operation_from_live_epoch(environment):
    authority, issuer, _, context, scope, _ = environment
    request = authority.request_access(
        context, [scope], purpose="project", operation_id="op", arguments_digest="a" * 64,
    )
    authority.decide(issuer.issue(request, context, [scope], nonce="shared"), approved_scopes=[scope])
    newer = replace(context, execution_epoch="epoch-b", worker_generation="worker-b",
                    context_generation="generation-b")
    authority.bind_execution(newer)
    with pytest.raises(OperationConflict):
        authority.request_access(newer, [scope], purpose="same", operation_id="op",
                                 arguments_digest="a" * 64)
    with pytest.raises(OperationConflict):
        authority.prepare(newer, "op", "a" * 64, [scope])
    authority.fence_execution(context)
    assert authority.request_access(
        newer, [scope], purpose="same", operation_id="op", arguments_digest="a" * 64,
    ).state == "granted_existing"
    claim = authority.prepare(newer, "op", "a" * 64, [scope])
    assert authority.start(newer, claim.claim_id).operation_id == "op"


def test_invalid_narrow_and_cross_task_views_do_not_change_request(environment, tmp_path):
    authority, _, _, context, scope, read = environment
    request = authority.request_access(context, [read], purpose="project")
    with pytest.raises(InvalidDecision):
        authority.narrow_request(context, request.request_id, 1, [scope])
    assert authority.get_request(context, request.request_id) == request
    other = replace(context, task_id="task-b", run_id="run-b", execution_epoch="epoch-b")
    authority.open_task(other)
    for read_other in (
        lambda: authority.get_request(other, request.request_id),
        lambda: authority.get_request_version(other, request.request_id, 1),
        lambda: authority.withdraw_request(other, request.request_id, 1),
    ):
        with pytest.raises(PermissionDenied):
            read_other()
    assert authority.list_requests(other) == ()


def test_withdraw_pending_cancels_wait_without_revoking_an_existing_lease(environment):
    authority, issuer, _, context, scope, read = environment
    request = authority.request_access(
        context, [scope], purpose="project", operation_id="op", arguments_digest="a" * 64,
    )
    narrowed = authority.narrow_request(context, request.request_id, 1, [read])
    with pytest.raises(RevisionConflict):
        authority.withdraw_request(context, request.request_id, 1)
    withdrawn = authority.withdraw_request(context, request.request_id, narrowed.revision)
    assert withdrawn.state == "withdrawn"
    assert authority.operation_state("op") == "cancelled"
    with pytest.raises(OperationConflict):
        authority.request_access(context, [scope], purpose="same", operation_id="op", arguments_digest="a" * 64)

    second = authority.request_access(context, [read], purpose="new")
    ref = issuer.issue(second, context, [read], nonce="second")
    lease = authority.decide(ref, approved_scopes=[read])
    assert authority.withdraw_request(context, second.request_id, second.revision).state == "decided"
    assert authority.get_lease(lease.lease_id).status == "active"
    assert authority.evaluate(context, [read])


def test_expired_request_can_be_reissued_without_reviving_old_revision(environment):
    authority, issuer, clock, context, scope, _ = environment
    old = authority.request_access(
        context, [scope], purpose="project", operation_id="op", arguments_digest="a" * 64,
    )
    stale = issuer.issue(old, context, [scope], nonce="expired")
    clock[0] += 24 * 60 * 60
    assert authority.get_request(context, old.request_id).state == "expired"
    new = authority.request_access(
        context, [scope], purpose="project", operation_id="op", arguments_digest="a" * 64,
    )
    assert new.request_id != old.request_id
    assert new.operation_id == old.operation_id
    with pytest.raises(InvalidDecision):
        authority.decide(stale, approved_scopes=[scope])
    assert not authority.evaluate(context, [scope])


def test_withdraw_and_approve_have_one_transactional_winner(environment):
    authority, issuer, _, context, scope, _ = environment
    request = authority.request_access(context, [scope], purpose="project")
    reference = issuer.issue(request, context, [scope], nonce="race")
    barrier = Barrier(2)

    def approve():
        barrier.wait()
        try:
            authority.decide(reference, approved_scopes=[scope])
            return "approved"
        except InvalidDecision:
            return "rejected"

    def withdraw():
        barrier.wait()
        return authority.withdraw_request(context, request.request_id, 1).state

    with ThreadPoolExecutor(max_workers=2) as pool:
        a, b = pool.submit(approve), pool.submit(withdraw)
        outcome = (a.result(), b.result())
    assert outcome in {("approved", "decided"), ("rejected", "withdrawn")}
    assert authority.evaluate(context, [scope]) is (outcome[0] == "approved")


def test_v2_upgrade_keeps_live_request_and_adds_safe_suggestions(environment):
    authority, issuer, clock, context, scope, _ = environment
    request = authority.request_access(context, [scope], purpose="keep this request")
    with sqlite3.connect(authority.path) as connection:
        connection.execute("DROP TABLE decision_action_presentations")
        connection.execute("DROP TABLE decision_actions")
        connection.execute("DROP TABLE request_idempotency")
        connection.execute("DROP TABLE operation_outcome_receipts")
        for table in ("access_requests", "access_request_versions"):
            for column in ("suggested_lifetime", "suggested_seconds", "request_key"):
                connection.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
        connection.execute("PRAGMA user_version=2")
    reopened = PermissionAuthority(
        authority.path, verifier=issuer, clock=lambda: clock[0], monotonic=lambda: clock[0],
    )
    restored = reopened.get_request(context, request.request_id)
    assert restored.scopes == request.scopes
    assert restored.purpose == "keep this request"
    assert restored.suggested_lifetime == "task"
    assert restored.suggested_seconds == 14_400
    assert restored.request_key is None
    assert reopened.get_request_version(context, request.request_id, 1).scopes == request.scopes


@pytest.mark.parametrize("conflicting_table", [False, True])
def test_v1_to_v6_migration_is_atomic_and_preserves_existing_request(tmp_path, conflicting_table):
    path = tmp_path / "legacy.sqlite"
    from orchestrator.permissions.authority import _REQUEST_VERSIONS_SCHEMA, _SCHEMA

    with sqlite3.connect(path) as connection:
        # A v1 ledger contains the complete authority schema, without the
        # request-version table or the v3 suggestion/idempotency fields.
        for statement in _SCHEMA:
            if statement != _REQUEST_VERSIONS_SCHEMA:
                connection.execute(statement)
        connection.execute(
            "INSERT INTO permission_tasks VALUES(?,?,?,?,?,?,?,?,?)",
            ("task", "instance", "owner", "agent", "session", "active", 1, 1000.0, None),
        )
        connection.execute(
            "INSERT INTO execution_bindings VALUES(?,?,?,?,?,?,?,?,?)",
            ("epoch", "task", "run", "worker", "generation", "herv3", "registry", 7, "active"),
        )
        connection.execute(
            "INSERT INTO access_requests VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("legacy", "task", "run", "session", "generation", "epoch", 7, "op", "a" * 64,
             "old-scope", "b" * 64, "keep this reason", 1, "pending", 1000.0, 4000000000.0),
        )
        connection.execute("PRAGMA user_version=1")
        if conflicting_table:
            connection.execute("CREATE TABLE access_request_versions (keep TEXT)")
            connection.execute("INSERT INTO access_request_versions VALUES('keep this table')")

    if conflicting_table:
        with pytest.raises(sqlite3.OperationalError):
            PermissionAuthority(path, verifier=ReceiptSource())
    else:
        PermissionAuthority(path, verifier=ReceiptSource())

    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == (1 if conflicting_table else 6)
        assert connection.execute(
            "SELECT purpose FROM access_requests WHERE request_id='legacy'"
        ).fetchone() == ("keep this reason",)
        if conflicting_table:
            assert connection.execute("SELECT keep FROM access_request_versions").fetchone() == ("keep this table",)
        else:
            assert connection.execute(
                "SELECT request_id,revision,scopes_json,scope_digest,run_id,session_id,"
                "context_generation,execution_epoch,policy_revision,operation_id,arguments_digest,purpose "
                "FROM access_request_versions"
            ).fetchone() == (
                "legacy", 1, "old-scope", "b" * 64, "run", "session",
                "generation", "epoch", 7, "op", "a" * 64, "keep this reason",
            )
