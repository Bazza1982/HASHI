"""Real sender -> PAO receipt facts without contacting other Agents."""
from __future__ import annotations

import errno
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from urllib.error import URLError

import pytest

from orchestrator.hchat_delivery import HChatDraft, deliver_hchat_draft, hchat_delivery_receipt_text
from tools import hchat_send
from tests.test_hchat_send import _local_cfg


@pytest.fixture
def local_sender(monkeypatch):
    cfg = _local_cfg()
    monkeypatch.setattr(hchat_send, "_load_config", lambda: cfg)
    monkeypatch.setattr(hchat_send, "_build_reply_route", lambda *_: None)
    monkeypatch.setattr(hchat_send, "_register_hchat_outbound_correlation", lambda *a, **k: True)
    monkeypatch.setattr(hchat_send, "_local_workbench_hosts", lambda *_: ["127.0.0.1", "localhost"])
    monkeypatch.setattr(hchat_send, "_probe_workbench_health", lambda *a: True)
    monkeypatch.setattr(hchat_send, "_workbench_supports_message_freshness", lambda *a: True)
    monkeypatch.setattr("orchestrator.message_context.seal_connector_evidence", lambda *a, **k: None)
    return cfg


def test_policy_rejection_survives_default_sender_false_and_receipt(local_sender, monkeypatch):
    monkeypatch.setattr(hchat_send, "_hchat_channel_egress_allowed", lambda *a, **k: False)
    result = deliver_hchat_draft(HChatDraft("akane", "one draft"), from_agent="zelda")
    assert not result.success and result.failure.reason == "channel_blocked"
    assert result.accepted is False and result.delivery_status == "failed"
    text = hchat_delivery_receipt_text(result, locale="en")
    assert result.failure.reference in text and "returned false" not in text


@pytest.mark.parametrize("state,expected", [("queued", "queued"), ("awaiting_confirmation", "awaiting_confirmation"),
    ("confirmed", "confirmed"), ("discarded", "discarded"), ("expired", "expired"), ("superseded", "superseded")])
def test_receiver_state_is_not_flattened_to_queued(local_sender, monkeypatch, state, expected):
    class Response:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return json.dumps({"ok": True, "state": state}).encode()
    monkeypatch.setattr(hchat_send.urllib_request, "urlopen", lambda *a, **k: Response())
    result = deliver_hchat_draft(HChatDraft("akane", "one draft"), from_agent="zelda")
    assert result.delivery_status == expected
    text = hchat_delivery_receipt_text(result, locale="en")
    if state != "queued":
        assert "queued for" not in text
    if state in {"discarded", "expired", "superseded"}:
        assert result.accepted is False and result.success is True


@pytest.mark.parametrize("cause,reason,attempts,accepted", [
    (ConnectionRefusedError(errno.ECONNREFUSED, "PRIVATE"), "connection_refused", 2, False),
    (TimeoutError("PRIVATE"), "timed_out", 1, None)])
def test_false_transport_cause_is_kept_and_unknown_submission_is_not_replayed(local_sender, monkeypatch, cause, reason, attempts, accepted):
    calls = []
    def unavailable(*args, **kwargs):
        calls.append(args)
        raise URLError(cause)
    monkeypatch.setattr(hchat_send.urllib_request, "urlopen", unavailable)
    result = deliver_hchat_draft(HChatDraft("akane", "one draft"), from_agent="zelda")
    assert result.failure.reason == reason and result.accepted is accepted
    assert len(calls) == attempts
    assert "PRIVATE" not in hchat_delivery_receipt_text(result, locale="en")
    assert result.retry_count == 0


def test_legacy_false_without_a_qualified_receipt_cannot_prove_nonacceptance():
    result = deliver_hchat_draft(HChatDraft("akane", "one draft"), from_agent="zelda", sender=lambda *a, **k: False)
    assert result.accepted is None and result.delivery_status == "unconfirmed"
    assert "returned false" not in hchat_delivery_receipt_text(result, locale="en")


def test_concurrent_per_call_results_never_share_failure_details(local_sender, monkeypatch):
    from orchestrator.hchat_submit_result import record_hchat_failure
    barrier = Barrier(2)
    def failing(cfg, port, target, *args, **kwargs):
        barrier.wait(timeout=5)
        return record_hchat_failure(PermissionError("PRIVATE") if target == "akane" else TimeoutError("PRIVATE"),
            phase="read", accepted=False)
    monkeypatch.setattr(hchat_send, "_is_local_agent", lambda *a: True)
    monkeypatch.setattr(hchat_send, "_send_via_local_workbench", failing)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda target: deliver_hchat_draft(HChatDraft(target, "one draft"), from_agent="zelda"), ["akane", "other"]))
    assert [result.failure.reason for result in results] == ["access_denied", "timed_out"]
    assert results[0].failure.reference != results[1].failure.reference


def test_group_receipt_reports_partial_acceptance_instead_of_all_queued(local_sender, monkeypatch):
    from orchestrator.hchat_submit_result import record_hchat_failure, record_hchat_response
    local_sender["groups"] = {"qa": {"members": ["akane", "other"]}}
    monkeypatch.setattr(hchat_send, "_is_local_agent", lambda *a: True)
    def send(cfg, port, target, *a, **k):
        if target == "akane":
            record_hchat_response({"ok": True, "state": "awaiting_confirmation"})
            return True
        return record_hchat_failure(PermissionError("PRIVATE"), phase="validate")
    monkeypatch.setattr(hchat_send, "_send_via_local_workbench", send)
    result = deliver_hchat_draft(HChatDraft("@qa", "one draft"), from_agent="zelda")
    assert result.delivery_status == "group_result"
    assert [item.status for item in result.recipient_results] == ["awaiting_confirmation", "failed"]
    text = hchat_delivery_receipt_text(result, locale="en")
    assert "2 recipients; 1 accepted" in text and "1 rejected" in text
    assert "queued for" not in text and "PRIVATE" not in text


@pytest.mark.parametrize("state", ["queued", "awaiting_confirmation", "discarded", "auth_failed"])
def test_protocol_receipt_survives_post_acceptance_bookkeeping_failure(local_sender, monkeypatch, state):
    from tools import protocol_send
    monkeypatch.setattr(hchat_send, "_shared_token_for_protocol", lambda: "fixture-only")
    monkeypatch.setattr(protocol_send, "_load_config", lambda: local_sender)
    monkeypatch.setattr(protocol_send, "_find_remote_instance", lambda *a, **k: {"host": "127.0.0.1", "remote_port": 18809})
    monkeypatch.setattr(protocol_send, "_load_instances", lambda: {})
    monkeypatch.setattr(protocol_send, "_local_sidecar_base_url", lambda *a: None)
    monkeypatch.setattr(protocol_send, "_probe_remote_http", lambda *a: True)
    monkeypatch.setattr(protocol_send, "fetch_remote_protocol_capabilities", lambda *a, **k:
        ({"message_freshness_v1", "hchat_delivery_status_v1"}, None))
    def track(payload, *, state, **kwargs):
        if state != "sending":
            raise PermissionError("PRIVATE bookkeeping")
    monkeypatch.setattr(protocol_send, "_record_outbound_correlation", track)
    monkeypatch.setattr(protocol_send, "_request_json", lambda *a, **k:
        {"ok": False, "code": "auth_failed", "accepted": False} if state == "auth_failed" else {"ok": True, "state": state})
    def no_fallback():
        raise AssertionError("A known receiver result must not enter another route")
    monkeypatch.setattr(hchat_send, "_load_instances", no_fallback)
    result = deliver_hchat_draft(HChatDraft("akane@HASHI2", "one draft"), from_agent="zelda")
    if state == "auth_failed":
        assert result.success is False and result.delivery_status == "failed" and result.accepted is False
        assert result.failure.reason == "authentication_rejected"
    else:
        assert result.success is True and result.delivery_status == state
        assert result.accepted is (state != "discarded")
        assert result.failure.phase == "persist" and result.failure.reason == "access_denied"
    assert "PRIVATE" not in hchat_delivery_receipt_text(result, locale="en")
