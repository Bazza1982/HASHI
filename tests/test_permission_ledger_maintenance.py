"""Trusted ledger reconciliation preserves progress and never restores authority."""
from contextlib import closing
from dataclasses import replace
import sqlite3

import pytest

from orchestrator.permissions.authority import PermissionAuthority, PermissionDenied, RevisionConflict
from tests.test_permissions_authority import approve
from tests.test_permissions_service_recovery import service as service


def copy_database(source, destination):
    with closing(sqlite3.connect(source)) as src, closing(sqlite3.connect(destination)) as dst:
        src.backup(dst)


def reconcile(path, revision, *, at=1000.0):
    return PermissionAuthority.reconcile_ledger(
        path, expected_revision=revision, recovery_id="a" * 32,
        reason="controlled backup recovery", observed_at=at, instance_id="instance",
    )


def test_old_backup_recovery_preserves_uncertain_effect_and_requires_cleanup(service, tmp_path):
    path, old, issuer, clock, context, scope = service
    _, _, lease = approve(old, issuer, clock, context, [scope])
    claim = old.prepare(context, "effect", "b" * 64, [scope])
    old.start(context, claim.claim_id)
    sink = tmp_path / "effect.txt"
    sink.write_text("one effect", encoding="utf-8")
    backup = tmp_path / "backup.sqlite"
    copy_database(path, backup)
    old.revoke(context, lease.lease_id)
    copy_database(backup, path)
    with pytest.raises(PermissionDenied):
        old.evaluate(context, [scope])

    observation = PermissionAuthority.inspect_ledger(path)
    assert not observation["continuity_matches"]
    result = reconcile(path, observation["revision"])
    assert result["revoked_leases"] == result["paused_tasks"] == 1
    recovered = PermissionAuthority(path, verifier=issuer, clock=clock, monotonic=clock)
    later = replace(context, execution_epoch="new", worker_generation="new")
    recovered.bind_execution(later)
    with pytest.raises(PermissionDenied, match="reconciliation"):
        recovered.resume_task(later)
    assert recovered.operation_status_for_task(later, "effect").state == "unknown"
    assert recovered.resource_state(claim.claim_id) == "stopping"
    assert recovered.get_lease(lease.lease_id).status == "revoked"
    assert sink.read_text(encoding="utf-8") == "one effect"
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("SELECT task_id,session_id,state FROM permission_tasks").fetchone() == (
            context.task_id, context.session_id, "paused",
        )
        assert connection.execute("SELECT count(*) FROM approval_decisions").fetchone() == (1,)


def test_recovery_replay_does_not_revoke_new_permission_or_accept_rolled_back_receipt(service, tmp_path):
    path, old, issuer, clock, context, scope = service
    approve(old, issuer, clock, context, [scope])
    revision = PermissionAuthority.inspect_ledger(path)["revision"]
    first = reconcile(path, revision)
    new = PermissionAuthority(path, verifier=issuer, clock=clock, monotonic=clock)
    later = replace(context, execution_epoch="new", worker_generation="new")
    new.bind_execution(later)
    new.resume_task(later)
    assert not new.evaluate(later, [scope])
    _, _, lease = approve(new, issuer, clock, later, [scope])
    before = PermissionAuthority.inspect_ledger(path)
    assert reconcile(path, revision) == first
    assert PermissionAuthority.inspect_ledger(path) == before
    assert new.evaluate(later, [scope])
    backup = tmp_path / "receipt.sqlite"
    copy_database(path, backup)
    new.revoke(later, lease.lease_id)
    copy_database(backup, path)
    with pytest.raises(PermissionDenied, match="continuity"):
        reconcile(path, revision)


@pytest.mark.parametrize("fault", ["stale", "clock", "witness"])
def test_failed_maintenance_is_atomic_and_does_not_displace_live_authority(service, fault):
    path, authority, issuer, clock, context, scope = service
    approve(authority, issuer, clock, context, [scope])
    revision = PermissionAuthority.inspect_ledger(path)["revision"]
    if fault == "stale":
        assert authority.evaluate(context, [scope])
    if fault == "witness":
        with closing(sqlite3.connect(authority.continuity_path)) as connection:
            connection.execute("CREATE TRIGGER fail_update BEFORE UPDATE ON ledger_head "
                               "BEGIN SELECT RAISE(ABORT,'maintenance witness failure'); END")
    before = path.read_bytes(), authority.continuity_path.read_bytes()
    expected = RevisionConflict if fault == "stale" else (
        PermissionDenied if fault == "clock" else sqlite3.DatabaseError
    )
    with pytest.raises(expected):
        reconcile(path, revision, at=999.0 if fault == "clock" else 1000.0)
    assert (path.read_bytes(), authority.continuity_path.read_bytes()) == before
    if fault == "witness":
        with closing(sqlite3.connect(authority.continuity_path)) as connection:
            connection.execute("DROP TRIGGER fail_update")
    assert authority.evaluate(context, [scope])


def test_inspection_never_creates_a_missing_witness(service):
    path, authority, *_ = service
    authority.continuity_path.unlink()
    before = path.read_bytes()
    with pytest.raises(PermissionDenied):
        PermissionAuthority.inspect_ledger(path)
    assert path.read_bytes() == before
    assert not authority.continuity_path.exists()

def test_foreign_installation_cannot_be_reconciled(service):
    path, authority, issuer, clock, context, scope = service
    approve(authority, issuer, clock, context, [scope])
    before = path.read_bytes(), authority.continuity_path.read_bytes()
    with pytest.raises(PermissionDenied, match="another installation"):
        PermissionAuthority.reconcile_ledger(
            path, expected_revision=PermissionAuthority.inspect_ledger(path)["revision"],
            recovery_id="c" * 32, reason="wrong installation", observed_at=1000,
            instance_id="another-instance",
        )
    assert (path.read_bytes(), authority.continuity_path.read_bytes()) == before
    assert authority.evaluate(context, [scope])


@pytest.mark.platform
def test_native_archive_preserves_both_databases_and_detects_changed_evidence(service):
    import os
    if os.name != "nt":
        pytest.skip("native Windows maintenance private ACL and kernel lock")
    from orchestrator.permissions.ledger_maintenance import _archive
    from orchestrator.permissions.windows_ipc import current_process_user_sid
    from orchestrator.permissions.windows_security import InstallationLock
    from scripts.permissions.windows_probe_staging import WindowsStageACL

    path, authority, issuer, clock, context, scope = service
    approve(authority, issuer, clock, context, [scope])
    sid = current_process_user_sid()
    WindowsStageACL().apply(path.parent, sid, writable=True)
    observation = PermissionAuthority.inspect_ledger(path)
    ownership = InstallationLock(path.parent)
    try:
        with pytest.raises(PermissionError):
            InstallationLock(path.parent)
        kwargs = dict(recovery_id="d" * 32, expected_revision=observation["revision"],
                      reason="archive qualification", sid=sid)
        archive = _archive(path.parent, path, **kwargs)
        with closing(sqlite3.connect(archive / "ledger.sqlite")) as saved:
            assert saved.execute("SELECT status FROM permission_leases").fetchone() == ("active",)
        with closing(sqlite3.connect(archive / "witness.sqlite")) as saved:
            assert len(saved.execute("SELECT token FROM ledger_head").fetchone()[0]) == 32
        assert _archive(path.parent, path, **kwargs) == archive
        with (archive / "ledger.sqlite").open("ab") as stream:
            stream.write(b"changed archive")
        with pytest.raises(PermissionDenied, match="archive changed"):
            _archive(path.parent, path, **kwargs)
    finally:
        ownership.close()
    assert authority.evaluate(context, [scope])

# HASHI-Fingerprint: hfp_cef62eae6d50427b902beb3a7d7ecad9
