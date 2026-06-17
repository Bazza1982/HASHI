from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from orchestrator.enterprise import EnterpriseLeaseStore, IdentityService


def _lease_store(tmp_path) -> EnterpriseLeaseStore:
    db_path = tmp_path / "enterprise.sqlite"
    identity = IdentityService.from_path(db_path)
    identity.create_organization(org_id="ORG-001", name="Acme")
    return EnterpriseLeaseStore.from_path(db_path, org_id="ORG-001")


def test_enterprise_lease_acquire_blocks_other_holder_until_expiry(tmp_path):
    leases = _lease_store(tmp_path)
    now = datetime(2026, 6, 17, 10, 0, tzinfo=timezone.utc)

    first = leases.acquire(
        "audit-export",
        holder_id="pod-a",
        ttl_seconds=30,
        metadata={"component": "audit-export"},
        now=now,
    )
    blocked = leases.acquire("audit-export", holder_id="pod-b", ttl_seconds=30, now=now + timedelta(seconds=5))
    stolen = leases.acquire("audit-export", holder_id="pod-b", ttl_seconds=30, now=now + timedelta(seconds=31))

    assert first.acquired is True
    assert first.lease is not None
    assert first.lease.holder_id == "pod-a"
    assert first.lease.metadata == {"component": "audit-export"}
    assert blocked.acquired is False
    assert blocked.current_holder_id == "pod-a"
    assert stolen.acquired is True
    assert stolen.lease is not None
    assert stolen.lease.holder_id == "pod-b"


def test_enterprise_lease_holder_can_renew_and_release(tmp_path):
    leases = _lease_store(tmp_path)
    now = datetime(2026, 6, 17, 10, 0, tzinfo=timezone.utc)

    acquired = leases.acquire("scheduler", holder_id="pod-a", ttl_seconds=10, now=now)
    renewed = leases.renew("scheduler", holder_id="pod-a", ttl_seconds=20, now=now + timedelta(seconds=5))
    wrong_release = leases.release("scheduler", holder_id="pod-b")
    released = leases.release("scheduler", holder_id="pod-a")

    assert acquired.acquired is True
    assert renewed.acquired is True
    assert renewed.lease is not None
    assert renewed.lease.acquired_at == acquired.lease.acquired_at
    assert renewed.lease.expires_at > acquired.lease.expires_at
    assert wrong_release is False
    assert released is True
    assert leases.get("scheduler") is None


def test_enterprise_lease_renew_requires_current_holder_and_unexpired_lease(tmp_path):
    leases = _lease_store(tmp_path)
    now = datetime(2026, 6, 17, 10, 0, tzinfo=timezone.utc)
    leases.acquire("queue-worker", holder_id="pod-a", ttl_seconds=10, now=now)

    wrong_holder = leases.renew("queue-worker", holder_id="pod-b", ttl_seconds=10, now=now + timedelta(seconds=1))
    expired = leases.renew("queue-worker", holder_id="pod-a", ttl_seconds=10, now=now + timedelta(seconds=11))

    assert wrong_holder.acquired is False
    assert wrong_holder.current_holder_id == "pod-a"
    assert expired.acquired is False
    assert expired.current_holder_id == "pod-a"


def test_enterprise_lease_rejects_empty_names_and_holders(tmp_path):
    leases = _lease_store(tmp_path)

    with pytest.raises(ValueError, match="lease name is required"):
        leases.acquire("", holder_id="pod-a")
    with pytest.raises(ValueError, match="holder id is required"):
        leases.acquire("audit-export", holder_id="")
