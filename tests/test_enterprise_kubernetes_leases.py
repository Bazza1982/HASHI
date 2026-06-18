from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from orchestrator.enterprise.kubernetes_leases import (
    KubernetesLease,
    KubernetesLeaseConflict,
    KubernetesLeaseCoordinator,
)


def test_kubernetes_lease_acquire_blocks_until_expiry():
    client = _FakeKubernetesLeaseClient()
    coordinator = KubernetesLeaseCoordinator(client, namespace="hashi-enterprise")
    now = datetime(2026, 6, 18, 12, 0, tzinfo=timezone.utc)

    first = coordinator.acquire("scheduler", holder_identity="pod-a", ttl_seconds=30, now=now)
    blocked = coordinator.acquire("scheduler", holder_identity="pod-b", ttl_seconds=30, now=now + timedelta(seconds=5))
    stolen = coordinator.acquire("scheduler", holder_identity="pod-b", ttl_seconds=30, now=now + timedelta(seconds=31))

    assert first.acquired is True
    assert first.lease.holder_identity == "pod-a"
    assert blocked.acquired is False
    assert blocked.current_holder_id == "pod-a"
    assert stolen.acquired is True
    assert stolen.lease.holder_identity == "pod-b"


def test_kubernetes_lease_holder_can_renew_and_release():
    client = _FakeKubernetesLeaseClient()
    coordinator = KubernetesLeaseCoordinator(client, namespace="hashi-enterprise")
    now = datetime(2026, 6, 18, 12, 0, tzinfo=timezone.utc)
    acquired = coordinator.acquire("scheduler", holder_identity="pod-a", ttl_seconds=30, now=now)

    renewed = coordinator.renew("scheduler", holder_identity="pod-a", ttl_seconds=60, now=now + timedelta(seconds=10))
    wrong_release = coordinator.release("scheduler", holder_identity="pod-b")
    released = coordinator.release("scheduler", holder_identity="pod-a")

    assert acquired.acquired is True
    assert renewed.acquired is True
    assert renewed.lease.acquire_time == acquired.lease.acquire_time
    assert renewed.lease.lease_duration_seconds == 60
    assert wrong_release is False
    assert released is True
    assert client.get_lease("hashi-enterprise", "scheduler") is None


def test_kubernetes_lease_renew_requires_current_unexpired_holder():
    client = _FakeKubernetesLeaseClient()
    coordinator = KubernetesLeaseCoordinator(client, namespace="hashi-enterprise")
    now = datetime(2026, 6, 18, 12, 0, tzinfo=timezone.utc)
    coordinator.acquire("scheduler", holder_identity="pod-a", ttl_seconds=30, now=now)

    wrong_holder = coordinator.renew("scheduler", holder_identity="pod-b", now=now + timedelta(seconds=1))
    expired = coordinator.renew("scheduler", holder_identity="pod-a", now=now + timedelta(seconds=31))

    assert wrong_holder.acquired is False
    assert wrong_holder.current_holder_id == "pod-a"
    assert expired.acquired is False
    assert expired.current_holder_id == "pod-a"


def test_kubernetes_lease_conflict_returns_latest_holder():
    client = _FakeKubernetesLeaseClient(conflict_on_replace=True)
    coordinator = KubernetesLeaseCoordinator(client, namespace="hashi-enterprise")
    now = datetime(2026, 6, 18, 12, 0, tzinfo=timezone.utc)
    coordinator.acquire("scheduler", holder_identity="pod-a", ttl_seconds=1, now=now)
    client.conflict_replacement = KubernetesLease(
        namespace="hashi-enterprise",
        name="scheduler",
        holder_identity="pod-c",
        lease_duration_seconds=30,
        acquire_time="2026-06-18T12:00:02Z",
        renew_time="2026-06-18T12:00:02Z",
        resource_version="rv-conflict",
    )

    attempt = coordinator.acquire("scheduler", holder_identity="pod-b", ttl_seconds=30, now=now + timedelta(seconds=2))

    assert attempt.acquired is False
    assert attempt.conflict is True
    assert attempt.current_holder_id == "pod-c"


def test_kubernetes_lease_rejects_empty_inputs():
    client = _FakeKubernetesLeaseClient()

    with pytest.raises(ValueError, match="namespace is required"):
        KubernetesLeaseCoordinator(client, namespace="")
    coordinator = KubernetesLeaseCoordinator(client, namespace="hashi-enterprise")
    with pytest.raises(ValueError, match="lease name is required"):
        coordinator.acquire("", holder_identity="pod-a")
    with pytest.raises(ValueError, match="holder identity is required"):
        coordinator.acquire("scheduler", holder_identity="")


class _FakeKubernetesLeaseClient:
    def __init__(self, *, conflict_on_replace: bool = False):
        self.leases = {}
        self.next_resource_version = 1
        self.conflict_on_replace = conflict_on_replace
        self.conflict_replacement = None

    def get_lease(self, namespace: str, name: str):
        return self.leases.get((namespace, name))

    def create_lease(self, lease: KubernetesLease):
        key = (lease.namespace, lease.name)
        if key in self.leases:
            raise KubernetesLeaseConflict()
        written = self._with_next_resource_version(lease)
        self.leases[key] = written
        return written

    def replace_lease(self, lease: KubernetesLease):
        key = (lease.namespace, lease.name)
        current = self.leases.get(key)
        if current is None or current.resource_version != lease.resource_version:
            raise KubernetesLeaseConflict()
        if self.conflict_on_replace:
            if self.conflict_replacement is not None:
                self.leases[key] = self.conflict_replacement
            self.conflict_on_replace = False
            raise KubernetesLeaseConflict()
        written = self._with_next_resource_version(lease)
        self.leases[key] = written
        return written

    def delete_lease(self, namespace: str, name: str, *, holder_identity: str):
        key = (namespace, name)
        current = self.leases.get(key)
        if current is None or current.holder_identity != holder_identity:
            return False
        del self.leases[key]
        return True

    def _with_next_resource_version(self, lease: KubernetesLease):
        resource_version = f"rv-{self.next_resource_version}"
        self.next_resource_version += 1
        return replace(lease, resource_version=resource_version)
