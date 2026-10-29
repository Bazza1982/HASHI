"""Formal frontend intentions bind a current human session and canonical Run."""
from dataclasses import replace
import sqlite3
import time

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import pytest

from orchestrator.enterprise import IdentityService
from orchestrator.permissions.authority import InvalidDecision, PermissionAuthority, PermissionDenied, RevisionConflict
from orchestrator.permissions.decision_commands import PermissionDecisionController, parse_decision_intent
from orchestrator.permissions.decision_gate import PaoDecisionAuthority
from orchestrator.permissions.enterprise_decisions import SessionDecisionDenied, SessionDecisionGateway
from orchestrator.permissions.models import PermissionSubject, normalize_scope
from orchestrator.permissions.pao_run_fence import PaoRunFence
from orchestrator.permissions.signed_receipts import SignedReceiptVerifier
from tests.test_permission_pao_fence import setup_run


@pytest.fixture
def control(tmp_path):
    store, session, accepted, fence, context = setup_run(tmp_path)
    identity = IdentityService.from_path(tmp_path / "identity.sqlite")
    identity.create_organization(org_id="org", name="Test")
    owner = identity.create_user(org_id="org", email="owner@example.test",
                                 display_name="Owner", password="test-only-password", user_id="u1")
    other = identity.create_user(org_id="org", email="other@example.test",
                                 display_name="Other", password="test-only-password", user_id="u2")
    user_session = identity.create_session(user_id=owner.id, ttl_seconds=600)
    other_session = identity.create_session(user_id=other.id, ttl_seconds=600)
    key = Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    authority = PermissionAuthority(tmp_path / "permissions.sqlite",
                                    verifier=SignedReceiptVerifier({"approval": public}))
    authority.open_task(context)
    authority.register_runtime_context(context, run_guard=PaoRunFence(
        store, context=context, request_id=accepted.request_id,
        fencing_token=fence, worker_id="INSTANCE:agent",
    ))
    scope = normalize_scope("file", str(tmp_path), ["read", "write"], cwd=tmp_path)
    request = authority.request_access(context, [scope], purpose="Read report",
                                       operation_id="op", arguments_digest="a" * 64)
    port = PaoDecisionAuthority(authority, store)
    gateway = SessionDecisionGateway(identity, port, key_id="approval", signing_key=key,
                                     owner_bindings={owner.id: context.owner_id, other.id: "other"})
    controller = PermissionDecisionController(port, gateway)
    subject = PermissionSubject(context.instance_id, context.owner_id, context.agent_id,
                                 context.session_id, context.context_generation)
    return store, identity, authority, context, scope, request, subject, user_session, other_session, controller


def submit(control, command, *, invocation="invocation", token=None, subject=None):
    _, _, _, _, _, _, owner, session, _, controller = control
    return controller.submit_session_decision(
        subject or owner, command, session_token=session.token if token is None else token,
        invocation_id=invocation, connection_binding="trusted-frontend-connection",
    )


def test_exact_partial_approval_and_retry_do_not_expand_or_duplicate(control):
    _, _, authority, context, scope, request, _, _, _, _ = control
    command = f"approve {request.request_id} once --revision 1 --for 2m --actions read"
    started = time.time()
    outcome = submit(control, command)
    assert outcome.lease.lifetime == "once" and outcome.lease.scopes[0].actions == ("read",)
    assert started + 120 <= outcome.lease.expires_at <= time.time() + 120
    read = replace(scope, actions=("read",))
    assert authority.evaluate(context, [read], operation_id="op")
    assert not authority.evaluate(context, [scope], operation_id="op")
    assert submit(control, command).replayed
    assert authority.decision_count(request.request_id) == 1
    with pytest.raises(RevisionConflict):
        submit(control, command.replace("read", "write"))
    assert authority.decision_count(request.request_id) == 1


@pytest.mark.parametrize("token", ["yes", "", "hs_sess_invalid", "shared-admin-token"])
def test_plain_answers_and_non_session_tokens_never_issue_actions(control, token):
    _, _, authority, _, _, request, _, _, _, _ = control
    with pytest.raises(SessionDecisionDenied):
        submit(control, f"approve {request.request_id} once --revision 1 --for 2m", token=token)
    with sqlite3.connect(authority.path) as connection:
        assert connection.execute("SELECT count(*) FROM decision_actions").fetchone()[0] == 0


def test_other_user_cannot_issue_or_submit_for_owner(control):
    _, _, authority, _, _, request, subject, _, other, _ = control
    for supplied in (subject, replace(subject, owner_id="other")):
        with pytest.raises((SessionDecisionDenied, PermissionDenied)):
            submit(control, f"approve {request.request_id} once --revision 1 --for 2m",
                   token=other.token, subject=supplied)
    assert authority.decision_count(request.request_id) == 0


def test_stopped_run_rejects_late_approval_and_committed_retry_is_read_only(control):
    store, _, authority, context, _, request, _, _, _, _ = control
    command = f"approve {request.request_id} once --revision 1 --for 2m"
    first = submit(control, command)
    store.cancel_run(context.run_id, owner_id=context.owner_id, reason="user stop")
    assert submit(control, command).lease.lease_id == first.lease.lease_id
    with pytest.raises((PermissionDenied, InvalidDecision)):
        submit(control, command, invocation="new-intention")
    assert authority.decision_count(request.request_id) == 1


def test_stop_before_any_approval_and_revoke_session_fail_closed(control):
    store, identity, authority, context, _, request, _, session, _, _ = control
    command = f"approve {request.request_id} once --revision 1 --for 2m"
    store.cancel_run(context.run_id, owner_id=context.owner_id, reason="user stop")
    with pytest.raises(PermissionDenied):
        submit(control, command)
    assert authority.decision_count(request.request_id) == 0
    identity.revoke_session(session.token)
    with pytest.raises(SessionDecisionDenied):
        submit(control, command, invocation="later")
    assert authority.decision_count(request.request_id) == 0


def test_deny_binds_whole_version_and_does_not_grant(control):
    _, _, authority, context, scope, request, _, _, _, _ = control
    outcome = submit(control, f"deny {request.request_id} --revision 1")
    assert outcome.choice == "deny" and outcome.lease is None
    assert submit(control, f"deny {request.request_id} --revision 1").replayed
    assert not authority.evaluate(context, [scope], operation_id="op")


def test_narrow_path_is_explicit_and_cannot_escape_requested_resource(control, tmp_path):
    _, _, authority, context, scope, request, _, _, _, _ = control
    command = (f'approve {request.request_id} task --revision 1 --for 1h '
               f'--actions read --path "{tmp_path / "child.txt"}"')
    outcome = submit(control, command)
    assert outcome.lease.scopes[0].resource == str(tmp_path / "child.txt").lower()
    assert not authority.evaluate(context, [scope], operation_id="op")


@pytest.mark.parametrize("command", [
    "yes", "approve id permanent --revision 1", "approve id once --revision 0",
    "approve id once --revision 1 --revision 2", "approve id once --revision 1 --for 0h",
    "approve id ttl --revision 1", "approve id once --revision 1 --for inf",
    "deny id --revision 1 --actions read", "deny id --revision 1 --for 2h",
    "approve id task --revision 1 --path relative.txt",
    "approve id task --revision 1 --resources 0", "approve id task --revision 1 --unknown x",
])
def test_ambiguous_or_unbounded_intentions_are_rejected(command):
    with pytest.raises(ValueError):
        parse_decision_intent(command)
