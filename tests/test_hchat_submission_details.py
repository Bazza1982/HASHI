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
    assert "akane@HASHI1" in text and "other@HASHI1" in text
    assert "waiting for confirmation" in text and "delivery failed" in text
    assert "PRIVATE" not in text
    from orchestrator.hchat_delivery import hchat_delivery_log_fields
    details = hchat_delivery_log_fields(result)["hchat_recipients"]
    assert [row["recipient"] for row in details] == ["akane@HASHI1", "other@HASHI1"]
    assert [row["status"] for row in details] == ["awaiting_confirmation", "failed"]
    assert all(row["message_id"] for row in details)


@pytest.mark.parametrize("state", ["queued", "awaiting_confirmation", "discarded", "auth_failed"])
def test_protocol_receipt_survives_post_acceptance_bookkeeping_failure(local_sender, monkeypatch, state):
    from tools import protocol_send
    monkeypatch.setattr(protocol_send, "_authenticated_peer_capabilities", lambda *a, **k: (set(), None))
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


def test_optional_hchat_feature_flags_do_not_block_ordinary_text(local_sender, monkeypatch):
    payloads = []

    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self): return json.dumps({"ok": True, "state": "queued"}).encode()

    monkeypatch.setattr(hchat_send, "_read_http_json", lambda *args, **kwargs: None)

    def post(request, **kwargs):
        payloads.append(json.loads(request.data.decode("utf-8")))
        return Response()

    monkeypatch.setattr(hchat_send.urllib_request, "urlopen", post)
    result = deliver_hchat_draft(HChatDraft("akane", "ordinary text"), from_agent="zelda")
    assert result.success is True and result.delivery_status == "queued"
    assert len(payloads) == 1
    assert payloads[0]["idempotency_key"] == f"hchat:{payloads[0]['message_id']}"
    assert payloads[0]["hchat_context"]["to_agent"] == "akane"


def test_legacy_remote_text_does_not_require_optional_capability_probe(monkeypatch):
    from orchestrator.hchat_submit_result import capture_hchat_submission
    sent = []

    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self): return json.dumps({"ok": True, "state": "queued"}).encode()

    monkeypatch.setattr(hchat_send, "_probe_remote", lambda *args: "http")
    monkeypatch.setattr(hchat_send, "_read_http_json", lambda *args, **kwargs: None)
    monkeypatch.setattr(hchat_send.urllib_request, "urlopen",
                        lambda request, **kwargs: sent.append(request) or Response())
    result = capture_hchat_submission(
        hchat_send._send_via_remote, "127.0.0.1", 18809, "akane", "zelda",
        "ordinary text", "HASHI1", to_instance="HASHI2", endpoint_instance="HASHI2",
        message_id="msg-fixed", idempotency_key="hchat:msg-fixed",
    )
    assert result.handled is True and result.status == "queued"
    assert len(sent) == 1 and sent[0].full_url.endswith("/hchat")
    payload = json.loads(sent[0].data.decode("utf-8"))
    assert payload["to_instance"] == "HASHI2"
    assert payload["message_id"] == "msg-fixed"
    assert payload["idempotency_key"] == "hchat:msg-fixed"


def test_exchange_relay_timeout_is_unknown_and_never_falls_back(monkeypatch):
    from orchestrator.hchat_submit_result import capture_hchat_submission
    fallbacks = []
    monkeypatch.setattr(hchat_send, "_probe_workbench_health", lambda *args: True)
    monkeypatch.setattr(hchat_send.urllib_request, "urlopen",
                        lambda *args, **kwargs: (_ for _ in ()).throw(TimeoutError("fixture")))
    monkeypatch.setattr(hchat_send, "_send_via_remote",
                        lambda *args, **kwargs: fallbacks.append(args) or True)
    result = capture_hchat_submission(
        hchat_send._send_via_exchange,
        {"instance_id": "RELAY", "host": "127.0.0.1", "workbench_port": 18800, "remote_port": 18809},
        "akane", "HASHI2", "zelda", "ordinary text", "HASHI1", {},
        message_id="msg-fixed", idempotency_key="hchat:msg-fixed",
    )
    assert result.handled is False and result.accepted is None
    assert result.status == "unconfirmed"
    assert fallbacks == []


def test_exchange_relay_connection_refused_can_try_legacy_route(monkeypatch):
    from orchestrator.hchat_submit_result import capture_hchat_submission, record_hchat_response
    fallbacks = []
    monkeypatch.setattr(hchat_send, "_probe_workbench_health", lambda *args: True)
    monkeypatch.setattr(hchat_send.urllib_request, "urlopen",
                        lambda *args, **kwargs: (_ for _ in ()).throw(
                            URLError(ConnectionRefusedError(errno.ECONNREFUSED, "fixture"))))
    def fallback(*args, **kwargs):
        fallbacks.append(args)
        record_hchat_response({"ok": True, "state": "queued"})
        return True
    monkeypatch.setattr(hchat_send, "_send_via_remote", fallback)
    result = capture_hchat_submission(
        hchat_send._send_via_exchange,
        {"instance_id": "RELAY", "host": "127.0.0.1", "workbench_port": 18800, "remote_port": 18809},
        "akane", "HASHI2", "zelda", "ordinary text", "HASHI1", {},
        message_id="msg-fixed", idempotency_key="hchat:msg-fixed",
    )
    assert result.handled is True and result.status == "queued"
    assert len(fallbacks) == 1


def test_direct_exchange_relay_timeout_does_not_fallback(monkeypatch):
    fallbacks = []
    monkeypatch.setattr(hchat_send, "_probe_workbench_health", lambda *args: True)
    monkeypatch.setattr(hchat_send.urllib_request, "urlopen",
                        lambda *args, **kwargs: (_ for _ in ()).throw(TimeoutError("fixture")))
    monkeypatch.setattr(hchat_send, "_send_via_remote",
                        lambda *args, **kwargs: fallbacks.append(args) or True)
    result = hchat_send._send_via_exchange(
        {"instance_id": "RELAY", "host": "127.0.0.1", "workbench_port": 18800, "remote_port": 18809},
        "akane", "HASHI2", "zelda", "ordinary text", "HASHI1", {},
        message_id="msg-fixed", idempotency_key="hchat:msg-fixed",
    )
    assert result is False
    assert fallbacks == []


def test_cross_instance_group_tracks_exact_members_and_stable_retry_ids(local_sender, monkeypatch):
    from orchestrator.hchat_submit_result import record_hchat_failure, record_hchat_response

    local_sender["groups"] = {"reviewers": {"members": ["akane", "other"]}}
    calls = []
    def protocol(to_agent, target_instance, from_agent, text, **kwargs):
        calls.append((to_agent, target_instance, from_agent, text, dict(kwargs)))
        if to_agent == "other" and len(calls) == 2:
            return record_hchat_failure(reason="route_unavailable", phase="scope")
        record_hchat_response({"ok": True, "state": "accepted"})
        return True
    monkeypatch.setattr(hchat_send, "_send_via_protocol_transport", protocol)
    monkeypatch.setattr(hchat_send, "_hchat_channel_egress_allowed", lambda *a, **k: True)

    result = hchat_send.send_hchat_result(
        "@reviewers", "zelda", "please review", target_instance="HASHI9",
        message_id="group-request-1", originated_at="2026-11-17T22:00:00Z",
    )
    assert result.status == "group_result" and result.handled
    assert [row.recipient for row in result.recipients] == ["akane@HASHI9", "other@HASHI9"]
    assert [row.status for row in result.recipients] == ["queued", "failed"]
    assert [call[:4] for call in calls] == [
        ("akane", "HASHI9", "zelda", "please review"),
        ("other", "HASHI9", "zelda", "please review"),
    ]
    child_ids = [row.message_id for row in result.recipients]
    assert all(child_ids) and len(set(child_ids)) == 2
    assert [call[4]["message_id"] for call in calls] == child_ids
    assert [call[4]["idempotency_key"] for call in calls] == [f"hchat:{value}" for value in child_ids]

    # A selective retry uses the original failed recipient's identity.
    retry = hchat_send.send_hchat_result(
        "other@HASHI9", "zelda", "please review", message_id=child_ids[1],
        originated_at="2026-11-17T22:00:00Z",
    )
    assert retry.handled and calls[-1][0] == "other"
    assert calls[-1][4]["message_id"] == child_ids[1]
    assert len(calls) == 3


def test_mixed_group_deduplicates_scoped_members_and_keeps_source_identity(local_sender, monkeypatch):
    from orchestrator.hchat_submit_result import record_hchat_response

    local_sender["groups"] = {"reviewers": {
        "members": ["akane", "akane@HASHI1", "rain@HASHI9", "reviewer@server.alice"]
    }}
    calls = []
    def local(cfg, port, target, sender, text, source, route, **kwargs):
        calls.append((f"{target}@HASHI1", source, kwargs["message_id"]))
        record_hchat_response({"ok": True, "state": "accepted"})
        return True
    def protocol(target, instance, sender, text, **kwargs):
        calls.append((f"{target}@{instance}", "HASHI1", kwargs["message_id"]))
        record_hchat_response({"ok": True, "state": "accepted"})
        return True
    def exchange(cfg, **kwargs):
        calls.append((kwargs["destination"].canonical, kwargs["source_instance"], kwargs["message_id"]))
        record_hchat_response({"ok": True, "state": "accepted"})
        return True
    monkeypatch.setattr(hchat_send, "_send_via_local_workbench", local)
    monkeypatch.setattr(hchat_send, "_send_via_protocol_transport", protocol)
    monkeypatch.setattr(hchat_send, "_send_via_exchange_transport", exchange)
    monkeypatch.setattr(hchat_send, "_hchat_channel_egress_allowed", lambda *a, **k: True)

    result = hchat_send.send_hchat_result("@reviewers", "zelda", "same exact text", message_id="parent-1")
    assert result.handled
    assert [row.recipient for row in result.recipients] == [
        "akane@HASHI1", "rain@HASHI9", "reviewer@server.alice"
    ]
    assert [call[:2] for call in calls] == [
        ("akane@HASHI1", "HASHI1"), ("rain@HASHI9", "HASHI1"),
        ("reviewer@server.alice", "HASHI1")
    ]
    assert len({call[2] for call in calls}) == 3


def test_group_explicit_instance_conflict_is_not_silently_redirected(local_sender, monkeypatch):
    local_sender["groups"] = {"reviewers": {"members": ["akane", "rain@HASHI3"]}}
    calls = []
    monkeypatch.setattr(hchat_send, "_send_via_protocol_transport", lambda *a, **k: calls.append((a, k)) or True)
    result = hchat_send.send_hchat_result("@reviewers", "zelda", "hello", target_instance="HASHI9")
    assert not result.handled and result.accepted is False
    assert calls == []

def test_cross_instance_group_preserves_one_attachment_snapshot_per_recipient(local_sender, monkeypatch, tmp_path):
    from orchestrator.hchat_submit_result import record_hchat_response

    local_sender["groups"] = {"reviewers": {"members": ["rain@HASHI9", "other@HASHI9"]}}
    source = tmp_path / "report.txt"
    source.write_bytes(b"original selected bytes")
    seen = []
    def protocol(target, instance, sender, text, **kwargs):
        [attachment] = kwargs["attachments"]
        seen.append((target, attachment, attachment.read_bytes(), kwargs["message_id"]))
        if len(seen) == 1:
            source.write_bytes(b"changed while group is sending")
        record_hchat_response({"ok": True, "state": "accepted"})
        return True
    monkeypatch.setattr(hchat_send, "_send_via_protocol_transport", protocol)
    monkeypatch.setattr(hchat_send, "_hchat_channel_egress_allowed", lambda *a, **k: True)

    result = hchat_send.send_hchat_result(
        "@reviewers", "zelda", "review file", message_id="group-attachments",
        attachments=[source],
    )
    assert result.handled and len(seen) == 2
    assert [row[2] for row in seen] == [b"original selected bytes"] * 2
    assert len({row[3] for row in seen}) == 2
    assert all(not row[1].exists() for row in seen)
    assert source.read_bytes() == b"changed while group is sending"


def test_group_attachment_partial_result_never_sends_local_text_alone(local_sender, monkeypatch, tmp_path):
    from orchestrator.hchat_submit_result import record_hchat_response

    local_sender["groups"] = {"reviewers": {"members": ["akane", "rain@HASHI9"]}}
    source = tmp_path / "report.txt"
    source.write_bytes(b"selected content")
    calls = []
    def remote(target, instance, sender, text, **kwargs):
        calls.append((target, [path.read_bytes() for path in kwargs["attachments"]]))
        record_hchat_response({"ok": True, "state": "accepted"})
        return True
    monkeypatch.setattr(hchat_send, "_send_via_protocol_transport", remote)
    monkeypatch.setattr(hchat_send, "_hchat_channel_egress_allowed", lambda *a, **k: True)
    monkeypatch.setattr(hchat_send, "_send_via_local_workbench", lambda *a, **k: pytest.fail("attachment was silently stripped"))
    result = hchat_send.send_hchat_result("@reviewers", "zelda", "review", attachments=[source])
    assert calls == [("rain", [b"selected content"])]
    assert [(item.recipient, item.status) for item in result.recipients] == [
        ("akane@HASHI1", "failed"), ("rain@HASHI9", "queued")
    ]


def test_group_missing_attachment_does_not_dispatch_any_recipient(local_sender, monkeypatch, tmp_path):
    local_sender["groups"] = {"reviewers": {"members": ["rain@HASHI9"]}}
    calls = []
    monkeypatch.setattr(hchat_send, "_send_via_protocol_transport", lambda *a, **k: calls.append(a) or True)
    result = hchat_send.send_hchat_result("@reviewers", "zelda", "review", attachments=[tmp_path / "missing.txt"])
    assert not result.handled and result.accepted is False and calls == []
