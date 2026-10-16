"""Unit tests for ingress confirmation persistence and state machine (PAO Functions layer)."""
from __future__ import annotations

import asyncio
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
from pathlib import Path
from threading import Barrier, Event, Lock
import pytest

from orchestrator import ingress_confirmation as ingress_confirmation_module
from orchestrator.session_store import SessionStore
from orchestrator.ingress_confirmation import (
    IngressConfirmationService,
    ConfirmationNotFoundError,
    ConfirmationConflictError,
    ConfirmationExpiredError,
    ConfirmationSupersededError,
)


def _digest(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


@pytest.fixture
def store(tmp_path: Path) -> SessionStore:
    db_path = tmp_path / "sessions.sqlite"
    store = SessionStore(db_path, instance_id="HASHI3")
    return store


@pytest.fixture
def test_session(store: SessionStore):
    return store.create_session(
        owner_id="user:alice",
        agent_id="kasumi",
        title="Test Session",
    )


def test_register_and_get_pending_confirmation(store: SessionStore, test_session):
    now = datetime(2026, 10, 16, 12, 0, 0, tzinfo=timezone.utc)
    service = IngressConfirmationService(store, clock=lambda: now)

    payload = {"text": "Delayed message", "blocks": [{"type": "text", "text": "Delayed message"}]}
    payload_json = json.dumps(payload, sort_keys=True)
    digest = _digest(payload_json)

    rec = service.register_pending(
        session_id=test_session["session_id"],
        owner_id="user:alice",
        agent_id="kasumi",
        connector_id="session_api",
        transport="workbench",
        source_message_id="msg-001",
        idempotency_key="idemp-001",
        payload_digest=digest,
        message_kind="frontend_message",
        context_generation=test_session["context_generation"],
        originated_at="2026-10-16T11:55:00Z",
        received_at="2026-10-16T12:00:00Z",
        threshold_seconds=30,
        reason="stale",
        delay_seconds=300.0,
        expires_at="2026-10-16T12:15:00Z",
        payload=payload,
        display_metadata={"text": "Delayed message", "attachment_count": 0},
    )

    assert rec["confirmation_id"].startswith("conf-")
    assert rec["state"] == "awaiting_confirmation"
    assert rec["reason"] == "stale"
    assert rec["delay_seconds"] == 300.0
    assert rec["source_message_id"] == "msg-001"
    assert rec["idempotency_key"] == "idemp-001"
    assert rec["run_id"] is None

    # Retrieve by ID
    retrieved = service.get(rec["confirmation_id"], owner_id="user:alice", session_id=test_session["session_id"])
    assert retrieved["confirmation_id"] == rec["confirmation_id"]
    assert retrieved["state"] == "awaiting_confirmation"

    # Listing
    items = service.list(session_id=test_session["session_id"], owner_id="user:alice")
    assert len(items) == 1
    assert items[0]["confirmation_id"] == rec["confirmation_id"]


def test_idempotent_registration_and_conflict(store: SessionStore, test_session):
    now = datetime(2026, 10, 16, 12, 0, 0, tzinfo=timezone.utc)
    service = IngressConfirmationService(store, clock=lambda: now)

    payload = {"text": "Original"}
    digest = _digest(json.dumps(payload))

    rec1 = service.register_pending(
        session_id=test_session["session_id"],
        owner_id="user:alice",
        agent_id="kasumi",
        connector_id="session_api",
        transport="workbench",
        source_message_id="msg-replay",
        idempotency_key="idemp-replay",
        payload_digest=digest,
        message_kind="frontend_message",
        context_generation=test_session["context_generation"],
        originated_at="2026-10-16T11:55:00Z",
        received_at="2026-10-16T12:00:00Z",
        threshold_seconds=30,
        reason="stale",
        delay_seconds=300.0,
        expires_at="2026-10-16T12:15:00Z",
        payload=payload,
        display_metadata={"text": "Original"},
    )

    # Same key and same digest -> returns existing record
    rec2 = service.register_pending(
        session_id=test_session["session_id"],
        owner_id="user:alice",
        agent_id="kasumi",
        connector_id="session_api",
        transport="workbench",
        source_message_id="msg-replay",
        idempotency_key="idemp-replay",
        payload_digest=digest,
        message_kind="frontend_message",
        context_generation=test_session["context_generation"],
        originated_at="2026-10-16T11:55:00Z",
        received_at="2026-10-16T12:00:00Z",
        threshold_seconds=30,
        reason="stale",
        delay_seconds=300.0,
        expires_at="2026-10-16T12:15:00Z",
        payload=payload,
        display_metadata={"text": "Original"},
    )
    assert rec2["confirmation_id"] == rec1["confirmation_id"]

    # Same key with DIFFERENT digest -> conflict error
    diff_payload = {"text": "Tampered"}
    diff_digest = _digest(json.dumps(diff_payload))
    with pytest.raises(ConfirmationConflictError):
        service.register_pending(
            session_id=test_session["session_id"],
            owner_id="user:alice",
            agent_id="kasumi",
            connector_id="session_api",
            transport="workbench",
            source_message_id="msg-replay",
            idempotency_key="idemp-replay",
            payload_digest=diff_digest,
            message_kind="frontend_message",
            context_generation=test_session["context_generation"],
            originated_at="2026-10-16T11:55:00Z",
            received_at="2026-10-16T12:00:00Z",
            threshold_seconds=30,
            reason="stale",
            delay_seconds=300.0,
            expires_at="2026-10-16T12:15:00Z",
            payload=diff_payload,
            display_metadata={"text": "Tampered"},
        )


def test_registration_rejects_crossed_idempotency_and_source_identities(
    store: SessionStore, test_session
):
    now = datetime(2026, 10, 16, 12, 0, 0, tzinfo=timezone.utc)
    service = IngressConfirmationService(store, clock=lambda: now)

    def register(*, source_message_id: str, idempotency_key: str, text: str):
        payload = {"text": text}
        return service.register_pending(
            session_id=test_session["session_id"],
            owner_id="user:alice",
            agent_id="kasumi",
            connector_id="session_api",
            transport="workbench",
            source_message_id=source_message_id,
            idempotency_key=idempotency_key,
            payload_digest=_digest(json.dumps(payload, sort_keys=True)),
            message_kind="frontend_message",
            context_generation=test_session["context_generation"],
            originated_at="2026-10-16T11:55:00Z",
            received_at="2026-10-16T12:00:00Z",
            threshold_seconds=30,
            reason="stale",
            delay_seconds=300.0,
            expires_at="2026-10-16T12:15:00Z",
            payload=payload,
            display_metadata={"text": text},
        )

    register(source_message_id="source-a", idempotency_key="idemp-a", text="A")
    register(source_message_id="source-b", idempotency_key="idemp-b", text="B")

    with pytest.raises(ConfirmationConflictError):
        register(
            source_message_id="source-b",
            idempotency_key="idemp-a",
            text="crossed identity",
        )


def test_concurrent_registration_across_store_instances_returns_one_record(
    store: SessionStore, test_session
):
    now = datetime(2026, 10, 16, 12, 0, 0, tzinfo=timezone.utc)
    second_store = SessionStore(
        store.db_path,
        instance_id="HASHI3",
        attachment_root=store.attachment_files_root,
    )
    services = (
        IngressConfirmationService(store, clock=lambda: now),
        IngressConfirmationService(second_store, clock=lambda: now),
    )
    payload = {"text": "one delayed message"}
    digest = _digest(json.dumps(payload, sort_keys=True))
    start = Barrier(2)

    def register(service):
        start.wait(timeout=5)
        return service.register_pending(
            session_id=test_session["session_id"],
            owner_id="user:alice",
            agent_id="kasumi",
            connector_id="session_api",
            transport="workbench",
            source_message_id="msg-concurrent-register",
            idempotency_key="idemp-concurrent-register",
            payload_digest=digest,
            message_kind="frontend_message",
            context_generation=test_session["context_generation"],
            originated_at="2026-10-16T11:55:00Z",
            received_at="2026-10-16T12:00:00Z",
            threshold_seconds=30,
            reason="stale",
            delay_seconds=300.0,
            expires_at="2026-10-16T12:15:00Z",
            payload=payload,
            display_metadata={"text": payload["text"]},
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        records = list(pool.map(register, services))

    assert records[0]["confirmation_id"] == records[1]["confirmation_id"]
    assert len(
        services[0].list(
            session_id=test_session["session_id"], owner_id="user:alice"
        )
    ) == 1


def test_discard_decision_never_creates_run(store: SessionStore, test_session):
    now = datetime(2026, 10, 16, 12, 0, 0, tzinfo=timezone.utc)
    service = IngressConfirmationService(store, clock=lambda: now)

    payload = {"text": "Discard me"}
    digest = _digest(json.dumps(payload))

    rec = service.register_pending(
        session_id=test_session["session_id"],
        owner_id="user:alice",
        agent_id="kasumi",
        connector_id="session_api",
        transport="workbench",
        source_message_id="msg-discard",
        idempotency_key="idemp-discard",
        payload_digest=digest,
        message_kind="frontend_message",
        context_generation=test_session["context_generation"],
        originated_at="2026-10-16T11:55:00Z",
        received_at="2026-10-16T12:00:00Z",
        threshold_seconds=30,
        reason="stale",
        delay_seconds=300.0,
        expires_at="2026-10-16T12:15:00Z",
        payload=payload,
        display_metadata={"text": "Discard me"},
    )

    res = service.decide(
        confirmation_id=rec["confirmation_id"],
        decision="discard",
        owner_id="user:alice",
    )

    assert res["state"] == "discarded"
    assert res["decision"] == "discard"
    assert res["run_id"] is None

    # Verify no run was created in session_store
    runs = store.recent_session_runs(test_session["session_id"], owner_id="user:alice")
    assert len(runs) == 0

    # Repeated decision returns same discarded state
    res_repeat = service.decide(
        confirmation_id=rec["confirmation_id"],
        decision="discard",
        owner_id="user:alice",
    )
    assert res_repeat["state"] == "discarded"


def test_confirm_decision_creates_exactly_one_run(store: SessionStore, test_session):
    now = datetime(2026, 10, 16, 12, 0, 0, tzinfo=timezone.utc)
    service = IngressConfirmationService(store, clock=lambda: now)

    payload = {
        "text": "Confirmed message",
        "content": [{"type": "text", "text": "Confirmed message"}],
    }
    digest = _digest(json.dumps(payload))

    rec = service.register_pending(
        session_id=test_session["session_id"],
        owner_id="user:alice",
        agent_id="kasumi",
        connector_id="session_api",
        transport="workbench",
        source_message_id="msg-confirm",
        idempotency_key="idemp-confirm",
        payload_digest=digest,
        message_kind="frontend_message",
        context_generation=test_session["context_generation"],
        originated_at="2026-10-16T11:55:00Z",
        received_at="2026-10-16T12:00:00Z",
        threshold_seconds=30,
        reason="stale",
        delay_seconds=300.0,
        expires_at="2026-10-16T12:15:00Z",
        payload=payload,
        display_metadata={"text": "Confirmed message"},
    )

    # Executor callback that admits the run into session_store
    def execute_callback(confirmation_record):
        accepted = store.accept_run(
            session_id=confirmation_record["session_id"],
            owner_id=confirmation_record["owner_id"],
            agent_id=confirmation_record["agent_id"],
            request_id=f"req-{confirmation_record['confirmation_id']}",
            text=confirmation_record["payload"]["text"],
            source=confirmation_record["transport"],
            idempotency_key=confirmation_record["idempotency_key"],
            content=confirmation_record["payload"]["content"],
            expected_context_generation=confirmation_record["context_generation"],
        )
        return accepted.run_id

    res1 = service.decide(
        confirmation_id=rec["confirmation_id"],
        decision="confirm",
        owner_id="user:alice",
        execute_fn=execute_callback,
    )

    assert res1["state"] == "confirmed"
    assert res1["decision"] == "confirm"
    assert res1["run_id"] is not None

    # Check that exactly one Run was created in session_store
    runs = store.recent_session_runs(test_session["session_id"], owner_id="user:alice")
    assert len(runs) == 1
    assert runs[0]["run_id"] == res1["run_id"]

    # Repeated confirm returns same run_id without executing execute_callback again
    res2 = service.decide(
        confirmation_id=rec["confirmation_id"],
        decision="confirm",
        owner_id="user:alice",
        execute_fn=lambda _: pytest.fail("Should not be executed twice"),
    )
    assert res2["state"] == "confirmed"
    assert res2["run_id"] == res1["run_id"]

    runs_after = store.recent_session_runs(test_session["session_id"], owner_id="user:alice")
    assert len(runs_after) == 1


def test_lazy_expiration(store: SessionStore, test_session):
    current_time = datetime(2026, 10, 16, 12, 0, 0, tzinfo=timezone.utc)
    service = IngressConfirmationService(store, clock=lambda: current_time)

    payload = {"text": "Expiring message"}
    digest = _digest(json.dumps(payload))

    rec = service.register_pending(
        session_id=test_session["session_id"],
        owner_id="user:alice",
        agent_id="kasumi",
        connector_id="session_api",
        transport="workbench",
        source_message_id="msg-exp",
        idempotency_key="idemp-exp",
        payload_digest=digest,
        message_kind="frontend_message",
        context_generation=test_session["context_generation"],
        originated_at="2026-10-16T11:55:00Z",
        received_at="2026-10-16T12:00:00Z",
        threshold_seconds=30,
        reason="stale",
        delay_seconds=300.0,
        expires_at="2026-10-16T12:15:00Z",
        payload=payload,
        display_metadata={"text": "Expiring message"},
    )

    # Time advances past expires_at (15 min later)
    current_time = datetime(2026, 10, 16, 12, 15, 1, tzinfo=timezone.utc)

    # Retrieval now shows expired
    retrieved = service.get(rec["confirmation_id"], owner_id="user:alice")
    assert retrieved["state"] == "expired"

    # Attempting to confirm an expired record raises ConfirmationExpiredError
    with pytest.raises(ConfirmationExpiredError):
        service.decide(
            confirmation_id=rec["confirmation_id"],
            decision="confirm",
            owner_id="user:alice",
            execute_fn=lambda _: pytest.fail("Should not execute expired message"),
        )


def test_superseded_when_context_generation_advances(store: SessionStore, test_session):
    now = datetime(2026, 10, 16, 12, 0, 0, tzinfo=timezone.utc)
    service = IngressConfirmationService(store, clock=lambda: now)

    payload = {"text": "Superseded message"}
    digest = _digest(json.dumps(payload))

    rec = service.register_pending(
        session_id=test_session["session_id"],
        owner_id="user:alice",
        agent_id="kasumi",
        connector_id="session_api",
        transport="workbench",
        source_message_id="msg-super",
        idempotency_key="idemp-super",
        payload_digest=digest,
        message_kind="frontend_message",
        context_generation=test_session["context_generation"],  # gen 1
        originated_at="2026-10-16T11:55:00Z",
        received_at="2026-10-16T12:00:00Z",
        threshold_seconds=30,
        reason="stale",
        delay_seconds=300.0,
        expires_at="2026-10-16T12:15:00Z",
        payload=payload,
        display_metadata={"text": "Superseded message"},
    )

    # Advance context generation in session_store (e.g. /new or reset)
    store.start_fresh_generation(
        session_id=test_session["session_id"],
        reason="user_reset",
    )

    # Reading confirmation updates to superseded
    retrieved = service.get(rec["confirmation_id"], owner_id="user:alice")
    assert retrieved["state"] == "superseded"

    # Confirming raises ConfirmationSupersededError
    with pytest.raises(ConfirmationSupersededError):
        service.decide(
            confirmation_id=rec["confirmation_id"],
            decision="confirm",
            owner_id="user:alice",
            execute_fn=lambda _: pytest.fail("Should not execute superseded message"),
        )


def _register_confirmation(
    service: IngressConfirmationService,
    session: dict,
    *,
    suffix: str,
) -> dict:
    payload = {
        "text": f"message-{suffix}",
        "content": [{"type": "text", "text": f"message-{suffix}"}],
    }
    return service.register_pending(
        session_id=session["session_id"],
        owner_id="user:alice",
        agent_id="kasumi",
        connector_id="session_api",
        transport="workbench",
        source_message_id=f"msg-{suffix}",
        idempotency_key=f"idemp-{suffix}",
        payload_digest=_digest(json.dumps(payload, sort_keys=True)),
        message_kind="frontend_message",
        context_generation=session["context_generation"],
        originated_at="2026-10-16T11:55:00Z",
        received_at="2026-10-16T12:00:00Z",
        threshold_seconds=30,
        reason="stale",
        delay_seconds=300.0,
        expires_at="2026-10-16T12:15:00Z",
        payload=payload,
        display_metadata={"text": payload["text"]},
    )


def test_concurrent_confirm_has_one_execution_winner(store: SessionStore, test_session):
    now = datetime(2026, 10, 16, 12, 0, 0, tzinfo=timezone.utc)
    service = IngressConfirmationService(store, clock=lambda: now)
    rec = _register_confirmation(service, test_session, suffix="concurrent")
    entered = Event()
    release = Event()
    calls = 0
    calls_lock = Lock()

    def execute_once(record):
        nonlocal calls
        with calls_lock:
            calls += 1
        entered.set()
        assert release.wait(timeout=5)
        accepted = store.accept_run(
            session_id=record["session_id"],
            owner_id=record["owner_id"],
            agent_id=record["agent_id"],
            request_id="req-concurrent",
            text=record["payload"]["text"],
            source=record["transport"],
            idempotency_key=record["idempotency_key"],
            content=record["payload"]["content"],
            expected_context_generation=record["context_generation"],
        )
        return accepted.run_id

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(
            service.decide,
            confirmation_id=rec["confirmation_id"],
            decision="confirm",
            owner_id="user:alice",
            execute_fn=execute_once,
        )
        assert entered.wait(timeout=5)
        duplicate = service.decide(
            confirmation_id=rec["confirmation_id"],
            decision="confirm",
            owner_id="user:alice",
            execute_fn=lambda _: pytest.fail("concurrent confirm executed twice"),
        )
        assert duplicate["state"] == "confirming"
        release.set()
        completed = first.result(timeout=5)

    assert completed["state"] == "confirmed"
    assert calls == 1
    runs = store.recent_session_runs(test_session["session_id"], owner_id="user:alice")
    assert len(runs) == 1


def test_restart_reconciles_run_created_before_confirmation_receipt(
    store: SessionStore, test_session
):
    now = datetime(2026, 10, 16, 12, 0, 0, tzinfo=timezone.utc)
    service = IngressConfirmationService(store, clock=lambda: now)
    rec = _register_confirmation(service, test_session, suffix="restart-after-run")

    # Simulate a process dying after its durable claim and Run admission but
    # before it can persist the terminal confirmation receipt.
    claimed, token = service._claim_confirmation(
        confirmation_id=rec["confirmation_id"],
        decision="confirm",
        owner_id="user:alice",
    )
    assert token
    accepted = store.accept_run(
        session_id=claimed["session_id"],
        owner_id=claimed["owner_id"],
        agent_id=claimed["agent_id"],
        request_id="req-restart-after-run",
        text=claimed["payload"]["text"],
        source=claimed["transport"],
        idempotency_key=claimed["idempotency_key"],
        content=claimed["payload"]["content"],
        expected_context_generation=claimed["context_generation"],
    )

    restarted = IngressConfirmationService(store, clock=lambda: now)
    recovered = restarted.decide(
        confirmation_id=rec["confirmation_id"],
        decision="confirm",
        owner_id="user:alice",
        execute_fn=lambda _: pytest.fail("existing Run must be recovered, not recreated"),
    )
    assert recovered["state"] == "confirmed"
    assert recovered["run_id"] == accepted.run_id
    assert len(
        store.recent_session_runs(test_session["session_id"], owner_id="user:alice")
    ) == 1


def test_restart_reclaims_abandoned_pre_run_lease(store: SessionStore, test_session):
    now = datetime(2026, 10, 16, 12, 0, 0, tzinfo=timezone.utc)
    service = IngressConfirmationService(store, clock=lambda: now)
    rec = _register_confirmation(service, test_session, suffix="restart-before-run")
    _, token = service._claim_confirmation(
        confirmation_id=rec["confirmation_id"],
        decision="confirm",
        owner_id="user:alice",
    )
    assert token

    restarted_at = now + timedelta(seconds=31)
    restarted = IngressConfirmationService(store, clock=lambda: restarted_at)

    def execute_after_restart(record):
        return store.accept_run(
            session_id=record["session_id"],
            owner_id=record["owner_id"],
            agent_id=record["agent_id"],
            request_id="req-restart-before-run",
            text=record["payload"]["text"],
            source=record["transport"],
            idempotency_key=record["idempotency_key"],
            content=record["payload"]["content"],
            expected_context_generation=record["context_generation"],
        ).run_id

    recovered = restarted.decide(
        confirmation_id=rec["confirmation_id"],
        decision="confirm",
        owner_id="user:alice",
        execute_fn=execute_after_restart,
    )
    assert recovered["state"] == "confirmed"
    assert len(
        store.recent_session_runs(test_session["session_id"], owner_id="user:alice")
    ) == 1


@pytest.mark.asyncio
async def test_slow_async_confirmation_renews_lease_and_cannot_execute_twice(
    store: SessionStore, test_session, monkeypatch
):
    monkeypatch.setattr(
        ingress_confirmation_module,
        "CONFIRMATION_EXECUTION_LEASE_SECONDS",
        0.12,
    )
    service = IngressConfirmationService(store)
    now = datetime.now(timezone.utc)
    payload = {
        "text": "slow connector action",
        "content": [{"type": "text", "text": "slow connector action"}],
    }
    rec = service.register_pending(
        session_id=test_session["session_id"],
        owner_id="user:alice",
        agent_id="kasumi",
        connector_id="telegram",
        transport="telegram",
        source_message_id="msg-slow-heartbeat",
        idempotency_key="idemp-slow-heartbeat",
        payload_digest=_digest(json.dumps(payload, sort_keys=True)),
        message_kind="frontend_message",
        context_generation=test_session["context_generation"],
        originated_at=(now - timedelta(seconds=90)),
        received_at=now,
        threshold_seconds=30,
        reason="stale",
        delay_seconds=90.0,
        expires_at=now + timedelta(minutes=15),
        payload=payload,
        display_metadata={"text": payload["text"]},
    )
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def slow_execute(_record):
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return None

    first = asyncio.create_task(
        service.decide_async(
            confirmation_id=rec["confirmation_id"],
            decision="confirm",
            owner_id="user:alice",
            execute_fn=slow_execute,
        )
    )
    try:
        await asyncio.wait_for(started.wait(), timeout=1)
        # Stay in flight for more than two complete unrenewed lease periods.
        await asyncio.sleep(0.30)
        duplicate = await service.decide_async(
            confirmation_id=rec["confirmation_id"],
            decision="confirm",
            owner_id="user:alice",
            execute_fn=lambda _record: pytest.fail(
                "a live renewed execution lease must not be reclaimed"
            ),
        )
        assert duplicate["state"] == "confirming"
        assert calls == 1
    finally:
        release.set()
    completed = await asyncio.wait_for(first, timeout=1)
    assert completed["state"] == "confirmed"
    assert calls == 1


def test_confirmation_owner_and_session_boundaries_are_not_cross_readable(
    store: SessionStore, test_session
):
    now = datetime(2026, 10, 16, 12, 0, 0, tzinfo=timezone.utc)
    service = IngressConfirmationService(store, clock=lambda: now)
    record = _register_confirmation(service, test_session, suffix="owner-boundary")
    other_session = store.create_session(
        owner_id="user:alice",
        agent_id="other-agent",
        title="Other session",
    )

    with pytest.raises(ConfirmationNotFoundError):
        service.get(record["confirmation_id"], owner_id="user:bob")
    with pytest.raises(ConfirmationNotFoundError):
        service.get(
            record["confirmation_id"],
            owner_id="user:alice",
            session_id=other_session["session_id"],
        )
    with pytest.raises(ConfirmationNotFoundError):
        service.decide(
            confirmation_id=record["confirmation_id"],
            decision="confirm",
            owner_id="user:bob",
            execute_fn=lambda _record: pytest.fail("cross-owner execution"),
        )
