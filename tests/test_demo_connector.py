from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestrator.demo.api import DemoConnector
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


@pytest.mark.asyncio
async def test_ready_demo_connector_sets_empty_start_and_authenticates_config(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setenv("HASHI_DEMO_ENABLED", "1")
    monkeypatch.setenv("HASHI_DEMO_DAILY_RUN_LIMIT", "25")
    token = "t" * 32
    orchestrator = SimpleNamespace()
    server = SimpleNamespace(
        global_config=SimpleNamespace(bridge_home=tmp_path),
        secrets={"demo_service_token": token},
        orchestrator=orchestrator,
    )
    connector = DemoConnector(server)
    assert connector.profile.ready is True
    assert orchestrator._allow_empty_start is True

    good = SimpleNamespace(
        headers={"X-Hashi-Demo-Service-Token": token}
    )
    response = await connector.handle_config(good)
    assert response.status == 200
    payload = __import__("json").loads(response.text)
    assert payload["protocol"] == "hashi.shared-demo"
    assert payload["ready"] is True
    assert payload["capabilities"]["tools"] is False

    bad = SimpleNamespace(
        headers={"X-Hashi-Demo-Service-Token": "wrong"}
    )
    response = await connector.handle_config(bad)
    assert response.status == 503


@pytest.mark.asyncio
async def test_demo_run_replay_returns_existing_run_without_starting_worker():
    lease = SimpleNamespace(
        lease_id="lease_one",
        owner_id="demo:one",
        agent_id="demo_agent_one",
        lease_epoch="epoch_one",
        csrf_token="csrf",
    )

    class Store:
        def find_run_by_idempotency(self, **kwargs):
            assert kwargs == {
                "session_id": "ses_one",
                "owner_id": "demo:one",
                "idempotency_key": "1234567890abcdef",
            }
            return {
                "run_id": "run_one",
                "user_message_id": "msg_one",
                "user_text": "hello",
                "state": "completed",
            }

        def list_active_runs(self, **kwargs):
            raise AssertionError("replay must return before active-run admission")

    connector = DemoConnector.__new__(DemoConnector)
    connector.profile = SimpleNamespace(max_request_bytes=32768, max_input_chars=4000)
    connector.server = SimpleNamespace(session_store=Store())
    connector._run_locks = {}
    connector._service_auth = lambda request: None
    connector._require_lease = lambda request, write=False: lease
    connector._owned_session = lambda current, session_id: {"session_id": "ses_one"}

    async def should_not_start(_lease):
        raise AssertionError("idempotent replay must not start a Worker")

    connector._ensure_worker = should_not_start
    raw = b'{"idempotency_key":"1234567890abcdef","text":"hello"}'

    async def read():
        return raw

    request = SimpleNamespace(
        headers={},
        match_info={"session_id": "ses_one"},
        content_length=len(raw),
        read=read,
    )
    response = await connector.handle_run(request)
    payload = __import__("json").loads(response.text)
    assert response.status == 202
    assert payload == {
        "ok": True,
        "session_id": "ses_one",
        "run_id": "run_one",
        "message_id": "msg_one",
        "state": "completed",
        "replayed": True,
    }


@pytest.mark.asyncio
async def test_demo_run_admission_uses_native_worker_and_durable_budget():
    lease = SimpleNamespace(
        lease_id="lease_one",
        owner_id="demo:one",
        agent_id="demo_agent_one",
        lease_epoch="epoch_one",
        csrf_token="csrf",
    )
    calls = {"reserved": 0, "committed": 0, "touched": 0, "enqueue": None}

    class Store:
        def find_run_by_idempotency(self, **kwargs):
            return None

        def list_active_runs(self, **kwargs):
            assert kwargs == {"owner_id": "demo:one"}
            return []

        def get_run_by_request(self, request_id):
            assert request_id == "request_one"
            return {
                "run_id": "run_one",
                "user_message_id": "msg_one",
                "state": "queued",
            }

    class Leases:
        def reserve_daily_run(self, **kwargs):
            calls["reserved"] += 1
            assert kwargs["lease_id"] == "lease_one"
            assert kwargs["idempotency_key"] == "1234567890abcdef"
            assert kwargs["text"] == "hello"
            assert kwargs["limit"] == 25
            return False

        def commit_daily_run(self, **kwargs):
            calls["committed"] += 1
            assert kwargs == {
                "lease_id": "lease_one",
                "idempotency_key": "1234567890abcdef",
                "run_id": "run_one",
            }

        def touch(self, lease_id):
            calls["touched"] += 1
            assert lease_id == "lease_one"

    class Runtime:
        def _primary_chat_id(self):
            return "chat"

        async def enqueue_request(self, *args, **kwargs):
            calls["enqueue"] = (args, kwargs)
            return "request_one"

    connector = DemoConnector.__new__(DemoConnector)
    connector.profile = SimpleNamespace(
        max_request_bytes=32768,
        max_input_chars=4000,
        daily_run_limit=25,
    )
    connector.server = SimpleNamespace(session_store=Store())
    connector.leases = Leases()
    connector._run_locks = {}
    connector._generation_slots = __import__("asyncio").BoundedSemaphore(1)
    connector._service_auth = lambda request: None
    connector._require_lease = lambda request, write=False: lease
    connector._owned_session = lambda current, session_id: {
        "session_id": "ses_one",
        "context_generation": 1,
    }

    async def ensure_worker(_lease):
        return Runtime()

    connector._ensure_worker = ensure_worker

    def discard_watcher(coro, *, name):
        assert name == "hashi-demo-run:run_one"
        coro.close()

    connector._track = discard_watcher
    raw = b'{"idempotency_key":"1234567890abcdef","text":"hello"}'

    async def read():
        return raw

    request = SimpleNamespace(
        headers={},
        match_info={"session_id": "ses_one"},
        content_length=len(raw),
        read=read,
    )
    response = await connector.handle_run(request)
    payload = __import__("json").loads(response.text)
    assert response.status == 202
    assert payload["run_id"] == "run_one"
    assert payload["replayed"] is False
    assert calls["reserved"] == 1
    assert calls["committed"] == 1
    assert calls["touched"] == 1
    args, kwargs = calls["enqueue"]
    assert args[1] == "hello"
    assert kwargs["skip_memory_injection"] is True
    assert kwargs["habit_learning_eligible"] is False
    assert kwargs["request_metadata"]["owner_id"] == "demo:one"
    assert kwargs["request_metadata"]["session_id"] == "ses_one"
    assert kwargs["request_metadata"]["execution_mode"] == "zero"


@pytest.mark.asyncio
async def test_demo_cancel_is_owner_and_session_scoped():
    lease = SimpleNamespace(
        lease_id="lease_one",
        owner_id="demo:one",
        agent_id="demo_agent_one",
        lease_epoch="epoch_one",
        csrf_token="csrf",
    )

    class Store:
        def get_run(self, run_id, *, owner_id):
            assert run_id == "run_one"
            assert owner_id == "demo:one"
            return {"run_id": "run_one", "session_id": "ses_one", "state": "running"}

        def cancel_run(self, run_id, *, owner_id, reason):
            assert run_id == "run_one"
            assert owner_id == "demo:one"
            assert reason == "demo_user_cancel"
            return {"run_id": "run_one", "state": "stopped"}

    connector = DemoConnector.__new__(DemoConnector)
    connector.server = SimpleNamespace(
        session_store=Store(),
        _runtime_map=lambda: {},
    )
    connector._service_auth = lambda request: None
    connector._require_lease = lambda request, write=False: lease
    connector._owned_session = lambda current, session_id: {"session_id": "ses_one"}
    request = SimpleNamespace(
        headers={},
        match_info={"session_id": "ses_one", "run_id": "run_one"},
    )
    response = await connector.handle_cancel(request)
    payload = __import__("json").loads(response.text)
    assert response.status == 200
    assert payload["state"] == "stopped"
    assert payload["session_id"] == "ses_one"
