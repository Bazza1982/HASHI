from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from threading import Barrier
from typing import Any


@dataclass(frozen=True)
class LeaseRehearsalResult:
    lease_name: str
    holder_a: str
    holder_b: str
    first_acquired_holder: str | None
    second_acquired_holder: str | None
    blocked_holder: str | None
    renew_succeeded: bool
    release_succeeded: bool
    takeover_succeeded: bool

    @property
    def passed(self) -> bool:
        return (
            self.first_acquired_holder in {self.holder_a, self.holder_b}
            and self.second_acquired_holder is None
            and self.blocked_holder in {self.holder_a, self.holder_b}
            and self.blocked_holder != self.first_acquired_holder
            and self.renew_succeeded
            and self.release_succeeded
            and self.takeover_succeeded
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "lease_name": self.lease_name,
            "holder_a": self.holder_a,
            "holder_b": self.holder_b,
            "first_acquired_holder": self.first_acquired_holder,
            "second_acquired_holder": self.second_acquired_holder,
            "blocked_holder": self.blocked_holder,
            "renew_succeeded": self.renew_succeeded,
            "release_succeeded": self.release_succeeded,
            "takeover_succeeded": self.takeover_succeeded,
            "passed": self.passed,
        }


def run_enterprise_lease_rehearsal(
    lease_store,
    *,
    lease_name: str | None = None,
    holder_a: str = "rehearsal-a",
    holder_b: str = "rehearsal-b",
    ttl_seconds: int = 30,
) -> LeaseRehearsalResult:
    lease = lease_name or f"lease-rehearsal-{uuid.uuid4().hex}"
    holders = (holder_a, holder_b)
    start = Barrier(2)

    def acquire(holder: str):
        start.wait()
        return lease_store.acquire(
            lease,
            holder_id=holder,
            ttl_seconds=ttl_seconds,
            metadata={"component": "lease-rehearsal"},
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        attempts = list(pool.map(acquire, holders))

    acquired = [
        holder
        for holder, attempt in zip(holders, attempts, strict=True)
        if attempt.acquired
    ]
    blocked = [
        holder
        for holder, attempt in zip(holders, attempts, strict=True)
        if not attempt.acquired
    ]
    first_holder = acquired[0] if acquired else None
    second_holder = acquired[1] if len(acquired) > 1 else None
    blocked_holder = blocked[0] if blocked else None

    renew_succeeded = False
    release_succeeded = False
    takeover_succeeded = False
    if first_holder is not None:
        renew_succeeded = lease_store.renew(
            lease,
            holder_id=first_holder,
            ttl_seconds=ttl_seconds,
        ).acquired
        release_succeeded = lease_store.release(lease, holder_id=first_holder)
    if blocked_holder is not None and release_succeeded:
        takeover_succeeded = lease_store.acquire(
            lease,
            holder_id=blocked_holder,
            ttl_seconds=ttl_seconds,
            metadata={"component": "lease-rehearsal", "phase": "takeover"},
        ).acquired
        lease_store.release(lease, holder_id=blocked_holder)

    return LeaseRehearsalResult(
        lease_name=lease,
        holder_a=holder_a,
        holder_b=holder_b,
        first_acquired_holder=first_holder,
        second_acquired_holder=second_holder,
        blocked_holder=blocked_holder,
        renew_succeeded=renew_succeeded,
        release_succeeded=release_succeeded,
        takeover_succeeded=takeover_succeeded,
    )
