"""UI action snapshots are durable bindings, never approval credentials."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier
import sqlite3

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import pytest

from orchestrator.permissions.authority import (
    DecisionReceipt,
    InvalidDecision,
    PermissionAuthority,
    PermissionDenied,
    RevisionConflict,
)
from orchestrator.permissions.models import ExecutionContext, normalize_scope
from orchestrator.permissions.signed_receipts import SignedReceiptVerifier
from tests.test_permission_signed_receipts import _public, _token


@pytest.fixture
def setup_actions(tmp_path):
    clock = [1000.0]
    context = ExecutionContext(
        instance_id="instance", owner_id="owner", agent_id="agent", task_id="task",
        run_id="run", session_id="session", turn_id="turn", worker_generation="worker",
        context_generation="generation", execution_epoch="epoch", backend_id="herv3",
        executor_id="executor", policy_revision=1,
    )
    key = Ed25519PrivateKey.generate()
    verifier = SignedReceiptVerifier({"trusted": _public(key)})
    authority = PermissionAuthority(
        tmp_path / "permissions.sqlite", verifier=verifier,
        clock=lambda: clock[0], monotonic=lambda: clock[0],
    )
    authority.open_task(context)
    scope = normalize_scope("file", str(tmp_path), ["read", "write"], cwd=tmp_path)
    request = authority.request_access(
        context, [scope], purpose="make report", operation_id="operation", arguments_digest="a" * 64,
    )
    return authority, key, verifier, clock, context, scope, request


def _issue(authority, context, request, scopes, **kwargs):
    values = dict(choice="approve", lifetime="once", duration_seconds=120, action_ttl_seconds=10)
    values.update(kwargs)
    return authority.issue_decision_action(
        context, request.request_id, request.revision, approved_scopes=scopes, **values,
    )


def _receipt(action, context):
    return DecisionReceipt(
        nonce=action.nonce, request_id=action.request_id, request_revision=action.request_revision,
        requested_digest=action.requested_digest, approved_digest=action.approved_digest,
        instance_id=context.instance_id, task_id=context.task_id, run_id=context.run_id,
        execution_epoch=context.execution_epoch, operation_id=action.operation_id,
        arguments_digest=action.arguments_digest, policy_revision=context.policy_revision,
        owner_id=context.owner_id, agent_id=context.agent_id, session_id=context.session_id,
        context_generation=context.context_generation, principal_id=context.owner_id,
        choice=action.choice, lifetime=action.lifetime, duration_seconds=action.duration_seconds,
        idempotency_key=action.action_id, expires_at=action.expires_at,
    )


def test_action_is_durable_but_issuing_or_presenting_it_never_grants(setup_actions):
    authority, _, _, _, context, scope, request = setup_actions
    action = _issue(authority, context, request, [scope])
    assert authority.get_decision_action(context, action.action_id) == action
    assert not authority.evaluate(context, [scope], operation_id="operation")
    with pytest.raises(InvalidDecision):
        authority.decide(action.action_id, approved_scopes=[scope], action_id=action.action_id)
    assert authority.decision_count(request.request_id) == 0


def test_signed_exact_action_commits_once_and_replays_after_card_expiry(setup_actions):
    authority, key, _, clock, context, scope, request = setup_actions
    action = _issue(authority, context, request, [scope])
    reference = _token(key, _receipt(action, context))
    lease = authority.decide(reference, approved_scopes=[scope], action_id=action.action_id)
    assert authority.evaluate(context, [scope], operation_id="operation")
    clock[0] += 11
    assert authority.decide(reference, approved_scopes=[scope], action_id=action.action_id) == lease
    assert authority.decision_count(request.request_id) == 1


def test_issued_action_cannot_skip_mapping_or_be_substituted(setup_actions):
    authority, key, _, _, context, scope, request = setup_actions
    first = _issue(authority, context, request, [scope])
    second = _issue(authority, context, request, [scope], lifetime="task")
    reference = _token(key, _receipt(first, context))
    for action_id in (None, second.action_id, "unknown"):
        with pytest.raises(InvalidDecision):
            authority.decide(reference, approved_scopes=[scope], action_id=action_id)
    assert not authority.evaluate(context, [scope], operation_id="operation")


@pytest.mark.parametrize("change", [
    {"duration_seconds": 121},
    {"lifetime": "task"},
    {"choice": "deny"},
    {"nonce": "different"},
    {"idempotency_key": "different"},
    {"expires_at": 1020.0},
    {"arguments_digest": "b" * 64},
])
def test_even_signed_receipt_cannot_change_the_displayed_action(setup_actions, change):
    authority, key, _, _, context, scope, request = setup_actions
    action = _issue(authority, context, request, [scope])
    reference = _token(key, replace(_receipt(action, context), **change))
    with pytest.raises(InvalidDecision):
        authority.decide(reference, approved_scopes=[scope], action_id=action.action_id)
    assert authority.decision_count(request.request_id) == 0


def test_old_action_never_approves_a_new_scope_revision(setup_actions, tmp_path):
    authority, key, _, _, context, scope, request = setup_actions
    action = _issue(authority, context, request, [scope])
    narrower = normalize_scope("file", str(tmp_path), ["read"], cwd=tmp_path)
    authority.narrow_request(context, request.request_id, request.revision, [narrower])
    with pytest.raises(RevisionConflict):
        authority.get_decision_action(context, action.action_id)
    with pytest.raises(RevisionConflict):
        authority.decide(_token(key, _receipt(action, context)),
                         approved_scopes=[scope], action_id=action.action_id)
    assert not authority.evaluate(context, [narrower], operation_id="operation")


def test_new_action_must_bind_exact_task_and_current_request(setup_actions):
    authority, _, _, _, context, scope, request = setup_actions
    action = _issue(authority, context, request, [scope])
    other = replace(context, session_id="other")
    with pytest.raises(PermissionDenied):
        authority.get_decision_action(other, action.action_id)
    with pytest.raises(PermissionDenied):
        _issue(authority, other, request, [scope])


def test_expired_action_cannot_issue_a_new_lease(setup_actions):
    authority, key, _, clock, context, scope, request = setup_actions
    action = _issue(authority, context, request, [scope])
    reference = _token(key, _receipt(action, context))
    clock[0] += 10
    with pytest.raises(InvalidDecision):
        authority.get_decision_action(context, action.action_id)
    with pytest.raises(InvalidDecision):
        authority.decide(reference, approved_scopes=[scope], action_id=action.action_id)
    assert authority.decision_count(request.request_id) == 0


def test_pause_allows_decision_but_not_execution_until_resumed(setup_actions):
    authority, key, _, _, context, scope, request = setup_actions
    authority.pause_task(context)
    action = _issue(authority, context, request, [scope])
    authority.decide(_token(key, _receipt(action, context)),
                     approved_scopes=[scope], action_id=action.action_id)
    assert not authority.evaluate(context, [scope], operation_id="operation")
    authority.resume_task(context)
    assert authority.evaluate(context, [scope], operation_id="operation")


def test_action_cannot_expand_or_silently_deny_a_subset(setup_actions, tmp_path):
    authority, _, _, _, context, scope, request = setup_actions
    outside = normalize_scope("file", str(tmp_path.parent), ["read"], cwd=tmp_path)
    with pytest.raises(InvalidDecision):
        _issue(authority, context, request, [outside])
    narrower = normalize_scope("file", str(tmp_path), ["read"], cwd=tmp_path)
    with pytest.raises(InvalidDecision):
        _issue(authority, context, request, [narrower], choice="deny")
    action = _issue(authority, context, request, [narrower])
    assert action.approved_scopes == (narrower,)


def test_two_distinct_concurrent_actions_cannot_create_two_decisions(setup_actions):
    authority, key, _, _, context, scope, request = setup_actions
    actions = [_issue(authority, context, request, [scope]) for _ in range(2)]
    barrier = Barrier(2)

    def submit(action):
        reference = _token(key, _receipt(action, context))
        barrier.wait()
        try:
            authority.decide(reference, approved_scopes=[scope], action_id=action.action_id)
            return "committed"
        except InvalidDecision:
            return "stale"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(submit, actions))
    assert sorted(results) == ["committed", "stale"]
    assert authority.decision_count(request.request_id) == 1


@pytest.mark.parametrize("conflicting_table", [False, True])
def test_v4_action_migration_is_atomic_and_preserves_requests(setup_actions, conflicting_table):
    authority, _, verifier, clock, context, scope, request = setup_actions
    with sqlite3.connect(authority.path) as connection:
        connection.execute("DROP TABLE decision_action_presentations")
        connection.execute("DROP TABLE decision_actions")
        connection.execute("PRAGMA user_version=4")
        if conflicting_table:
            connection.execute("CREATE TABLE decision_actions (keep TEXT)")
            connection.execute("INSERT INTO decision_actions VALUES('preserve')")
    if conflicting_table:
        with pytest.raises(sqlite3.OperationalError):
            PermissionAuthority(authority.path, verifier=verifier,
                                clock=lambda: clock[0], monotonic=lambda: clock[0])
    else:
        reopened = PermissionAuthority(authority.path, verifier=verifier,
                                       clock=lambda: clock[0], monotonic=lambda: clock[0])
        assert reopened.get_request(context, request.request_id).scopes == (scope,)
    with sqlite3.connect(authority.path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == (4 if conflicting_table else 6)
        if conflicting_table:
            assert connection.execute("SELECT keep FROM decision_actions").fetchone() == ("preserve",)


@pytest.mark.parametrize("change", [
    {"idempotency_key": "another-key"},
    {"nonce": "another-nonce"},
])
def test_action_receipt_cannot_be_detached_from_its_registered_mapping(setup_actions, change):
    authority, key, _, _, context, scope, request = setup_actions
    action = _issue(authority, context, request, [scope])
    reference = _token(key, replace(_receipt(action, context), **change))
    with pytest.raises(InvalidDecision):
        authority.decide(reference, approved_scopes=[scope])
    assert authority.decision_count(request.request_id) == 0


def test_restart_fences_the_old_action_and_prevents_late_approval(setup_actions):
    authority, key, verifier, clock, context, scope, request = setup_actions
    action = _issue(authority, context, request, [scope])
    reopened = PermissionAuthority(authority.path, verifier=verifier,
                                   clock=lambda: clock[0], monotonic=lambda: clock[0])
    with pytest.raises(PermissionDenied):
        reopened.get_decision_action(context, action.action_id)
    with pytest.raises(InvalidDecision):
        reopened.decide(_token(key, _receipt(action, context)),
                        approved_scopes=[scope], action_id=action.action_id)
    assert reopened.decision_count(request.request_id) == 0


def test_reopening_rejects_an_action_table_with_missing_constraints(setup_actions):
    from orchestrator.permissions.authority import _DECISION_ACTIONS_SCHEMA

    authority, _, verifier, clock, _, _, _ = setup_actions
    damaged = _DECISION_ACTIONS_SCHEMA.replace("CHECK(choice IN ('approve','deny'))", "")
    assert damaged != _DECISION_ACTIONS_SCHEMA
    with sqlite3.connect(authority.path) as connection:
        connection.execute("DROP TABLE decision_actions")
        connection.execute(damaged)
    with pytest.raises(ValueError, match="constraints are incomplete"):
        PermissionAuthority(authority.path, verifier=verifier,
                            clock=lambda: clock[0], monotonic=lambda: clock[0])


def test_unbound_request_cannot_issue_once_action(setup_actions):
    authority, _, _, _, context, scope, _ = setup_actions
    request = authority.request_access(context, [scope], purpose="proactive request")
    with pytest.raises(InvalidDecision, match="once requires an operation"):
        _issue(authority, context, request, [scope])


def test_action_card_lifetime_is_bounded_by_the_request(setup_actions):
    authority, _, _, clock, context, scope, request = setup_actions
    clock[0] = request.expires_at - 5
    action = _issue(authority, context, request, [scope], action_ttl_seconds=10)
    assert action.expires_at == request.expires_at
    with pytest.raises(ValueError):
        _issue(authority, context, request, [scope], action_ttl_seconds=901)


def test_committed_action_can_be_reconstructed_for_idempotent_replay(setup_actions):
    authority, key, verifier, clock, context, scope, request = setup_actions
    action = _issue(authority, context, request, [scope])
    with pytest.raises(InvalidDecision):
        authority.get_committed_decision_action(context, action.action_id)
    reference = _token(key, _receipt(action, context))
    lease = authority.decide(reference, approved_scopes=[scope], action_id=action.action_id)
    clock[0] += 11
    reopened = PermissionAuthority(authority.path, verifier=verifier,
                                   clock=lambda: clock[0], monotonic=lambda: clock[0])
    assert reopened.get_committed_decision_action(context, action.action_id) == action
    assert reopened.decide(reference, approved_scopes=[scope], action_id=action.action_id) == lease
    with pytest.raises(PermissionDenied):
        reopened.get_committed_decision_action(replace(context, owner_id="other"), action.action_id)


def test_refresh_reuses_only_the_same_presentation_action_without_extending_it(setup_actions):
    authority, key, _, clock, context, scope, request = setup_actions
    view = {"presentation_id": "menu-one", "presentation_slot": "approve-once"}
    first = _issue(authority, context, request, [scope], **view)
    clock[0] += 1
    for _ in range(70):
        assert _issue(authority, context, request, [scope], **view) == first
    other = _issue(
        authority, context, request, [scope],
        presentation_id="menu-two", presentation_slot="approve-once",
    )
    assert other.action_id != first.action_id
    with pytest.raises(InvalidDecision):
        authority.decide(
            _token(key, _receipt(first, context)), approved_scopes=[scope], action_id=other.action_id,
        )
    assert authority.decision_count(request.request_id) == 0
    assert not authority.evaluate(context, [scope], operation_id="operation")
    clock[0] = first.expires_at
    with pytest.raises(InvalidDecision):
        _issue(authority, context, request, [scope], **view)
    assert _issue(
        authority, context, request, [scope],
        presentation_id="menu-three", presentation_slot="approve-once",
    ).action_id != first.action_id


def test_presentation_slot_cannot_be_rebound_to_a_different_decision(setup_actions, tmp_path):
    authority, _, _, clock, context, scope, request = setup_actions
    view = {"presentation_id": "menu-one", "presentation_slot": "approve-once"}
    first = _issue(authority, context, request, [scope], **view)
    narrower = normalize_scope("file", str(tmp_path), ["read"], cwd=tmp_path)
    for scopes, changes in (([scope], {"lifetime": "task"}), ([scope], {"choice": "deny"}),
                            ([scope], {"duration_seconds": 121}), ([narrower], {})):
        with pytest.raises(RevisionConflict):
            _issue(authority, context, request, scopes, **view, **changes)
    clock[0] += 1
    with pytest.raises(RevisionConflict):
        _issue(authority, context, request, [scope], **view, action_ttl_seconds=1)
    assert authority.get_decision_action(context, first.action_id) == first
    assert authority.decision_count(request.request_id) == 0


def test_concurrent_same_presentation_is_idempotent_and_grants_only_after_signed_decision(setup_actions):
    authority, key, _, _, context, scope, request = setup_actions
    barrier = Barrier(8)

    def render(_):
        barrier.wait()
        return _issue(authority, context, request, [scope],
                      presentation_id="menu-one", presentation_slot="approve-once")

    with ThreadPoolExecutor(max_workers=8) as pool:
        actions = list(pool.map(render, range(8)))
    assert len({action.action_id for action in actions}) == 1
    assert authority.decision_count(request.request_id) == 0
    action = actions[0]
    authority.decide(_token(key, _receipt(action, context)),
                     approved_scopes=[scope], action_id=action.action_id)
    assert authority.evaluate(context, [scope], operation_id="operation")
    assert authority.decision_count(request.request_id) == 1


@pytest.mark.parametrize("partial", [
    {"presentation_id": "menu-one"}, {"presentation_slot": "approve-once"},
    {"presentation_id": "", "presentation_slot": "approve-once"},
])
def test_presentation_identifier_must_be_a_complete_trusted_binding(setup_actions, partial):
    authority, _, _, _, context, scope, request = setup_actions
    with pytest.raises(ValueError):
        _issue(authority, context, request, [scope], **partial)


def test_new_request_revision_cannot_reuse_the_previous_slot(setup_actions, tmp_path):
    authority, _, _, _, context, scope, request = setup_actions
    first = _issue(authority, context, request, [scope],
                   presentation_id="menu-one", presentation_slot="approve-once")
    narrower = normalize_scope("file", str(tmp_path), ["read"], cwd=tmp_path)
    updated = authority.narrow_request(context, request.request_id, request.revision, [narrower])
    with pytest.raises(RevisionConflict):
        _issue(authority, context, updated, [narrower],
               presentation_id="menu-one", presentation_slot="approve-once")
    fresh = _issue(authority, context, updated, [narrower],
                   presentation_id="menu-two", presentation_slot="approve-once")
    assert fresh.action_id != first.action_id
    assert fresh.request_revision == updated.revision
    with pytest.raises(RevisionConflict):
        authority.get_decision_action(context, first.action_id)


def test_multiple_presentations_can_remain_independent(setup_actions):
    authority, _, _, _, context, scope, request = setup_actions
    actions = [
        _issue(authority, context, request, [scope], presentation_id=f"menu-{menu}",
               presentation_slot=f"choice-{slot}")
        for menu in range(12) for slot in range(6)
    ]
    assert len({action.action_id for action in actions}) == 72
    assert authority.decision_count(request.request_id) == 0
    assert not authority.evaluate(context, [scope], operation_id="operation")


@pytest.mark.parametrize("conflicting_table", [False, True])
def test_v5_presentation_migration_preserves_a_committed_action(setup_actions, conflicting_table):
    authority, key, verifier, clock, context, scope, request = setup_actions
    action = _issue(authority, context, request, [scope])
    reference = _token(key, _receipt(action, context))
    lease = authority.decide(reference, approved_scopes=[scope], action_id=action.action_id)
    with sqlite3.connect(authority.path) as connection:
        connection.execute("DROP TABLE decision_action_presentations")
        connection.execute("PRAGMA user_version=5")
        if conflicting_table:
            connection.execute("CREATE TABLE decision_action_presentations (keep TEXT)")
            connection.execute("INSERT INTO decision_action_presentations VALUES('preserve')")
    if conflicting_table:
        with pytest.raises(sqlite3.OperationalError):
            PermissionAuthority(authority.path, verifier=verifier,
                                clock=lambda: clock[0], monotonic=lambda: clock[0])
        assert authority.evaluate(context, [scope], operation_id="operation")
    else:
        reopened = PermissionAuthority(authority.path, verifier=verifier,
                                       clock=lambda: clock[0], monotonic=lambda: clock[0])
        assert reopened.get_committed_decision_action(context, action.action_id) == action
        assert reopened.decide(reference, approved_scopes=[scope], action_id=action.action_id) == lease
        assert reopened.decision_count(request.request_id) == 1
    with sqlite3.connect(authority.path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == (5 if conflicting_table else 6)
        if conflicting_table:
            assert connection.execute("SELECT keep FROM decision_action_presentations").fetchone() == ("preserve",)


def test_reopening_rejects_a_presentation_table_without_foreign_key(setup_actions):
    from orchestrator.permissions.authority import _DECISION_PRESENTATIONS_SCHEMA

    authority, _, verifier, clock, _, _, _ = setup_actions
    damaged = _DECISION_PRESENTATIONS_SCHEMA.replace(" REFERENCES decision_actions(action_id)", "")
    assert damaged != _DECISION_PRESENTATIONS_SCHEMA
    with sqlite3.connect(authority.path) as connection:
        connection.execute("DROP TABLE decision_action_presentations")
        connection.execute(damaged)
    with pytest.raises(ValueError, match="presentation constraints are incomplete"):
        PermissionAuthority(authority.path, verifier=verifier,
                            clock=lambda: clock[0], monotonic=lambda: clock[0])
