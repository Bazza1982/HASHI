"""Tests for HASHI Frontend Connector standard interface (M1 qualification).

Covers:
- T02: Unified ingress admission, deduplication, conflict detection
- T03: Command and control admission (cancellation, reservation, execution)
- T04: Unified event feed projection (durable & ephemeral lanes, cursors)
- T05: Standard content presentation rendering (text, HTML, structured cards)
- T06: Durable dispatcher crash boundaries, lease fencing, competing workers
- T07: Multi-destination delivery with independent receipts
- T10: Transport failure simulation (timeout -> unknown, rejection -> failed)
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestrator.frontend_connector_registry import ReferenceConnectorAdapter
from orchestrator.frontend_contracts import build_frontend_ingress_envelope
from orchestrator.frontend_dispatch import FrontendDispatcher
from orchestrator.frontend_ingress import (
    admit_frontend_ingress,
    query_ingress_admission,
)
from orchestrator.frontend_projection import (
    poll_frontend_feed,
    project_ephemeral_event,
    project_frontend_event,
    render_event_to_html,
    render_event_to_plain_text,
)
from orchestrator.session_store import SessionStore


def _setup_runtime(tmp_path: Path):
    store = SessionStore(
        tmp_path / "sessions.sqlite3",
        instance_id="HASHI1",
    )
    owner = "user:7"
    session = store.ensure_default_session(owner_id=owner, agent_id="test-agent")

    class FakeRegistry:
        def __init__(self):
            self.cmds = {
                "ping": SimpleNamespace(
                    handler=lambda rt, *args: {"ok": True, "pong": True, "args": list(args)}
                )
            }

        def has_command(self, cmd):
            return cmd in self.cmds

        def get_command(self, cmd):
            return self.cmds[cmd]

    runtime = SimpleNamespace(
        name="test-agent",
        global_config=SimpleNamespace(instance_id="HASHI1", authorized_id=7),
        session_store=store,
        command_registry=FakeRegistry(),
    )
    return runtime, store, owner, session


@pytest.mark.asyncio
async def test_t02_ingress_admission_idempotency_and_conflict(tmp_path: Path):
    runtime, store, owner, session = _setup_runtime(tmp_path)

    envelope = build_frontend_ingress_envelope(
        source_id="backend_api",
        ingress_transport="session-api",
        surface="workbench",
        channel_key="default",
        instance_id="HASHI1",
        principal={"kind": "human_or_client", "assurance": "runtime_observed"},
        network_authentication="not_applicable",
        relay_chain=[],
        request_id="req-turn-1",
        idempotency_key="key-turn-1",
        session_id=session["session_id"],
        agent_id="test-agent",
    )

    # 1. First admission succeeds
    receipt1 = admit_frontend_ingress(
        runtime,
        envelope,
        text="Hello agent",
    )
    assert receipt1["status"] == "accepted"
    assert receipt1["replayed"] is False
    assert receipt1["run_id"] is not None

    # 2. Identical retry returns replayed
    receipt2 = admit_frontend_ingress(
        runtime,
        envelope,
        text="Hello agent",
    )
    assert receipt2["status"] == "accepted"
    assert receipt2["replayed"] is True
    assert receipt2["run_id"] == receipt1["run_id"]

    # 3. Same key with different content returns conflict
    receipt3 = admit_frontend_ingress(
        runtime,
        envelope,
        text="Different text conflict",
    )
    assert receipt3["status"] == "conflict"

    # 4. Unknown outcome query finds the admitted run
    queried = query_ingress_admission(
        runtime,
        session_id=session["session_id"],
        request_id="req-turn-1",
        idempotency_digest=envelope["message"]["idempotency_digest"],
    )
    assert queried is not None
    assert queried["status"] == "accepted"
    assert queried["run_id"] == receipt1["run_id"]


@pytest.mark.asyncio
async def test_t03_ingress_command_and_control_cancellation(tmp_path: Path):
    runtime, store, owner, session = _setup_runtime(tmp_path)

    # Admit a turn to have an active run
    env_msg = build_frontend_ingress_envelope(
        source_id="backend_api",
        ingress_transport="session-api",
        surface="workbench",
        channel_key="default",
        instance_id="HASHI1",
        principal={"kind": "human_or_client", "assurance": "runtime_observed"},
        network_authentication="not_applicable",
        relay_chain=[],
        request_id="req-active-1",
        idempotency_key="key-active-1",
        session_id=session["session_id"],
        agent_id="test-agent",
    )
    run_receipt = admit_frontend_ingress(runtime, env_msg, text="Running task")
    assert run_receipt["status"] == "accepted"

    # Cancel control action
    env_cancel = build_frontend_ingress_envelope(
        source_id="backend_api",
        ingress_transport="session-api",
        surface="workbench",
        channel_key="default",
        instance_id="HASHI1",
        principal={"kind": "human_or_client", "assurance": "runtime_observed"},
        network_authentication="not_applicable",
        relay_chain=[],
        request_id="req-cancel-1",
        idempotency_key="key-cancel-1",
        session_id=session["session_id"],
        agent_id="test-agent",
    )
    cancel_receipt = admit_frontend_ingress(
        runtime,
        env_cancel,
        control_action="cancel",
    )
    assert cancel_receipt["status"] == "accepted"
    assert cancel_receipt["run_id"] == run_receipt["run_id"]

    # A replay returns the original outcome and must not cancel a later Run.
    env_later = build_frontend_ingress_envelope(
        source_id="backend_api",
        ingress_transport="session-api",
        surface="workbench",
        channel_key="default",
        instance_id="HASHI1",
        principal={"kind": "human_or_client", "assurance": "runtime_observed"},
        network_authentication="not_applicable",
        relay_chain=[],
        request_id="req-active-2",
        idempotency_key="key-active-2",
        session_id=session["session_id"],
        agent_id="test-agent",
    )
    later_run = admit_frontend_ingress(runtime, env_later, text="Later task")
    cancel_replay = admit_frontend_ingress(
        runtime,
        env_cancel,
        control_action="cancel",
    )
    assert cancel_replay["status"] == "accepted"
    assert cancel_replay["replayed"] is True
    assert cancel_replay["run_id"] == run_receipt["run_id"]
    active_run_ids = {
        item["run_id"]
        for item in store.list_active_runs(
            owner_id=owner,
            session_id=session["session_id"],
        )
    }
    assert later_run["run_id"] in active_run_ids

    # Command execution admission
    env_cmd = build_frontend_ingress_envelope(
        source_id="telegram",
        ingress_transport="telegram",
        surface="telegram",
        channel_key="chat-1",
        instance_id="HASHI1",
        principal={"kind": "human_or_client", "assurance": "runtime_observed"},
        network_authentication="not_applicable",
        relay_chain=[],
        request_id="req-cmd-ping",
        idempotency_key="key-cmd-ping",
        session_id=session["session_id"],
        agent_id="test-agent",
    )
    cmd_receipt = admit_frontend_ingress(
        runtime,
        env_cmd,
        command_name="ping",
        command_arguments=["fast"],
    )
    assert cmd_receipt["status"] == "accepted"
    assert cmd_receipt["replayed"] is False

    # Command replay does not re-execute handler
    cmd_replay = admit_frontend_ingress(
        runtime,
        env_cmd,
        command_name="ping",
        command_arguments=["fast"],
    )
    assert cmd_replay["status"] == "accepted"
    assert cmd_replay["replayed"] is True


def test_t04_t05_frontend_projection_and_presentation_rendering(tmp_path: Path):
    runtime, store, owner, session = _setup_runtime(tmp_path)

    # 1. Presentation event (meter card) in SessionStore
    msg = store.append_presentation_message(
        session_id=session["session_id"],
        owner_id=owner,
        agent_id="test-agent",
        role="assistant",
        text="Token usage summary",
        source="meter",
        idempotency_key="meter:t04",
        presentation_channel="meter",
    )

    feed = poll_frontend_feed(store, session["session_id"], owner_id=owner)
    assert len(feed["durable_events"]) >= 1
    event = feed["durable_events"][-1]
    assert event["durability"] == "durable"
    assert event["semantic_kind"] == "meter"
    assert event["sequence"] == feed["durable_watermark"]

    # 2. Rendering into text and HTML
    plain = render_event_to_plain_text(event)
    assert "Token usage summary" in plain

    html_out = render_event_to_html(event)
    assert "Token usage summary" in html_out

    # 3. Ephemeral event projection
    eph = project_ephemeral_event(
        session["session_id"],
        {"id": "act-1", "sequence": 1, "kind": "commentary", "text": "Drafting answer..."},
        epoch=2,
    )
    assert eph["durability"] == "ephemeral"
    assert eph["epoch"] == 2
    assert eph["ephemeral_sequence"] == 1
    assert eph["content_blocks"][0]["text"] == "Drafting answer..."


@pytest.mark.asyncio
async def test_t06_t07_t10_dispatcher_leases_multi_destinations_and_recovery(
    tmp_path: Path,
):
    runtime, store, owner, session = _setup_runtime(tmp_path)

    # Set up event with outbox task
    msg = store.append_presentation_message(
        session_id=session["session_id"],
        owner_id=owner,
        agent_id="test-agent",
        role="assistant",
        text="Multi-destination announcement",
        source="system",
        idempotency_key="msg:multi-dest",
        presentation_channel="status",
        outbox=True,
    )
    evt_id = msg["delivery_event_id"]

    # Configure custom reference adapters for primary and mirror
    adapter_primary = ReferenceConnectorAdapter(connector_id="reference", endpoint_id="reference:primary")
    adapter_mirror = ReferenceConnectorAdapter(connector_id="reference_mirror", endpoint_id="reference_mirror:ep1")

    dispatcher1 = FrontendDispatcher(
        store,
        worker_id="worker_alpha",
        adapters={"reference": adapter_primary},
    )

    # T06: Competing dispatcher cannot claim tasks held by worker_alpha
    claimed_alpha = store.claim_delivery_outbox(
        session_id=session["session_id"],
        owner_id=owner,
        worker_id="worker_alpha",
        event_id=evt_id,
        lease_seconds=60,
    )
    assert len(claimed_alpha) == 1

    claimed_beta = store.claim_delivery_outbox(
        session_id=session["session_id"],
        owner_id=owner,
        worker_id="worker_beta",
        event_id=evt_id,
        lease_seconds=60,
    )
    assert len(claimed_beta) == 0  # Beta cannot steal active lease

    # T06: Stale lease completion fails closed
    store.complete_delivery_outbox(
        outbox_id=claimed_alpha[0]["outbox_id"],
        lease_token=claimed_alpha[0]["lease_token"],
        status="completed",
    )
    # Stale attempt with old token raises SessionConflict
    with pytest.raises(Exception):
        store.complete_delivery_outbox(
            outbox_id=claimed_alpha[0]["outbox_id"],
            lease_token="stale_token",
            status="completed",
        )

    # T07 & T10: Transport outcomes: primary delivered, mirror timeout -> unknown
    receipt_p = await adapter_primary.dispatch({"event_id": evt_id, "content_blocks": [{"type": "text", "text": "Hi"}]})
    assert receipt_p["status"] == "delivered"
    assert receipt_p["proof"] is not None

    adapter_mirror.simulate_mode = "timeout"
    receipt_m = await adapter_mirror.dispatch({"event_id": evt_id, "content_blocks": [{"type": "text", "text": "Hi"}]})
    assert receipt_m["status"] == "unknown"
    assert receipt_m["proof"] is None

    # Record both receipts
    store.record_frontend_delivery_receipt(
        session_id=session["session_id"],
        owner_id=owner,
        receipt=receipt_p,
    )
    store.record_frontend_delivery_receipt(
        session_id=session["session_id"],
        owner_id=owner,
        receipt=receipt_m,
    )

    receipts = store.frontend_delivery_receipts(
        session_id=session["session_id"],
        owner_id=owner,
        event_id=evt_id,
    )
    statuses = {r["endpoint_id"]: r["status"] for r in receipts}
    assert statuses["reference:primary"] == "delivered"
    assert statuses["reference_mirror:ep1"] == "unknown"

    # Known delivered endpoint cannot be regressed by non-delivered status
    updated = store.record_frontend_delivery_receipt(
        session_id=session["session_id"],
        owner_id=owner,
        receipt={
            "type": "hashi.delivery-receipt",
            "version": 1,
            "event_id": evt_id,
            "endpoint_id": "reference:primary",
            "status": "unknown",
        },
    )
    assert updated["ignored_regression"] is True
    assert updated["status"] == "delivered"


@pytest.mark.asyncio
async def test_dispatcher_end_to_end_claim_and_dispatch(tmp_path: Path):
    runtime, store, owner, session = _setup_runtime(tmp_path)

    msg = store.append_presentation_message(
        session_id=session["session_id"],
        owner_id=owner,
        agent_id="test-agent",
        role="assistant",
        text="End to end outbox message",
        source="system",
        idempotency_key="msg:e2e-dispatch",
        presentation_channel="final",
        outbox=True,
    )
    evt_id = msg["delivery_event_id"]

    ref_adapter = ReferenceConnectorAdapter(connector_id="reference", endpoint_id="reference:primary")
    session_adapter = ReferenceConnectorAdapter(
        connector_id="session_api",
        endpoint_id="session_api:default",
    )
    dispatcher = FrontendDispatcher(
        store,
        worker_id="dispatcher_e2e",
        adapters={"reference": ref_adapter, "session_api": session_adapter},
    )

    # Dispatch pending outbox tasks
    dispatched = await dispatcher.dispatch_once(
        session_id=session["session_id"],
        owner_id=owner,
        event_id=evt_id,
    )
    assert len(dispatched) == 1
    assert dispatched[0]["status"] == "completed"
    assert len(session_adapter.received_events) == 1
    delivered_event = session_adapter.received_events[0]
    assert delivered_event["type"] == "hashi.frontend-event"
    assert delivered_event["interface_kind"] == "message"
    assert "lease_token" not in delivered_event
    assert delivered_event["content_blocks"][0]["text"] == "End to end outbox message"

    # Receipts recorded
    receipts = store.frontend_delivery_receipts(
        session_id=session["session_id"],
        owner_id=owner,
        event_id=evt_id,
    )
    assert len(receipts) >= 1
    assert any(r["status"] == "delivered" for r in receipts)

    # Second dispatch has nothing pending
    second = await dispatcher.dispatch_once(
        session_id=session["session_id"],
        owner_id=owner,
        event_id=evt_id,
    )
    assert len(second) == 0


@pytest.mark.asyncio
async def test_dispatcher_leaves_pull_tasks_for_feed_and_isolates_adapter_exceptions(
    tmp_path: Path,
):
    runtime, store, owner, session = _setup_runtime(tmp_path)

    pull_message = store.append_presentation_message(
        session_id=session["session_id"],
        owner_id=owner,
        agent_id="test-agent",
        role="assistant",
        text="Available in the durable feed",
        source="system",
        idempotency_key="msg:pull-feed-admission",
        presentation_channel="final",
        outbox=True,
    )
    pull_dispatcher = FrontendDispatcher(
        store,
        worker_id="dispatcher_pull_feed",
    )
    pull_result = await pull_dispatcher.dispatch_once(
        session_id=session["session_id"],
        owner_id=owner,
        event_id=pull_message["delivery_event_id"],
    )
    assert pull_result == []
    pull_receipts = store.frontend_delivery_receipts(
        session_id=session["session_id"],
        owner_id=owner,
        event_id=pull_message["delivery_event_id"],
    )
    assert pull_receipts == []

    class ExplodingAdapter:
        async def dispatch(self, _task, *, endpoint_id):
            raise RuntimeError(f"ambiguous transport failure for {endpoint_id}")

    ambiguous_message = store.append_presentation_message(
        session_id=session["session_id"],
        owner_id=owner,
        agent_id="test-agent",
        role="assistant",
        text="Ambiguous adapter outcome",
        source="system",
        idempotency_key="msg:ambiguous-adapter",
        presentation_channel="final",
        outbox=True,
    )
    ambiguous_dispatcher = FrontendDispatcher(
        store,
        worker_id="dispatcher_ambiguous",
        adapters={"session_api": ExplodingAdapter()},
    )
    ambiguous_result = await ambiguous_dispatcher.dispatch_once(
        session_id=session["session_id"],
        owner_id=owner,
        event_id=ambiguous_message["delivery_event_id"],
    )
    assert ambiguous_result[0]["status"] == "unknown"
    ambiguous_receipts = store.frontend_delivery_receipts(
        session_id=session["session_id"],
        owner_id=owner,
        event_id=ambiguous_message["delivery_event_id"],
    )
    assert ambiguous_receipts[0]["status"] == "unknown"
    assert ambiguous_receipts[0]["proof"] is None

def test_runtime_ingress_keeps_reply_reference_typed_in_request_and_session(tmp_path, monkeypatch):
    from orchestrator import frontend_ingress
    from orchestrator.frontend_ingress import accept_runtime_ingress
    from orchestrator.frontend_contracts import normalize_frontend_request

    runtime, store, owner, session = _setup_runtime(tmp_path)
    target = store.append_presentation_message(
        session_id=session["session_id"],
        owner_id=owner,
        agent_id="test-agent",
        role="assistant",
        text="Quoted answer from the same Session",
        source="test",
        idempotency_key="reply-target",
        outbox=True,
    )
    envelope = build_frontend_ingress_envelope(
        source_id="workbench",
        ingress_transport="session-api",
        surface="workbench",
        channel_key="default",
        instance_id="HASHI1",
        principal={"kind": "human_or_client", "assurance": "runtime_observed"},
        network_authentication="not_applicable",
        relay_chain=[],
        request_id="req-reply-reference",
        idempotency_key="key-reply-reference",
        session_id=session["session_id"],
        agent_id="test-agent",
    )
    observed = {}
    original_normalize = frontend_ingress.normalize_frontend_request

    def capture_request(value):
        observed["operation"] = value["operation"]
        return normalize_frontend_request(value)

    monkeypatch.setattr(frontend_ingress, "normalize_frontend_request", capture_request)
    metadata = {
        "session_id": session["session_id"],
        "reply_to_event_id": target["delivery_event_id"],
    }
    _session, accepted, _owner, _surface, _channel = accept_runtime_ingress(
        runtime,
        envelope,
        request_id="req-reply-reference",
        chat_id=None,
        prompt="What did I quote?",
        source="session-api",
        request_metadata=metadata,
        request_content=None,
        idempotency_key="key-reply-reference",
    )

    assert observed["operation"]["content"] == [
        {"type": "reply_ref", "event_id": target["delivery_event_id"]},
        {"type": "text", "text": "What did I quote?"},
    ]
    stored = store.get_message(
        accepted.message_id,
        session_id=session["session_id"],
        owner_id=owner,
    )
    assert stored["content"] == observed["operation"]["content"]
    assert stored["message_context"]["reply_reference"] == {
        "event_id": target["delivery_event_id"],
        "message_ref": target["delivery_event_id"],
        "session_id": session["session_id"],
        "context_generation": 1,
        "role": "assistant",
        "author": "test-agent",
        "timestamp": target["created_at"],
        "text": "Quoted answer from the same Session",
    }
    from orchestrator.chat_transcript_projection import _canonical_projection_row

    projected = _canonical_projection_row(store, stored, owner_id=owner)
    assert projected["reply_reference"] == stored["message_context"]["reply_reference"]
