"""Session-scoped permission views must not enumerate other users or grant access."""
from dataclasses import replace
import sqlite3

import pytest

from orchestrator.permissions.authority import PermissionAuthority, PermissionDenied
from orchestrator.permissions.models import ExecutionContext, PermissionSubject, normalize_scope


class NoApprovals:
    def verify(self, reference):
        return None


@pytest.fixture
def views(tmp_path):
    clock = [1000.0]
    authority = PermissionAuthority(
        tmp_path / "permissions.sqlite", verifier=NoApprovals(),
        clock=lambda: clock[0], monotonic=lambda: clock[0],
    )
    context = ExecutionContext(
        instance_id="TEST", owner_id="user:7", agent_id="agent", task_id="task",
        run_id="run", session_id="session", turn_id="turn", worker_generation="worker",
        context_generation="1", execution_epoch="epoch", backend_id="her-v3",
        executor_id="files", policy_revision=1,
    )
    authority.open_task(context)
    subject = PermissionSubject("TEST", "user:7", "agent", "session", "1")
    requests = []
    for index in range(3):
        scope = normalize_scope("capability", f"report:{index}", ["read"], cwd=tmp_path)
        requests.append(authority.request_access(context, [scope], purpose=f"Read report {index}"))
    return authority, context, subject, clock, requests


def test_keyset_pages_and_request_details_are_bound_to_the_entire_subject(views):
    authority, context, subject, _, requests = views
    first = authority.list_session_requests(subject, limit=2)
    assert [view.request.request_id for view in first.requests] == [
        requests[2].request_id, requests[1].request_id,
    ]
    last = authority.list_session_requests(subject, before=first.next_before, limit=2)
    assert [view.request.request_id for view in last.requests] == [requests[0].request_id]
    assert last.next_before is None
    assert authority.get_session_request(subject, requests[1].request_id).request == requests[1]
    for name, value in (
        ("instance_id", "OTHER"), ("owner_id", "user:8"), ("agent_id", "other"),
        ("session_id", "other"), ("context_generation", "2"),
    ):
        other = replace(subject, **{name: value})
        assert authority.list_session_requests(other).requests == ()
        with pytest.raises(PermissionDenied):
            authority.get_session_request(other, requests[0].request_id)
        with pytest.raises(PermissionDenied):
            authority.list_session_requests(other, before=requests[0].request_id)
    assert authority.decision_count(requests[0].request_id) == 0
    assert not authority.evaluate(context, requests[0].scopes)


def test_views_use_current_revision_expiration_and_lifecycle_without_extending_it(views):
    authority, context, subject, clock, requests = views
    request = requests[0]
    authority.pause_task(context)
    view = authority.get_session_request(subject, request.request_id)
    assert view.task_state == "paused" and view.binding_state == "active"
    clock[0] = request.expires_at
    expired = authority.get_session_request(subject, request.request_id)
    assert expired.request.state == "expired"
    assert expired.request.expires_at == request.expires_at
    authority.end_task(context, outcome="cancelled")
    terminal = authority.get_session_request(subject, request.request_id)
    assert terminal.task_state == "cancelled"
    assert terminal.binding_state == "fenced"
    assert authority.decision_count(request.request_id) == 0


def test_corrupt_scope_snapshot_is_never_presented_as_a_valid_request(views):
    authority, _, subject, _, requests = views
    with sqlite3.connect(authority.path) as connection:
        connection.execute(
            "UPDATE access_requests SET scope_digest=? WHERE request_id=?",
            ("f" * 64, requests[0].request_id),
        )
    with pytest.raises(PermissionDenied):
        authority.get_session_request(subject, requests[0].request_id)
    with pytest.raises(PermissionDenied):
        authority.list_session_requests(subject)


def test_subject_and_page_limits_fail_closed(views):
    authority, _, subject, _, _ = views
    with pytest.raises(ValueError):
        PermissionSubject("TEST", "", "agent", "session", "1")
    for limit in (True, 0, 101):
        with pytest.raises(ValueError):
            authority.list_session_requests(subject, limit=limit)


def test_session_withdraw_is_versioned_idempotent_and_cancels_only_its_operation(views):
    from orchestrator.permissions.authority import RevisionConflict

    authority, context, subject, _, _ = views
    scope = normalize_scope("capability", "report:withdraw", ["read"], cwd=authority.path.parent)
    request = authority.request_access(
        context, [scope], purpose="Read report", operation_id="waiting-operation",
        arguments_digest="a" * 64,
    )
    with pytest.raises(PermissionDenied):
        authority.withdraw_session_request(replace(subject, owner_id="user:8"), request.request_id, 1)
    with pytest.raises(RevisionConflict):
        authority.withdraw_session_request(subject, request.request_id, 2)
    assert authority.get_request(context, request.request_id).state == "pending"
    view = authority.withdraw_session_request(subject, request.request_id, 1)
    assert view.request.state == "withdrawn"
    assert authority.operation_state("waiting-operation") == "cancelled"
    assert authority.withdraw_session_request(subject, request.request_id, 1) == view
    assert authority.decision_count(request.request_id) == 0
    assert not authority.evaluate(context, [scope], operation_id="waiting-operation")
