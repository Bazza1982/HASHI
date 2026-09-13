from __future__ import annotations

import asyncio
import copy
import json
import time
from types import SimpleNamespace

import pytest

from orchestrator.exchange_ingress import (
    ExchangeInbox,
    ExchangeIngressAuthenticationError,
    ExchangeIngressConflict,
    ExchangeIngressPermissionError,
    ExchangeIngressService,
    build_exchange_ingress_claims,
    render_exchange_hchat_prompt,
)
from orchestrator.message_context import seal_connector_evidence
from orchestrator.session_store import SessionStore
from remote.exchange_outbox import ExchangeOutbox
from remote.exchange_protocol import send_frame, utc_timestamp


def _address(*, actor: str, instance: str, agent: str, address: str):
    return {
        "authority_id": "authority_1",
        "actor_id": actor,
        "registered_instance_id": instance,
        "agent_id": agent,
        "address": address,
    }


def _delivery(now: float | None = None):
    now = time.time() if now is None else now
    return {
        "v": 1,
        "type": "delivery",
        "delivery_id": "delivery_1",
        "recipient_epoch": "epoch_1",
        "sender": _address(
            actor="actor_alice",
            instance="instance_alice",
            agent="planner",
            address="planner@home.alice",
        ),
        "recipient": _address(
            actor="actor_barry",
            instance="instance_barry",
            agent="reviewer",
            address="reviewer@server.barry",
        ),
        "grant_revision": 7,
        "authorization_expires_at": utc_timestamp(now + 60),
        "message_id": "message_1",
        "conversation_id": "conversation_1",
        "created_at": utc_timestamp(now),
        "expires_at": utc_timestamp(now + 300),
        "message_type": "agent_message",
        "in_reply_to": None,
        "content": {
            "format": "hchat",
            "text": "Please review.",
            "authorization_message_id": None,
            "authorization_resources": [],
            "private_authorization_proofs": [],
        },
    }


def _reply_delivery(now: float | None = None):
    delivery = _delivery(now)
    delivery["message_id"] = "reply_1"
    delivery["message_type"] = "agent_reply"
    delivery["in_reply_to"] = "original_1"
    delivery["content"]["text"] = "Review complete."
    return delivery


def _record_original_outbound(root, delivery):
    now = time.time()
    outbox = ExchangeOutbox(root / "state" / "exchange_outbox.sqlite3")
    outbox.put(
        send_frame(
            message_id="original_1",
            conversation_id=delivery["conversation_id"],
            from_agent=delivery["recipient"]["agent_id"],
            to=delivery["sender"],
            created_at=utc_timestamp(now),
            expires_at=utc_timestamp(now + 300),
            message_type="agent_message",
            in_reply_to=None,
            text="Original request.",
        )
    )
    outbox.set_state("original_1", "accepted")


def _configure(root):
    (root / "agents.json").write_text(
        json.dumps(
            {
                "global": {"instance_id": "HASHI2"},
                "exchange": {
                    "enabled": True,
                    "authority_id": "authority_1",
                    "url": "wss://exchange.example.test/v1/connect",
                    "registered_instance_id": "instance_barry",
                    "instance_alias": "server",
                    "credential_ref": "exchange_token",
                    "published_agents": ["reviewer"],
                },
                "agents": [{"name": "reviewer", "is_active": True}],
            }
        ),
        encoding="utf-8",
    )
    (root / "secrets.json").write_text(
        json.dumps(
            {
                "hashi_remote_shared_token": "synthetic-local-shared-token",
                "exchange_token": "synthetic-exchange-token",
            }
        ),
        encoding="utf-8",
    )


def _evidence(root, delivery):
    welcome = {
        "epoch": delivery["recipient_epoch"],
        "actor_id": delivery["recipient"]["actor_id"],
        "instance_address": "server.barry",
    }
    claims = build_exchange_ingress_claims(
        delivery=delivery,
        welcome=welcome,
    )
    prompt = render_exchange_hchat_prompt(delivery)
    return prompt, seal_connector_evidence(
        root,
        claims=claims,
        prompt=prompt,
    )


def test_inbox_commit_is_persistent_and_idempotent(tmp_path):
    path = tmp_path / "state" / "exchange_inbox.sqlite3"
    delivery = _delivery(1_800_000_000)
    inbox = ExchangeInbox(path)

    accepted = inbox.accept(delivery)
    replay = ExchangeInbox(path).accept(delivery)

    assert accepted.state == "accepted"
    assert replay.inbox_key == accepted.inbox_key
    assert replay.replayed is True
    assert replay.delivery["content"]["text"] == "Please review."


def test_inbox_rejects_same_identity_with_different_payload(tmp_path):
    inbox = ExchangeInbox(tmp_path / "inbox.sqlite3")
    first = _delivery(1_800_000_000)
    conflict = copy.deepcopy(first)
    conflict["content"]["text"] = "Different request"

    inbox.accept(first)
    with pytest.raises(ExchangeIngressConflict):
        inbox.accept(conflict)


def test_inbox_claim_recovery_and_schedule_state(tmp_path):
    inbox = ExchangeInbox(tmp_path / "inbox.sqlite3")
    accepted = inbox.accept(_delivery(1_800_000_000))

    claimed = inbox.claim(accepted.inbox_key, now=100)
    assert claimed is not None and claimed.state == "scheduling"
    assert inbox.claim(accepted.inbox_key, now=101) is None
    reclaimed = inbox.claim(accepted.inbox_key, now=131)
    assert reclaimed is not None
    scheduled = inbox.mark_scheduled(
        accepted.inbox_key,
        request_id="request_1",
        run_id="run_1",
        session_id="session_1",
        message_id="local_message_1",
    )
    assert scheduled.state == "scheduled"
    assert scheduled.delivery["content"]["text"] == "Please review."
    inbox.redact_scheduled(accepted.inbox_key)
    assert ExchangeInbox(tmp_path / "inbox.sqlite3").get(
        accepted.inbox_key
    ).delivery == {}
    assert inbox.claim(accepted.inbox_key, now=200) is None


def test_inbox_purges_only_old_scheduled_markers(tmp_path):
    inbox = ExchangeInbox(tmp_path / "inbox.sqlite3")
    scheduled = inbox.accept(_delivery(1_800_000_000))
    pending = copy.deepcopy(_delivery(1_800_000_001))
    pending["message_id"] = "message_pending"
    pending["delivery_id"] = "delivery_pending"
    pending_record = inbox.accept(pending)
    inbox.mark_scheduled(
        scheduled.inbox_key,
        request_id="request_1",
        run_id="run_1",
        session_id="session_1",
        message_id="local_message_1",
    )
    inbox.redact_scheduled(scheduled.inbox_key)

    assert inbox.purge(now=time.time() + 31 * 24 * 60 * 60) == 1
    assert inbox.get(scheduled.inbox_key) is None
    assert inbox.get(pending_record.inbox_key) is not None


def test_service_persists_before_scheduling_and_preserves_verified_identity(
    tmp_path,
):
    _configure(tmp_path)
    delivery = _delivery()
    prompt, evidence = _evidence(tmp_path, delivery)
    calls = []

    class Runtime:
        name = "reviewer"
        startup_success = True

        async def enqueue_api_text(self, text, **kwargs):
            calls.append((text, kwargs))
            return "request_1"

    runtime = Runtime()
    store = SessionStore(
        tmp_path / "state" / "sessions.sqlite3",
        instance_id="HASHI2",
    )
    service = ExchangeIngressService(
        hashi_root=tmp_path,
        runtime_map=lambda: {"reviewer": runtime},
        session_store=store,
    )
    assert service.inbox.path == store.db_path

    accepted = service.accept(evidence=evidence, prompt=prompt)
    assert accepted.state == "accepted"
    assert calls == []

    scheduled = asyncio.run(
        service.schedule(evidence=evidence, prompt=prompt)
    )
    assert scheduled.state == "scheduled"
    assert len(calls) == 1
    queued_prompt, kwargs = calls[0]
    assert queued_prompt == prompt
    assert kwargs["source"] == "hchat-exchange"
    assert kwargs["idempotency_key"] == accepted.idempotency_key
    assert (
        kwargs["request_metadata"]["owner_id"]
        == "exchange:authority_1:actor_alice"
    )


def test_ack_loss_redelivery_with_new_epoch_reuses_the_scheduled_run(tmp_path):
    _configure(tmp_path)
    first = _delivery()
    calls = []

    class Runtime:
        startup_success = True

        async def enqueue_api_text(self, text, **kwargs):
            calls.append((text, kwargs))
            return "request_1"

    service = ExchangeIngressService(
        hashi_root=tmp_path,
        runtime_map=lambda: {"reviewer": Runtime()},
        session_store=SessionStore(
            tmp_path / "state" / "sessions.sqlite3",
            instance_id="HASHI2",
        ),
    )
    prompt, evidence = _evidence(tmp_path, first)
    initial = service.accept(evidence=evidence, prompt=prompt)
    scheduled = asyncio.run(
        service.schedule(evidence=evidence, prompt=prompt)
    )

    redelivery = copy.deepcopy(first)
    redelivery["delivery_id"] = "delivery_after_reconnect"
    redelivery["recipient_epoch"] = "epoch_after_reconnect"
    replay_prompt, replay_evidence = _evidence(tmp_path, redelivery)
    replay = service.accept(
        evidence=replay_evidence,
        prompt=replay_prompt,
    )
    replay_scheduled = asyncio.run(
        service.schedule(
            evidence=replay_evidence,
            prompt=replay_prompt,
        )
    )

    assert replay.inbox_key == initial.inbox_key
    assert replay.replayed is True
    assert replay_scheduled.request_id == scheduled.request_id
    assert len(calls) == 1


def test_new_workbench_process_recovers_an_acked_but_unscheduled_accept(
    tmp_path,
):
    _configure(tmp_path)
    delivery = _delivery()
    prompt, evidence = _evidence(tmp_path, delivery)
    calls = []

    class Runtime:
        startup_success = True

        async def enqueue_api_text(self, text, **kwargs):
            calls.append((text, kwargs))
            return "request_recovered"

    store = SessionStore(
        tmp_path / "state" / "sessions.sqlite3",
        instance_id="HASHI2",
    )
    first_process = ExchangeIngressService(
        hashi_root=tmp_path,
        runtime_map=lambda: {"reviewer": Runtime()},
        session_store=store,
    )
    accepted = first_process.accept(evidence=evidence, prompt=prompt)
    assert calls == []

    value = json.loads((tmp_path / "agents.json").read_text(encoding="utf-8"))
    value["exchange"]["published_agents"] = []
    (tmp_path / "agents.json").write_text(
        json.dumps(value),
        encoding="utf-8",
    )
    restarted_process = ExchangeIngressService(
        hashi_root=tmp_path,
        runtime_map=lambda: {"reviewer": Runtime()},
        session_store=store,
    )

    recovered = asyncio.run(restarted_process.recover_pending())

    assert [record.inbox_key for record in recovered] == [accepted.inbox_key]
    assert recovered[0].state == "scheduled"
    assert len(calls) == 1


def test_recovery_failure_for_one_item_does_not_block_the_next(tmp_path):
    _configure(tmp_path)
    first = _delivery()
    second = copy.deepcopy(first)
    second["delivery_id"] = "delivery_2"
    second["message_id"] = "message_2"
    calls = []

    class Runtime:
        startup_success = True

        async def enqueue_api_text(self, text, **kwargs):
            calls.append((text, kwargs))
            if len(calls) == 1:
                raise RuntimeError("synthetic transient failure")
            return "request_recovered"

    runtime = Runtime()
    service = ExchangeIngressService(
        hashi_root=tmp_path,
        runtime_map=lambda: {"reviewer": runtime},
        session_store=SessionStore(
            tmp_path / "state" / "sessions.sqlite3",
            instance_id="HASHI2",
        ),
    )
    first_prompt, first_evidence = _evidence(tmp_path, first)
    second_prompt, second_evidence = _evidence(tmp_path, second)
    first_record = service.accept(
        evidence=first_evidence,
        prompt=first_prompt,
    )
    second_record = service.accept(
        evidence=second_evidence,
        prompt=second_prompt,
    )

    recovered = asyncio.run(service.recover_pending())

    assert len(calls) == 2
    assert [record.inbox_key for record in recovered] == [
        second_record.inbox_key
    ]
    assert service.inbox.get(first_record.inbox_key).state == "accepted"
    assert service.inbox.get(second_record.inbox_key).state == "scheduled"


def test_signed_sender_cannot_target_a_different_local_instance(tmp_path):
    _configure(tmp_path)
    delivery = _delivery()
    delivery["recipient"] = _address(
        actor="actor_barry",
        instance="instance_other",
        agent="reviewer",
        address="reviewer@server.barry",
    )
    prompt, evidence = _evidence(tmp_path, delivery)
    service = ExchangeIngressService(
        hashi_root=tmp_path,
        runtime_map=lambda: {"reviewer": SimpleNamespace(startup_success=True)},
        session_store=SessionStore(
            tmp_path / "state" / "sessions.sqlite3",
            instance_id="HASHI2",
        ),
    )

    with pytest.raises(ExchangeIngressPermissionError):
        service.accept(evidence=evidence, prompt=prompt)


def test_sender_tuple_cannot_be_changed_after_connector_signature(tmp_path):
    _configure(tmp_path)
    delivery = _delivery()
    prompt, evidence = _evidence(tmp_path, delivery)
    tampered = copy.deepcopy(evidence)
    tampered["claims"]["delivery"]["sender"]["actor_id"] = (
        "actor_mallory"
    )
    service = ExchangeIngressService(
        hashi_root=tmp_path,
        runtime_map=lambda: {
            "reviewer": SimpleNamespace(startup_success=True)
        },
        session_store=SessionStore(
            tmp_path / "state" / "sessions.sqlite3",
            instance_id="HASHI2",
        ),
    )

    with pytest.raises(ExchangeIngressAuthenticationError):
        service.accept(evidence=tampered, prompt=prompt)


def test_same_named_senders_from_different_actors_keep_separate_owners(
    tmp_path,
):
    _configure(tmp_path)
    alice = _delivery()
    bob = copy.deepcopy(alice)
    bob["delivery_id"] = "delivery_bob"
    bob["sender"] = _address(
        actor="actor_bob",
        instance="instance_bob",
        agent="planner",
        address="planner@home.bob",
    )
    calls = []

    class Runtime:
        startup_success = True

        async def enqueue_api_text(self, text, **kwargs):
            calls.append((text, kwargs))
            return f"request_{len(calls)}"

    service = ExchangeIngressService(
        hashi_root=tmp_path,
        runtime_map=lambda: {"reviewer": Runtime()},
        session_store=SessionStore(
            tmp_path / "state" / "sessions.sqlite3",
            instance_id="HASHI2",
        ),
    )
    for delivery in (alice, bob):
        prompt, evidence = _evidence(tmp_path, delivery)
        service.accept(evidence=evidence, prompt=prompt)
        asyncio.run(service.schedule(evidence=evidence, prompt=prompt))

    assert [call[1]["request_metadata"]["owner_id"] for call in calls] == [
        "exchange:authority_1:actor_alice",
        "exchange:authority_1:actor_bob",
    ]
    assert [
        call[1]["request_metadata"]["session_channel_key"]
        for call in calls
    ] == [
        "instance_alice:conversation_1",
        "instance_bob:conversation_1",
    ]


def test_unpublishing_agent_immediately_blocks_new_acceptance(tmp_path):
    _configure(tmp_path)
    delivery = _delivery()
    prompt, evidence = _evidence(tmp_path, delivery)
    service = ExchangeIngressService(
        hashi_root=tmp_path,
        runtime_map=lambda: {
            "reviewer": SimpleNamespace(startup_success=True)
        },
        session_store=SessionStore(
            tmp_path / "state" / "sessions.sqlite3",
            instance_id="HASHI2",
        ),
    )
    value = json.loads((tmp_path / "agents.json").read_text(encoding="utf-8"))
    value["exchange"]["published_agents"] = []
    (tmp_path / "agents.json").write_text(
        json.dumps(value),
        encoding="utf-8",
    )

    with pytest.raises(ExchangeIngressPermissionError):
        service.accept(evidence=evidence, prompt=prompt)


def test_inbound_reply_must_match_the_original_outbound_tuple(tmp_path):
    _configure(tmp_path)
    delivery = _reply_delivery()
    _record_original_outbound(tmp_path, delivery)
    prompt, evidence = _evidence(tmp_path, delivery)
    service = ExchangeIngressService(
        hashi_root=tmp_path,
        runtime_map=lambda: {
            "reviewer": SimpleNamespace(startup_success=True)
        },
        session_store=SessionStore(
            tmp_path / "state" / "sessions.sqlite3",
            instance_id="HASHI2",
        ),
    )

    accepted = service.accept(evidence=evidence, prompt=prompt)

    assert accepted.state == "accepted"


def test_inbound_reply_without_local_request_is_rejected(tmp_path):
    _configure(tmp_path)
    delivery = _reply_delivery()
    prompt, evidence = _evidence(tmp_path, delivery)
    service = ExchangeIngressService(
        hashi_root=tmp_path,
        runtime_map=lambda: {
            "reviewer": SimpleNamespace(startup_success=True)
        },
        session_store=SessionStore(
            tmp_path / "state" / "sessions.sqlite3",
            instance_id="HASHI2",
        ),
    )

    with pytest.raises(ExchangeIngressPermissionError):
        service.accept(evidence=evidence, prompt=prompt)


def test_inbound_reply_to_only_prepared_request_is_rejected(tmp_path):
    _configure(tmp_path)
    delivery = _reply_delivery()
    now = time.time()
    ExchangeOutbox(tmp_path / "state" / "exchange_outbox.sqlite3").put(
        send_frame(
            message_id="original_1",
            conversation_id=delivery["conversation_id"],
            from_agent=delivery["recipient"]["agent_id"],
            to=delivery["sender"],
            created_at=utc_timestamp(now),
            expires_at=utc_timestamp(now + 300),
            message_type="agent_message",
            in_reply_to=None,
            text="Never accepted by Exchange.",
        )
    )
    prompt, evidence = _evidence(tmp_path, delivery)
    service = ExchangeIngressService(
        hashi_root=tmp_path,
        runtime_map=lambda: {
            "reviewer": SimpleNamespace(startup_success=True)
        },
        session_store=SessionStore(
            tmp_path / "state" / "sessions.sqlite3",
            instance_id="HASHI2",
        ),
    )

    with pytest.raises(ExchangeIngressPermissionError):
        service.accept(evidence=evidence, prompt=prompt)


def test_inbound_reply_cannot_swap_the_original_conversation(tmp_path):
    _configure(tmp_path)
    delivery = _reply_delivery()
    _record_original_outbound(tmp_path, delivery)
    delivery["conversation_id"] = "different_conversation"
    prompt, evidence = _evidence(tmp_path, delivery)
    service = ExchangeIngressService(
        hashi_root=tmp_path,
        runtime_map=lambda: {
            "reviewer": SimpleNamespace(startup_success=True)
        },
        session_store=SessionStore(
            tmp_path / "state" / "sessions.sqlite3",
            instance_id="HASHI2",
        ),
    )

    with pytest.raises(ExchangeIngressPermissionError):
        service.accept(evidence=evidence, prompt=prompt)
