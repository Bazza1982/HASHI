"""The final application-level dispatch barrier serializes authority changes."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from orchestrator.permissions.authority import OperationConflict, PermissionAuthority, PermissionDenied
from orchestrator.permissions.models import ExecutionContext, normalize_scope
from tests.test_permissions_authority import AuthenticatedReceiptSource, Clock, approve


@pytest.fixture
def environment(tmp_path):
    clock = Clock()
    issuer = AuthenticatedReceiptSource()
    context = ExecutionContext(
        instance_id="instance", owner_id="owner", agent_id="agent", task_id="task",
        run_id="run", session_id="session", turn_id="turn", worker_generation="worker",
        context_generation="generation", execution_epoch="epoch", backend_id="herv3",
        executor_id="guarded-test", policy_revision=1,
    )
    authority = PermissionAuthority(tmp_path / "ledger.sqlite", verifier=issuer, clock=clock, monotonic=clock)
    authority.open_task(context)
    project = tmp_path / "project"
    project.mkdir()
    scope = normalize_scope("file", str(project), ["create", "read", "write"], cwd=tmp_path)
    return authority, issuer, clock, context, scope


def prepared(environment, *, lifetime="once"):
    authority, issuer, clock, context, scope = environment
    _, _, lease = approve(
        authority, issuer, clock, context, [scope], lifetime=lifetime,
        operation_id="operation", arguments_digest="a" * 64,
    )
    claim = authority.prepare(context, "operation", "a" * 64, [scope])
    return authority, issuer, clock, context, scope, lease, claim


def dispatch(environment, claim, effect, *, arguments_digest="a" * 64):
    authority, _, _, context, scope = environment
    return authority.dispatch_guarded(
        context, claim.claim_id, operation_id="operation", arguments_digest=arguments_digest,
        scopes=[scope], effect=effect,
    )


@pytest.mark.parametrize("change", ["revoke", "pause", "fence", "end", "expire"])
def test_no_file_effect_after_authority_changes_before_dispatch(environment, tmp_path, monkeypatch, change):
    authority, _, clock, context, _, lease, claim = prepared(environment)
    original_start = authority.start
    target = tmp_path / "project" / "must-not-exist.txt"

    def start_then_change(ctx, claim_id):
        result = original_start(ctx, claim_id)
        if change == "revoke":
            authority.revoke(ctx, lease.lease_id)
        elif change == "pause":
            authority.pause_task(ctx)
        elif change == "fence":
            authority.fence_execution(ctx)
        elif change == "end":
            authority.end_task(ctx, outcome="cancelled")
        else:
            clock.value = lease.expires_at
        return result

    monkeypatch.setattr(authority, "start", start_then_change)
    with pytest.raises(PermissionDenied):
        dispatch(environment, claim, lambda: target.write_text("unsafe", encoding="utf-8"))
    assert not target.exists()
    status = authority.operation_status_for_task(context, "operation")
    assert status.state == "started"
    assert status.resource_state in {"running", "stopping"}


@pytest.mark.parametrize("lifetime", ["once", "task"])
def test_revocation_waits_for_an_already_started_file_effect(environment, tmp_path, lifetime):
    authority, _, _, context, _, lease, claim = prepared(environment, lifetime=lifetime)
    entered, release, revoking, revoked = Event(), Event(), Event(), Event()
    target = tmp_path / "project" / "once.txt"
    order = []

    def effect():
        entered.set()
        assert release.wait(5)
        target.write_text("written once", encoding="utf-8")
        order.append("effect")
        return "saved"

    def revoke():
        revoking.set()
        authority.revoke(context, lease.lease_id)
        order.append("revoked")
        revoked.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        running = pool.submit(dispatch, environment, claim, effect)
        try:
            assert entered.wait(2)
            removal = pool.submit(revoke)
            assert revoking.wait(2)
            assert not revoked.wait(0.2)
        finally:
            release.set()
        assert running.result(timeout=5) == "saved"
        removal.result(timeout=5)
    assert order == ["effect", "revoked"]
    assert target.read_text(encoding="utf-8") == "written once"
    assert authority.resource_state(claim.claim_id) == "stopping"
    with pytest.raises((PermissionDenied, OperationConflict)):
        dispatch(environment, claim, lambda: target.write_text("again", encoding="utf-8"))
    assert target.read_text(encoding="utf-8") == "written once"


def test_guarded_claim_can_never_dispatch_twice(environment, tmp_path):
    authority, _, _, context, _, _, claim = prepared(environment)
    target = tmp_path / "project" / "once.txt"
    assert dispatch(environment, claim, lambda: target.write_text("first", encoding="utf-8")) == 5
    with pytest.raises(OperationConflict):
        dispatch(environment, claim, lambda: target.write_text("again", encoding="utf-8"))
    assert target.read_text(encoding="utf-8") == "first"


def test_mismatched_arguments_do_not_consume_a_prepared_claim(environment, tmp_path):
    authority, _, _, context, _, lease, claim = prepared(environment)
    target = tmp_path / "project" / "matched.txt"
    with pytest.raises(OperationConflict):
        dispatch(environment, claim, lambda: target.write_text("wrong", encoding="utf-8"),
                 arguments_digest="b" * 64)
    assert not target.exists()
    assert authority.operation_status_for_task(context, "operation").state == "prepared"
    assert authority.get_lease(lease.lease_id).status == "reserved"
    assert dispatch(environment, claim, lambda: target.write_text("right", encoding="utf-8")) == 5
    assert target.read_text(encoding="utf-8") == "right"


def test_crash_after_file_effect_remains_unknown_on_restart(environment, tmp_path):
    authority, issuer, clock, context, _, _, claim = prepared(environment)
    target = tmp_path / "project" / "uncertain.txt"

    def effect():
        target.write_text("already written", encoding="utf-8")
        raise OSError("result was lost after the effect")

    with pytest.raises(OSError):
        dispatch(environment, claim, effect)
    recovered = PermissionAuthority(authority.path, verifier=issuer, clock=clock, monotonic=clock)
    assert recovered.operation_status_for_task(context, "operation").state == "unknown"
    assert target.read_text(encoding="utf-8") == "already written"
    with pytest.raises(PermissionDenied):
        dispatch(environment, claim, lambda: target.write_text("again", encoding="utf-8"))


def test_mismatched_scope_does_not_consume_a_prepared_claim(environment, tmp_path):
    authority, _, _, context, _, lease, claim = prepared(environment)
    target = tmp_path / "project" / "scope.txt"
    wrong_scope = normalize_scope("file", str(tmp_path), ["read"], cwd=tmp_path)
    with pytest.raises(OperationConflict):
        authority.dispatch_guarded(
            context, claim.claim_id, operation_id="operation", arguments_digest="a" * 64,
            scopes=[wrong_scope], effect=lambda: target.write_text("wrong", encoding="utf-8"),
        )
    assert not target.exists()
    assert authority.operation_status_for_task(context, "operation").state == "prepared"
    assert authority.get_lease(lease.lease_id).status == "reserved"


def test_async_effect_is_rejected_before_start(environment, tmp_path):
    authority, _, _, context, _, lease, claim = prepared(environment)
    target = tmp_path / "project" / "async.txt"

    async def effect():
        target.write_text("should never run", encoding="utf-8")

    with pytest.raises(ValueError):
        dispatch(environment, claim, effect)
    assert not target.exists()
    assert authority.operation_status_for_task(context, "operation").state == "prepared"
    assert authority.get_lease(lease.lease_id).status == "reserved"


def test_deferred_effect_cannot_escape_the_guard(environment, tmp_path):
    authority, _, _, context, _, _, claim = prepared(environment)
    target = tmp_path / "project" / "deferred.txt"

    async def later():
        target.write_text("should never run", encoding="utf-8")

    with pytest.raises(TypeError):
        dispatch(environment, claim, lambda: later())
    assert not target.exists()
    assert authority.operation_status_for_task(context, "operation").state == "started"
    with pytest.raises(OperationConflict):
        dispatch(environment, claim, lambda: target.write_text("again", encoding="utf-8"))


def test_separate_leases_can_cover_the_actions_of_one_operation(environment, tmp_path):
    authority, issuer, clock, context, scope = environment
    root = tmp_path / "project"
    read = normalize_scope("file", str(root), ["read"], cwd=tmp_path)
    write = normalize_scope("file", str(root), ["create", "write"], cwd=tmp_path)
    approve(authority, issuer, clock, context, [read], lifetime="task")
    approve(authority, issuer, clock, context, [write], lifetime="task")
    claim = authority.prepare(context, "operation", "a" * 64, [scope])
    target = root / "combined.txt"
    assert dispatch(environment, claim, lambda: target.write_text("combined", encoding="utf-8")) == 8
    assert target.read_text(encoding="utf-8") == "combined"
