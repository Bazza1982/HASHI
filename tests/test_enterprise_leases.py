from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from orchestrator.enterprise import EnterpriseLeaseStore, IdentityService, PostgresEnterpriseLeaseStore


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


def test_enterprise_lease_store_from_url_dispatches_sqlite(tmp_path):
    db_path = tmp_path / "enterprise.sqlite"
    IdentityService.from_path(db_path).create_organization(org_id="ORG-001", name="Acme")

    leases = EnterpriseLeaseStore.from_url(f"sqlite:///{db_path}", org_id="ORG-001")

    assert isinstance(leases, EnterpriseLeaseStore)
    assert leases.store.db_path == db_path


def test_postgres_enterprise_lease_store_acquire_blocks_and_expires():
    state = {}
    leases = PostgresEnterpriseLeaseStore(
        "postgresql://hashi@example.invalid/hashi",
        org_id="ORG-001",
        connect=_fake_pg_connect(state),
    )
    now = datetime(2026, 6, 17, 10, 0, tzinfo=timezone.utc)

    first = leases.acquire("scheduler", holder_id="pod-a", ttl_seconds=30, now=now)
    blocked = leases.acquire("scheduler", holder_id="pod-b", ttl_seconds=30, now=now + timedelta(seconds=5))
    stolen = leases.acquire("scheduler", holder_id="pod-b", ttl_seconds=30, now=now + timedelta(seconds=31))

    assert first.acquired is True
    assert first.lease.holder_id == "pod-a"
    assert blocked.acquired is False
    assert blocked.current_holder_id == "pod-a"
    assert stolen.acquired is True
    assert stolen.lease.holder_id == "pod-b"


def test_postgres_enterprise_lease_store_renews_and_releases_current_holder():
    state = {}
    leases = PostgresEnterpriseLeaseStore(
        "postgresql://hashi@example.invalid/hashi",
        org_id="ORG-001",
        connect=_fake_pg_connect(state),
    )
    now = datetime(2026, 6, 17, 10, 0, tzinfo=timezone.utc)

    acquired = leases.acquire("audit-export", holder_id="pod-a", ttl_seconds=10, now=now)
    renewed = leases.renew("audit-export", holder_id="pod-a", ttl_seconds=20, now=now + timedelta(seconds=5))
    wrong_release = leases.release("audit-export", holder_id="pod-b")
    released = leases.release("audit-export", holder_id="pod-a")

    assert acquired.acquired is True
    assert renewed.acquired is True
    assert renewed.lease.acquired_at == acquired.lease.acquired_at
    assert wrong_release is False
    assert released is True
    assert leases.get("audit-export") is None


def _fake_pg_connect(state: dict):
    def connect(_dsn: str):
        return _FakePgConnection(state)

    return connect


class _FakePgConnection:
    def __init__(self, state: dict):
        self.state = state

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def cursor(self):
        return _FakePgCursor(self.state)


class _FakePgCursor:
    def __init__(self, state: dict):
        self.state = state
        self._result = None
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql: str, params: tuple = ()):
        normalized = " ".join(sql.split())
        self.rowcount = 0
        if normalized.startswith("CREATE TABLE") or normalized.startswith("CREATE INDEX"):
            return
        if normalized.startswith("SELECT pg_advisory_xact_lock"):
            return
        if normalized.startswith("SELECT org_id, name, holder_id"):
            self._result = self.state.get((params[0], params[1]))
            return
        if normalized.startswith("INSERT INTO enterprise_leases"):
            org_id, name, holder_id, expires_at, acquired_at, renewed_at, metadata_json = params
            self.state[(org_id, name)] = (
                org_id,
                name,
                holder_id,
                expires_at,
                acquired_at,
                renewed_at,
                metadata_json,
            )
            self.rowcount = 1
            return
        if normalized.startswith("UPDATE enterprise_leases SET holder_id"):
            holder_id, expires_at, acquired_at, renewed_at, metadata_json, org_id, name = params
            self.state[(org_id, name)] = (
                org_id,
                name,
                holder_id,
                expires_at,
                acquired_at,
                renewed_at,
                metadata_json,
            )
            self.rowcount = 1
            return
        if normalized.startswith("UPDATE enterprise_leases SET expires_at"):
            expires_at, renewed_at, org_id, name, holder_id = params
            current = self.state.get((org_id, name))
            if current and current[2] == holder_id:
                self.state[(org_id, name)] = (
                    current[0],
                    current[1],
                    current[2],
                    expires_at,
                    current[4],
                    renewed_at,
                    current[6],
                )
                self.rowcount = 1
            return
        if normalized.startswith("DELETE FROM enterprise_leases"):
            org_id, name, holder_id = params
            current = self.state.get((org_id, name))
            if current and current[2] == holder_id:
                del self.state[(org_id, name)]
                self.rowcount = 1
            return
        raise AssertionError(f"unexpected SQL: {normalized}")

    def fetchone(self):
        return self._result
