from __future__ import annotations

import copy
import time

import pytest

from remote.exchange_outbox import ExchangeOutbox, ExchangeOutboxConflict
from remote.exchange_protocol import send_frame, utc_timestamp


def _target():
    return {
        "authority_id": "authority_1",
        "actor_id": "actor_alice",
        "registered_instance_id": "instance_alice",
        "agent_id": "planner",
        "address": "planner@home.alice",
    }


def _frame(now: float):
    return send_frame(
        message_id="message_1",
        conversation_id="conversation_1",
        from_agent="reviewer",
        to=_target(),
        created_at=utc_timestamp(now),
        expires_at=utc_timestamp(now + 300),
        message_type="agent_message",
        in_reply_to=None,
        text="Please review.",
    )


def test_outbox_keeps_exact_retry_and_minimal_reply_correlation(tmp_path):
    now = time.time()
    outbox = ExchangeOutbox(tmp_path / "exchange_outbox.sqlite3")

    first = outbox.put(_frame(now))
    replay = outbox.put(_frame(now))
    correlation = outbox.get_correlation("message_1", now=now + 600)

    assert replay.replayed is True
    assert replay.frame == first.frame
    assert correlation is not None
    assert correlation.from_agent == "reviewer"
    assert correlation.conversation_id == "conversation_1"
    assert correlation.recipient == _target()
    assert correlation.message_type == "agent_message"
    assert correlation.delivery_state == "prepared"
    assert correlation.accepted_at is None

    outbox.set_state("message_1", "accepted")
    correlation = outbox.get_correlation("message_1", now=now + 600)
    assert correlation.delivery_state == "accepted"
    assert correlation.accepted_at is not None

    assert outbox.purge(now=now + 601, grace_seconds=0) == 1
    assert outbox.get("message_1") is None
    assert outbox.get_correlation("message_1", now=now + 601) is not None


def test_outbox_rejects_same_message_id_with_changed_payload(tmp_path):
    now = time.time()
    outbox = ExchangeOutbox(tmp_path / "exchange_outbox.sqlite3")
    frame = _frame(now)
    changed = copy.deepcopy(frame)
    changed["content"]["text"] = "Different request."

    outbox.put(frame)

    with pytest.raises(ExchangeOutboxConflict):
        outbox.put(changed)


def test_correlation_rejects_id_reuse_after_retry_frame_is_purged(tmp_path):
    now = time.time()
    outbox = ExchangeOutbox(tmp_path / "exchange_outbox.sqlite3")
    original = _frame(now)
    outbox.put(original)

    assert outbox.purge(now=now + 7200, grace_seconds=0) == 1
    assert outbox.get(original["message_id"]) is None
    assert outbox.get_correlation(original["message_id"], now=now + 7200)

    changed = copy.deepcopy(original)
    changed["content"]["text"] = "Different request after spool purge."
    with pytest.raises(ExchangeOutboxConflict):
        outbox.put(changed)


def test_terminal_receipt_state_cannot_regress_or_reauthorize_reply(tmp_path):
    now = time.time()
    outbox = ExchangeOutbox(tmp_path / "exchange_outbox.sqlite3")
    outbox.put(_frame(now))
    outbox.set_state("message_1", "accepted")
    outbox.set_state("message_1", "rejected", code="PERMISSION_DENIED")

    record = outbox.set_state("message_1", "accepted")
    correlation = outbox.get_correlation("message_1", now=now + 1)

    assert record.state == "rejected"
    assert record.code == "PERMISSION_DENIED"
    assert correlation.delivery_state == "rejected"
