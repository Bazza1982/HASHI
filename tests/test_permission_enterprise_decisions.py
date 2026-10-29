"""A governed session may submit only its owner's exact issued action."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime
from threading import Event
import sqlite3
import time

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import pytest

from orchestrator.enterprise import IdentityService
from orchestrator.permissions.authority import InvalidDecision, PermissionAuthority, PermissionDenied, RevisionConflict
from orchestrator.permissions.enterprise_decisions import (
    EnterpriseSessionDecisionPrototype,
    SessionDecisionDenied,
)
from orchestrator.permissions.models import ExecutionContext, normalize_scope
from orchestrator.permissions.signed_receipts import SignedReceiptVerifier


@pytest.fixture
def governed(tmp_path):
    identity = IdentityService.from_path(tmp_path / "identity.sqlite")
    identity.create_organization(org_id="test-org", name="Test")
    user = identity.create_user(
        org_id="test-org", email="owner@example.test", display_name="Owner",
        password="test-only-password", user_id="owner",
    )
    other = identity.create_user(
        org_id="test-org", email="other@example.test", display_name="Other",
        password="test-only-password", user_id="other",
    )
    assert identity.authenticate_user(
        org_id="test-org", email="owner@example.test", password="test-only-password",
    ) == user
    session = identity.create_session(user_id=user.id, ttl_seconds=600)
    key = Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw,
    )
    verifier = SignedReceiptVerifier({"approval": public})
    clock = [time.time()]
    authority = PermissionAuthority(
        tmp_path / "permissions.sqlite", verifier=verifier,
        clock=lambda: clock[0], monotonic=lambda: clock[0],
    )
    context = ExecutionContext(
        instance_id="instance", owner_id="enterprise:owner", agent_id="agent", task_id="task",
        run_id="run", session_id="session", turn_id="turn", worker_generation="worker",
        context_generation="generation", execution_epoch="epoch", backend_id="herv3",
        executor_id="executor", policy_revision=1,
    )
    authority.open_task(context)
    scope = normalize_scope("file", str(tmp_path), ["read", "write"], cwd=tmp_path)
    request = authority.request_access(
        context, [scope], purpose="review report", operation_id="operation", arguments_digest="a" * 64,
    )
    action = authority.issue_decision_action(
        context, request.request_id, request.revision, approved_scopes=[scope],
        choice="approve", lifetime="once", duration_seconds=120, action_ttl_seconds=10,
    )
    gateway = EnterpriseSessionDecisionPrototype(identity, authority, key_id="approval", signing_key=key)
    return identity, user, other, session, authority, clock, context, scope, request, action, gateway


def test_valid_user_session_commits_exact_action_and_replays_current_result(governed):
    _, _, _, session, authority, clock, context, scope, request, action, gateway = governed
    outcome = gateway.decide(session.token, context, action.action_id)
    assert outcome.choice == "approve" and not outcome.replayed
    assert authority.evaluate(context, [scope], operation_id="operation")
    assert session.token not in repr(outcome)
    assert session.token.encode() not in authority.path.read_bytes()
    clock[0] += 11
    replay = gateway.decide(session.token, context, action.action_id)
    assert replay.replayed and replay.lease.lease_id == outcome.lease.lease_id
    assert authority.decision_count(request.request_id) == 1
    clock[0] += 120
    expired = gateway.decide(session.token, context, action.action_id)
    assert expired.replayed and expired.lease.status == "expired"


def test_revoked_session_and_plain_answer_cannot_approve(governed):
    identity, _, _, session, authority, _, context, scope, request, action, gateway = governed
    assert identity.revoke_session(session.token)
    for token in (session.token, "yes", "", "hs_sess_invalid"):
        with pytest.raises(SessionDecisionDenied):
            gateway.decide(token, context, action.action_id)
    assert not authority.evaluate(context, [scope], operation_id="operation")
    assert authority.decision_count(request.request_id) == 0


def test_other_user_and_api_token_cannot_approve(governed):
    identity, user, other, _, authority, _, context, scope, request, action, gateway = governed
    other_session = identity.create_session(user_id=other.id, ttl_seconds=60)
    api_token = identity.create_api_token(user_id=user.id, scopes=["tasks:write"])
    for token in (other_session.token, api_token.token):
        with pytest.raises(SessionDecisionDenied):
            gateway.decide(token, context, action.action_id)
    assert not authority.evaluate(context, [scope], operation_id="operation")
    assert authority.decision_count(request.request_id) == 0


def test_deactivated_user_cannot_approve(governed):
    identity, user, _, session, authority, _, context, scope, request, action, gateway = governed
    identity.deactivate_user(user_id=user.id)
    with pytest.raises(SessionDecisionDenied):
        gateway.decide(session.token, context, action.action_id)
    assert not authority.evaluate(context, [scope], operation_id="operation")
    assert authority.decision_count(request.request_id) == 0


def test_outdated_action_cannot_approve_the_new_revision(governed, tmp_path):
    _, _, _, session, authority, _, context, scope, request, action, gateway = governed
    narrower = normalize_scope("file", str(tmp_path), ["read"], cwd=tmp_path)
    authority.narrow_request(context, request.request_id, request.revision, [narrower])
    with pytest.raises(RevisionConflict):
        gateway.decide(session.token, context, action.action_id)
    assert not authority.evaluate(context, [scope], operation_id="operation")


def test_deny_action_has_no_lease_and_replays_as_denied(governed):
    _, _, _, session, authority, _, context, scope, request, _, gateway = governed
    action = authority.issue_decision_action(
        context, request.request_id, request.revision, approved_scopes=[scope],
        choice="deny", lifetime="once", duration_seconds=120,
    )
    outcome = gateway.decide(session.token, context, action.action_id)
    assert outcome.choice == "deny" and outcome.lease is None
    assert gateway.decide(session.token, context, action.action_id).replayed
    assert authority.decision_count(request.request_id) == 1
    assert not authority.evaluate(context, [scope], operation_id="operation")


def test_session_expiring_before_ledger_commit_cannot_grant(governed, monkeypatch):
    _, _, _, session, authority, clock, context, scope, request, _, gateway = governed
    action = authority.issue_decision_action(
        context, request.request_id, request.revision, approved_scopes=[scope],
        choice="approve", lifetime="once", duration_seconds=120, action_ttl_seconds=900,
    )
    assert datetime.fromisoformat(session.expires_at).timestamp() < action.expires_at
    decide = authority.decide

    def expire_before_commit(*args, **kwargs):
        clock[0] = datetime.fromisoformat(session.expires_at).timestamp()
        return decide(*args, **kwargs)

    monkeypatch.setattr(authority, "decide", expire_before_commit)
    with pytest.raises(InvalidDecision):
        gateway.decide(session.token, context, action.action_id)
    assert authority.decision_count(request.request_id) == 0
    assert not authority.evaluate(context, [scope], operation_id="operation")


def test_session_revocation_waits_for_inflight_decision(governed, monkeypatch):
    identity, _, _, session, authority, _, context, scope, request, action, gateway = governed
    guard_acquired, release, revoke_started, revoke_entered = Event(), Event(), Event(), Event()

    class GuardedIdentity:
        @contextmanager
        def hold_active_session(self, token):
            with identity.hold_active_session(token) as user:
                guard_acquired.set()
                assert release.wait(3)
                yield user

    gateway._identity = GuardedIdentity()

    def revoke():
        revoke_started.set()
        return identity.revoke_session(session.token)

    with ThreadPoolExecutor(max_workers=2) as pool:
        decision = pool.submit(gateway.decide, session.token, context, action.action_id)
        assert guard_acquired.wait(3)
        connect = identity.store.connect

        def observed_connect():
            connection = connect()
            connection.set_trace_callback(
                lambda statement: revoke_entered.set() if statement.startswith("BEGIN") else None
            )
            return connection

        monkeypatch.setattr(identity.store, "connect", observed_connect)
        revocation = pool.submit(revoke)
        assert revoke_started.wait(3)
        assert revoke_entered.wait(3)
        assert not revocation.done()
        release.set()
        assert decision.result(timeout=3).lease is not None
        assert revocation.result(timeout=3) is True
    assert authority.decision_count(request.request_id) == 1
    with pytest.raises(SessionDecisionDenied):
        gateway.decide(session.token, context, action.action_id)
    assert authority.evaluate(context, [scope], operation_id="operation")


def test_identity_store_error_does_not_create_an_approval(governed, monkeypatch):
    identity, _, _, session, authority, _, context, scope, request, action, gateway = governed

    def unavailable():
        raise sqlite3.OperationalError("identity store unavailable")

    monkeypatch.setattr(identity.store, "connect", unavailable)
    with pytest.raises(sqlite3.OperationalError):
        gateway.decide(session.token, context, action.action_id)
    assert authority.decision_count(request.request_id) == 0
    assert not authority.evaluate(context, [scope], operation_id="operation")


def test_wrong_owner_context_is_not_a_valid_session_authority(governed):
    _, _, _, session, authority, _, context, scope, request, action, gateway = governed
    with pytest.raises(SessionDecisionDenied):
        gateway.decide(session.token, replace(context, owner_id="enterprise:other"), action.action_id)
    assert authority.decision_count(request.request_id) == 0
    assert not authority.evaluate(context, [scope], operation_id="operation")


def test_replay_rejects_a_session_expired_by_the_ledger_clock(governed):
    _, _, _, session, authority, clock, context, _, request, action, gateway = governed
    outcome = gateway.decide(session.token, context, action.action_id)
    assert outcome.lease is not None
    clock[0] = datetime.fromisoformat(session.expires_at).timestamp()
    with pytest.raises(PermissionDenied, match="session expired"):
        gateway.decide(session.token, context, action.action_id)
    assert authority.decision_count(request.request_id) == 1


def test_expired_identity_session_is_rejected_before_decision(governed):
    identity, _, _, session, authority, _, context, _, request, action, gateway = governed
    with identity.store.connect() as connection:
        connection.execute(
            "UPDATE sessions SET expires_at=? WHERE id=?", ("2000-01-01T00:00:00+00:00", session.id),
        )
    with pytest.raises(SessionDecisionDenied):
        gateway.decide(session.token, context, action.action_id)
    assert authority.decision_count(request.request_id) == 0


def test_unpinned_signer_cannot_create_a_decision(governed):
    identity, _, _, session, authority, _, context, scope, request, action, _ = governed
    attacker = EnterpriseSessionDecisionPrototype(
        identity, authority, key_id="approval", signing_key=Ed25519PrivateKey.generate(),
    )
    with pytest.raises(InvalidDecision):
        attacker.decide(session.token, context, action.action_id)
    assert authority.decision_count(request.request_id) == 0
    assert not authority.evaluate(context, [scope], operation_id="operation")


@pytest.mark.asyncio
async def test_governed_session_resumes_the_same_managed_registry_operation(governed, tmp_path):
    import asyncio

    from orchestrator.permissions.registry import ManagedToolRegistry
    from tests.test_permission_execution_gate import TestFileSupervisor, find_pending
    from tools.registry import ToolResult

    _, _, _, session, authority, _, base, _, _, _, gateway = governed
    context = replace(base, task_id="integration-task", run_id="integration-run",
                      execution_epoch="integration-epoch")
    authority.open_task(context)
    ceiling = normalize_scope("file", str(tmp_path), ["create", "write"], cwd=tmp_path)
    supervisor = TestFileSupervisor(authority, tmp_path)
    registry = ManagedToolRegistry(
        allowed_tools=["file_write"], access_root=tmp_path, workspace_dir=tmp_path, secrets={},
        audit_context={"agent_name": "agent", "workspace_dir": str(tmp_path), "safety_mode": "read_write"},
        permission_authority=authority, permission_context=context,
        permission_supervisor=supervisor, requestable_scopes=(ceiling,),
    )
    task = asyncio.create_task(registry.execute(
        "file_write", {"path": "approved.txt", "content": "original"}, "call-1",
    ))
    request = await find_pending(authority, context)
    assert not (tmp_path / "approved.txt").exists()
    action = authority.issue_decision_action(
        context, request.request_id, request.revision, approved_scopes=request.scopes,
        choice="approve", lifetime="once", duration_seconds=120,
    )
    outcome = await asyncio.to_thread(gateway.decide, session.token, context, action.action_id)
    assert outcome.lease is not None
    registry._permission_waiter.notify()
    result = await asyncio.wait_for(task, 3)
    assert isinstance(result, ToolResult) and not result.is_error
    assert (tmp_path / "approved.txt").read_text(encoding="utf-8") == "original"
    assert authority.operation_state(request.operation_id) == "succeeded"


def test_lost_response_after_commit_replays_without_a_second_lease(governed):
    identity, _, _, session, authority, _, context, _, request, action, gateway = governed

    class LostResponse:
        @contextmanager
        def hold_active_session(self, token):
            with identity.hold_active_session(token) as user:
                yield user
                raise OSError("injected response loss")

    gateway._identity = LostResponse()
    with pytest.raises(OSError, match="response loss"):
        gateway.decide(session.token, context, action.action_id)
    assert authority.decision_count(request.request_id) == 1
    gateway._identity = identity
    replay = gateway.decide(session.token, context, action.action_id)
    assert replay.replayed and replay.lease is not None
    assert authority.decision_count(request.request_id) == 1
