from __future__ import annotations

import asyncio
import copy
import time
from types import SimpleNamespace

import pytest

from orchestrator.exchange_ingress import (
    ExchangeIngressConflict,
    ExchangeIngressPermissionError,
    ExchangeIngressService,
)
from orchestrator.hchat_origin_binding import bind_outbound_origin
from orchestrator.session_store import SessionStore
from remote.exchange_outbox import ExchangeOutbox
from remote.exchange_protocol import send_frame, utc_timestamp
from tests.test_exchange_ingress import _configure, _evidence, _reply_delivery


def _bound_fixture(tmp_path, *, target="planner@home.alice"):
    _configure(tmp_path)
    now = time.time()
    store = SessionStore(tmp_path / "state" / "sessions.sqlite3", instance_id="HASHI2")
    origin = store.create_session(owner_id="user:7", agent_id="reviewer")
    other = store.create_session(owner_id="user:7", agent_id="reviewer")
    store.create_session(owner_id="user:8", agent_id="reviewer")
    run = store.accept_run(
        session_id=origin["session_id"], owner_id="user:7", agent_id="reviewer",
        request_id="req-exchange-origin", text="send", source="test",
        idempotency_key="exchange-origin-run",
    )
    fence = store.mark_request_running(run.request_id, worker_id="test-worker")
    bind_outbound_origin(
        store, message_id="original_1", owner_id="user:7", agent_id="reviewer",
        session_id=origin["session_id"], run_id=run.run_id, request_id=run.request_id,
        context_generation=origin["context_generation"], fencing_token=fence, target=target,
    )
    store.bind_primary_session(owner_id="user:7", agent_id="reviewer", session_id=other["session_id"])
    delivery = _reply_delivery(now - 100)
    delivery["authorization_expires_at"] = utc_timestamp(now + 60)
    delivery["expires_at"] = utc_timestamp(now + 200)
    outbox = ExchangeOutbox(tmp_path / "state" / "exchange_outbox.sqlite3")
    outbox.put(send_frame(
        message_id="original_1", conversation_id=delivery["conversation_id"],
        from_agent="reviewer", to=delivery["sender"], created_at=utc_timestamp(now - 120),
        expires_at=utc_timestamp(now + 180), message_type="agent_message",
        in_reply_to=None, text="Original request.",
    ))
    outbox.set_state("original_1", "accepted")

    class Runtime:
        startup_success = True

        async def enqueue_api_text(self, *args, **kwargs):
            raise AssertionError("a terminal reply must not start a new Run")

    service = ExchangeIngressService(
        hashi_root=tmp_path, runtime_map=lambda: {"reviewer": Runtime()}, session_store=store,
    )
    return service, store, origin, other, delivery


def test_delayed_exchange_reply_reaches_original_session_once_without_confirmation(tmp_path):
    service, store, origin, other, delivery = _bound_fixture(tmp_path)
    prompt, evidence = _evidence(tmp_path, delivery)
    accepted = service.accept(evidence=evidence, prompt=prompt)
    displayed = asyncio.run(service.schedule(evidence=evidence, prompt=prompt))

    assert accepted.state == "accepted"
    assert displayed.state == "displayed"
    assert displayed.session_id == origin["session_id"]
    assert displayed.confirmation_id is None
    visible = store.recent_visible_messages(origin["session_id"], owner_id="user:7")
    assert len([x for x in visible if x["source"] == "hchat-exchange-reply"]) == 1
    assert visible[-1]["text"] == "Review complete."
    assert visible[-1]["message_context"]["message_age"]["reason"] == "stale"
    assert store.recent_visible_messages(other["session_id"], owner_id="user:7") == []

    retry = service.accept(evidence=evidence, prompt=prompt)
    assert retry.replayed
    assert asyncio.run(service.schedule(evidence=evidence, prompt=prompt)).state == "displayed"
    reopened = ExchangeIngressService(
        hashi_root=tmp_path, runtime_map=lambda: {"reviewer": SimpleNamespace(startup_success=True)},
        session_store=SessionStore(tmp_path / "state" / "sessions.sqlite3", instance_id="HASHI2"),
    )
    assert asyncio.run(reopened.schedule(evidence=evidence, prompt=prompt)).session_id == origin["session_id"]
    assert len([x for x in store.recent_visible_messages(origin["session_id"], owner_id="user:7")
                if x["source"] == "hchat-exchange-reply"]) == 1

    changed = copy.deepcopy(delivery)
    changed["content"]["text"] = "A different answer."
    changed_prompt, changed_evidence = _evidence(tmp_path, changed)
    with pytest.raises(ExchangeIngressConflict):
        service.accept(evidence=changed_evidence, prompt=changed_prompt)


def test_exchange_reply_cannot_use_a_bound_origin_for_another_target(tmp_path):
    service, store, origin, _other, delivery = _bound_fixture(tmp_path, target="planner@home.other")
    prompt, evidence = _evidence(tmp_path, delivery)
    with pytest.raises(ExchangeIngressPermissionError):
        service.accept(evidence=evidence, prompt=prompt)
    assert not any(x["source"] == "hchat-exchange-reply" for x in store.recent_visible_messages(
        origin["session_id"], owner_id="user:7"
    ))


@pytest.mark.asyncio
async def test_public_hchat_draft_records_its_source_session_before_sending(tmp_path):
    from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime
    from orchestrator.hchat_origin_binding import find_outbound_origin
    from tests.test_wrapper_commands import (
        HCHAT_TARGET_METADATA_KEY,
        _make_background_runtime,
        _queued_request_from,
    )

    runtime, _sent, _voices = _make_background_runtime(tmp_path)
    runtime.session_store = store = SessionStore(tmp_path / "state" / "sessions.sqlite3", instance_id="HASHI2")
    origin = store.create_session(owner_id="user:7", agent_id=runtime.name)
    run = store.accept_run(
        session_id=origin["session_id"], owner_id="user:7", agent_id=runtime.name,
        request_id="req-001", text="send", source="bridge:hchat-draft",
        idempotency_key="public-draft-run",
    )
    fence = store.mark_request_running(run.request_id, worker_id="test-worker")
    item = _queued_request_from("bridge:hchat-draft")
    item.request_metadata = {HCHAT_TARGET_METADATA_KEY: "planner@home.alice"}
    item.owner_id, item.session_id, item.run_id, item.fencing_token = (
        "user:7", origin["session_id"], run.run_id, fence,
    )
    sent = []

    def sender(target, from_agent, text, **kwargs):
        recorded = find_outbound_origin(store, kwargs["message_id"])
        assert recorded["session_id"] == origin["session_id"]
        assert recorded["run_id"] == run.run_id
        assert recorded["target"] == target
        sent.append((target, kwargs["message_id"]))
        return True

    runtime._hchat_draft_sender = sender
    result = await FlexibleAgentRuntime._prepare_hchat_draft_success(
        runtime, item, core_raw="Please inspect.", completion_path="foreground",
    )
    assert result.visible_text.startswith("🟡 Hchat queued")
    assert len(sent) == 1



def test_exchange_reply_recovers_after_presentation_before_receipt(tmp_path, monkeypatch):
    service, store, origin, _other, delivery = _bound_fixture(tmp_path)
    prompt, evidence = _evidence(tmp_path, delivery)
    accepted = service.accept(evidence=evidence, prompt=prompt)
    original_mark = service.inbox.mark_displayed
    calls = 0

    def fail_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("synthetic receipt write interruption")
        return original_mark(*args, **kwargs)

    monkeypatch.setattr(service.inbox, "mark_displayed", fail_once)
    with pytest.raises(OSError, match="synthetic receipt"):
        asyncio.run(service.schedule(evidence=evidence, prompt=prompt))
    assert service.inbox.get(accepted.inbox_key).state == "accepted"
    displayed = asyncio.run(service.schedule(evidence=evidence, prompt=prompt))
    assert displayed.state == "displayed"
    assert displayed.session_id == origin["session_id"]
    visible = store.recent_visible_messages(origin["session_id"], owner_id="user:7")
    assert len([x for x in visible if x["source"] == "hchat-exchange-reply"]) == 1


def test_exchange_reply_cannot_fall_back_after_bound_conversation_is_archived(tmp_path):
    service, store, origin, other, delivery = _bound_fixture(tmp_path)
    prompt, evidence = _evidence(tmp_path, delivery)
    service.accept(evidence=evidence, prompt=prompt)
    store.archive_session(origin["session_id"], deleted=True)
    with pytest.raises(ExchangeIngressPermissionError):
        asyncio.run(service.schedule(evidence=evidence, prompt=prompt))
    assert not store.recent_visible_messages(other["session_id"], owner_id="user:7")
