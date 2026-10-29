"""Frontend decisions recover their exact canonical PAO execution binding."""
from dataclasses import replace
import sqlite3

import pytest

from orchestrator.permissions.authority import OperationConflict, PermissionAuthority, PermissionDenied
from orchestrator.permissions.models import PermissionSubject, normalize_scope
from orchestrator.permissions.pao_run_fence import PaoRunFence
from tests.test_permission_pao_fence import setup_run


class NoApprovals:
    def verify(self, reference):
        return None


@pytest.fixture
def bound(tmp_path):
    store, session, accepted, fence, context = setup_run(tmp_path)
    authority = PermissionAuthority(tmp_path / "permissions.sqlite", verifier=NoApprovals())
    authority.open_task(context)
    scope = normalize_scope("capability", "reports", ["read"], cwd=tmp_path)
    request = authority.request_access(context, [scope], purpose="Read reports")
    subject = PermissionSubject(
        context.instance_id, context.owner_id, context.agent_id, context.session_id,
        context.context_generation,
    )
    guard = PaoRunFence(
        store, context=context, request_id=accepted.request_id,
        fencing_token=fence, worker_id="INSTANCE:agent",
    )
    return store, authority, context, subject, guard, request


def test_runtime_binding_is_durable_exact_and_not_rebuilt_from_frontend_fields(bound):
    store, authority, context, subject, guard, request = bound
    registered = authority.register_runtime_context(context, run_guard=guard)
    assert registered == authority.get_request_runtime_binding(subject, request.request_id)
    assert registered.context == context
    assert registered.request_id == "request" and registered.fencing_token == guard.binding.fencing_token
    assert authority.register_runtime_context(context, run_guard=guard) == registered
    altered = replace(context, turn_id="invented-turn")
    altered_guard = PaoRunFence(
        store, context=altered, request_id=registered.request_id,
        fencing_token=registered.fencing_token, worker_id=registered.worker_id,
    )
    with pytest.raises(OperationConflict, match="immutable"):
        authority.register_runtime_context(altered, run_guard=altered_guard)
    with pytest.raises(PermissionDenied):
        authority.get_request_runtime_binding(replace(subject, owner_id="other"), request.request_id)
    assert authority.decision_count(request.request_id) == 0

    # Recovery preserves the recorded Turn but never revives the old executor.
    reopened = PermissionAuthority(authority.path, verifier=NoApprovals())
    assert reopened.get_request_runtime_binding(subject, request.request_id) == registered
    assert reopened.execution_status(context).binding_state == "fenced"


def test_stopped_pao_run_cannot_register_or_reuse_a_runtime_binding(bound):
    store, authority, context, subject, guard, request = bound
    store.cancel_run(context.run_id, owner_id=context.owner_id)
    with pytest.raises(PermissionDenied):
        authority.register_runtime_context(context, run_guard=guard)
    with pytest.raises(PermissionDenied):
        authority.get_request_runtime_binding(subject, request.request_id)


@pytest.mark.parametrize("conflict", [False, True])
def test_v6_runtime_metadata_migration_preserves_requests_and_never_invents_claims(bound, conflict):
    _, authority, context, subject, _, request = bound
    with sqlite3.connect(authority.path) as connection:
        connection.execute("DROP TABLE execution_runtime_metadata")
        connection.execute("PRAGMA user_version=6")
        if conflict:
            connection.execute("CREATE TABLE execution_runtime_metadata (keep TEXT)")
            connection.execute("INSERT INTO execution_runtime_metadata VALUES('preserve')")
    if conflict:
        with pytest.raises(sqlite3.OperationalError):
            PermissionAuthority(authority.path, verifier=NoApprovals())
    else:
        reopened = PermissionAuthority(authority.path, verifier=NoApprovals())
        assert reopened.get_request(context, request.request_id).scopes == request.scopes
        with pytest.raises(PermissionDenied):
            reopened.get_request_runtime_binding(subject, request.request_id)
    with sqlite3.connect(authority.path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == (6 if conflict else 7)
        if conflict:
            assert connection.execute("SELECT keep FROM execution_runtime_metadata").fetchone() == ("preserve",)


def test_parked_request_rebind_preserves_scope_and_invalidates_old_actions(bound):
    store, authority, context, subject, guard, request = bound
    authority.register_runtime_context(context, run_guard=guard)
    action = authority.issue_decision_action(
        context, request.request_id, request.revision, approved_scopes=request.scopes,
        choice="approve", lifetime="task", duration_seconds=120,
    )
    successor = replace(context, execution_epoch="next", worker_generation="next-worker")
    next_guard = PaoRunFence(
        store, context=successor, request_id=guard.binding.request_id,
        fencing_token=guard.binding.fencing_token, worker_id=guard.binding.worker_id,
    )
    authority.bind_execution(successor)
    with pytest.raises(OperationConflict):
        authority.rebind_parked_requests(successor, run_guard=next_guard)
    authority.fence_execution(context)
    authority.register_runtime_context(successor, run_guard=next_guard)
    assert authority.rebind_parked_requests(successor, run_guard=next_guard) == (request.request_id,)
    adopted = authority.get_request(successor, request.request_id)
    assert adopted.revision == request.revision + 1
    assert adopted.scopes == request.scopes and adopted.expires_at == request.expires_at
    assert authority.get_request_runtime_binding(subject, request.request_id).context == successor
    from orchestrator.permissions.authority import InvalidDecision
    with pytest.raises((InvalidDecision, PermissionDenied)):
        authority.get_decision_action(context, action.action_id)
    assert authority.rebind_parked_requests(successor, run_guard=next_guard) == ()
