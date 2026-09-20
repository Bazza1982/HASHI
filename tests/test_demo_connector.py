from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestrator.demo.leases import (
    DemoBusy,
    DemoConflict,
    DemoExpired,
    DemoLeaseStore,
)
from orchestrator.demo.profile import DemoProfile
from orchestrator.session_store import SessionNotFound, SessionStore


def test_demo_profile_requires_explicit_enable_and_strong_service_token():
    cfg = SimpleNamespace()
    disabled = DemoProfile.from_runtime(
        cfg,
        {"demo_service_token": "x" * 32},
        environ={},
    )
    assert disabled.enabled is False
    assert disabled.ready is False

    ready = DemoProfile.from_runtime(
        cfg,
        {"demo_service_token": "x" * 32},
        environ={
            "HASHI_DEMO_ENABLED": "1",
            "HASHI_DEMO_DAILY_RUN_LIMIT": "1000",
        },
    )
    assert ready.ready is True
    assert ready.public_config()["capabilities"]["tools"] is False
    assert ready.public_config()["limits"]["max_sessions"] == 3


def test_demo_lease_store_hashes_credentials_and_enforces_capacity(tmp_path: Path):
    store = DemoLeaseStore(
        tmp_path / "demo.sqlite3",
        max_live_visitors=1,
        absolute_ttl_seconds=60,
        idle_ttl_seconds=30,
    )
    lease, token = store.allocate(locale="en", now=1000)
    assert token not in (tmp_path / "demo.sqlite3").read_bytes().decode("latin1", errors="ignore")
    ready = store.mark_ready(lease.lease_id)
    assert store.authenticate(token, now=1001).lease_id == ready.lease_id
    with pytest.raises(DemoBusy):
        store.allocate(locale="en", now=1002)


def test_demo_lease_expiry_revokes_identity(tmp_path: Path):
    store = DemoLeaseStore(
        tmp_path / "demo.sqlite3",
        max_live_visitors=2,
        absolute_ttl_seconds=10,
        idle_ttl_seconds=0,
    )
    lease, token = store.allocate(now=100)
    store.mark_ready(lease.lease_id)
    with pytest.raises(DemoExpired):
        store.authenticate(token, now=111)
    assert store.list_expired_or_pending(now=111)[0].lease_id == lease.lease_id


def test_session_creation_intent_is_idempotent_and_detects_conflict(tmp_path: Path):
    store = DemoLeaseStore(
        tmp_path / "demo.sqlite3",
        max_live_visitors=2,
        absolute_ttl_seconds=60,
        idle_ttl_seconds=0,
    )
    lease, _ = store.allocate(now=time.time())
    store.mark_ready(lease.lease_id)
    calls = []

    def create():
        calls.append(1)
        return {"session_id": "ses_one", "title": "One"}

    first = store.session_intent(
        lease_id=lease.lease_id,
        idempotency_key="1234567890abcdef",
        title="One",
        create_session=create,
    )
    second = store.session_intent(
        lease_id=lease.lease_id,
        idempotency_key="1234567890abcdef",
        title="One",
        create_session=create,
    )
    assert first == ("ses_one", "One", False)
    assert second == ("ses_one", "One", True)
    assert calls == [1]
    with pytest.raises(DemoConflict):
        store.session_intent(
            lease_id=lease.lease_id,
            idempotency_key="1234567890abcdef",
            title="Different",
            create_session=create,
        )


def test_session_store_demo_policy_and_owner_purge_are_scoped(tmp_path: Path):
    store = SessionStore(tmp_path / "sessions.sqlite3")
    demo = store.create_session(owner_id="demo:one", agent_id="demo_agent")
    other = store.create_session(owner_id="user:2", agent_id="normal_agent")

    updated = store.set_memory_policy(
        demo["session_id"], owner_id="demo:one", policy="disabled"
    )
    assert updated["memory_policy"] == "disabled"
    assert store.list_active_runs(owner_id="demo:one") == []

    result = store.purge_owner(owner_id="demo:one", agent_id="demo_agent")
    assert result["sessions"] == 1
    with pytest.raises(SessionNotFound):
        store.get_session(demo["session_id"], owner_id="demo:one")
    assert store.get_session(other["session_id"], owner_id="user:2")["agent_id"] == "normal_agent"


def test_owner_purge_does_not_cross_agent_boundary(tmp_path: Path):
    store = SessionStore(tmp_path / "sessions.sqlite3")
    a = store.create_session(owner_id="demo:shared", agent_id="demo_a")
    b = store.create_session(owner_id="demo:shared", agent_id="demo_b")
    store.purge_owner(owner_id="demo:shared", agent_id="demo_a")
    with pytest.raises(SessionNotFound):
        store.get_session(a["session_id"], owner_id="demo:shared")
    assert store.get_session(b["session_id"], owner_id="demo:shared")["agent_id"] == "demo_b"


def test_demo_daily_budget_is_durable_conservative_and_unlinkable_after_purge(tmp_path: Path):
    store = DemoLeaseStore(
        tmp_path / "demo.sqlite3",
        max_live_visitors=2,
        absolute_ttl_seconds=60,
        idle_ttl_seconds=0,
    )
    first, _ = store.allocate(now=100)
    second, _ = store.allocate(now=100)
    store.mark_ready(first.lease_id)
    store.mark_ready(second.lease_id)

    assert store.reserve_daily_run(
        lease_id=first.lease_id,
        idempotency_key="1234567890abcdef",
        text="hello",
        limit=2,
        now=100,
    ) is False
    assert store.reserve_daily_run(
        lease_id=first.lease_id,
        idempotency_key="1234567890abcdef",
        text="hello",
        limit=2,
        now=101,
    ) is True
    with pytest.raises(DemoConflict):
        store.reserve_daily_run(
            lease_id=first.lease_id,
            idempotency_key="1234567890abcdef",
            text="changed",
            limit=2,
            now=102,
        )

    assert store.reserve_daily_run(
        lease_id=second.lease_id,
        idempotency_key="abcdef1234567890",
        text="second",
        limit=2,
        now=103,
    ) is False
    assert store.budget_used(now=103) == 2

    from orchestrator.demo.leases import DemoBudgetExhausted

    with pytest.raises(DemoBudgetExhausted):
        store.reserve_daily_run(
            lease_id=second.lease_id,
            idempotency_key="fedcba0987654321",
            text="third",
            limit=2,
            now=104,
        )

    store.delete(first.lease_id)
    # Per-visitor reservation rows cascade away, while the aggregate daily
    # counter survives so clearing a visitor cannot reset the public budget.
    assert store.budget_used(now=105) == 2
