from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
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


@dataclass(frozen=True)
class LeaseLoadRehearsalResult:
    lease_prefix: str
    lease_count: int
    passed_count: int
    failed_count: int
    results: tuple[LeaseRehearsalResult, ...]

    @property
    def passed(self) -> bool:
        return (
            self.lease_count > 0
            and self.failed_count == 0
            and self.passed_count == self.lease_count
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "lease_prefix": self.lease_prefix,
            "lease_count": self.lease_count,
            "passed_count": self.passed_count,
            "failed_count": self.failed_count,
            "passed": self.passed,
            "results": [result.as_dict() for result in self.results],
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


def run_enterprise_lease_load_rehearsal(
    lease_store,
    *,
    lease_prefix: str | None = None,
    lease_count: int = 8,
    holder_a: str = "load-a",
    holder_b: str = "load-b",
    ttl_seconds: int = 30,
    max_workers: int = 4,
) -> LeaseLoadRehearsalResult:
    if lease_count < 1:
        raise ValueError("lease_count must be at least 1")
    if max_workers < 1:
        raise ValueError("max_workers must be at least 1")

    prefix = lease_prefix or f"lease-load-{uuid.uuid4().hex}"

    def rehearse(index: int) -> LeaseRehearsalResult:
        return run_enterprise_lease_rehearsal(
            lease_store,
            lease_name=f"{prefix}-{index}",
            holder_a=f"{holder_a}-{index}",
            holder_b=f"{holder_b}-{index}",
            ttl_seconds=ttl_seconds,
        )

    results: list[LeaseRehearsalResult] = []
    worker_count = min(max_workers, lease_count)
    with ThreadPoolExecutor(max_workers=worker_count) as pool:
        futures = {pool.submit(rehearse, index): index for index in range(lease_count)}
        for future in as_completed(futures):
            results.append(future.result())

    ordered_results = tuple(sorted(results, key=lambda result: result.lease_name))
    passed_count = sum(1 for result in ordered_results if result.passed)
    failed_count = lease_count - passed_count
    return LeaseLoadRehearsalResult(
        lease_prefix=prefix,
        lease_count=lease_count,
        passed_count=passed_count,
        failed_count=failed_count,
        results=ordered_results,
    )
