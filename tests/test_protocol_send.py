from __future__ import annotations

import json

import pytest

from remote.security.shared_token import AUTH_SCHEME
from tools import protocol_send


@pytest.fixture(autouse=True)
def isolate_outbound_state(tmp_path, monkeypatch):
    monkeypatch.setattr(protocol_send.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(
        protocol_send,
        "fetch_remote_protocol_capabilities",
        lambda base_url, timeout=5: (
            {"message_freshness_v1", "hchat_delivery_status_v1", "message_attachments_v1"},
            None,
        ),
    )
    # Most legacy sender tests exercise transport after an already verified peer.
    # Dedicated tests below exercise the actual handshake and tampering.
    monkeypatch.setattr(
        protocol_send, "_authenticated_peer_capabilities",
        lambda base_url, **kwargs: protocol_send.fetch_remote_protocol_capabilities(base_url, timeout=kwargs["timeout"]),
    )


class _FakeResponse:
    def __init__(self, payload: dict):
        self._payload = payload

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False



def _signed_response(request, payload, *, token="shared-secret", target="HASHI9", status=200):
    from remote.security.shared_token import build_response_auth

    nonce = {key.lower(): value for key, value in request.header_items()}["x-hashi-nonce"]
    body = {**payload, "responder_instance": target, "response_status": status}
    body["response_auth"] = build_response_auth(
        shared_token=token, request_nonce=nonce, payload=body,
    )
    response = _FakeResponse(body)
    response.status = status
    return response


def test_protocol_cli_configures_console_encoding_before_delivery(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(
        protocol_send,
        "_configure_cli_console_encoding",
        lambda: calls.append("configure"),
    )
    monkeypatch.setattr(
        protocol_send,
        "send_protocol_message",
        lambda *args, **kwargs: calls.append("send") or True,
    )
    monkeypatch.setattr(
        protocol_send.sys,
        "argv",
        [
            "protocol_send.py",
            "--to",
            "akane@HASHI2",
            "--from",
            "zelda",
            "--text",
            "hello",
        ],
    )

    assert protocol_send.main() == 0
    assert calls == ["configure", "send"]


def test_send_protocol_message_uses_shared_token_for_plain_protocol_send(monkeypatch, capsys):
    captured: list[dict] = []

    def fake_urlopen(req, timeout=0):
        body = json.loads((req.data or b"{}").decode("utf-8"))
        captured.append(
            {
                "url": req.full_url,
                "headers": dict(req.header_items()),
                "body": body,
                "timeout": timeout,
            }
        )
        return _signed_response(req, {"ok": True, "state": "accepted"})

    monkeypatch.setattr(protocol_send.urllib_request, "urlopen", fake_urlopen)
    monkeypatch.setattr(protocol_send, "_authenticated_peer_capabilities", lambda *a, **k: (set(), None))
    monkeypatch.setattr(protocol_send, "_load_config", lambda: {"global": {"instance_id": "HASHI1"}})
    monkeypatch.setattr(protocol_send, "_load_instances", lambda: {})
    monkeypatch.setattr(
        protocol_send,
        "_find_remote_instance",
        lambda *args, **kwargs: {"remote_host": "10.0.0.9", "remote_port": 8766},
    )
    monkeypatch.setattr(protocol_send, "_probe_remote_http", lambda host, port: True)
    monkeypatch.setattr(
        protocol_send,
        "fetch_remote_protocol_capabilities",
        lambda base_url, timeout=5: pytest.fail("plain HChat must not require optional capability flags"),
    )

    ok = protocol_send.send_protocol_message(
        "lily@HASHI9",
        "zelda",
        "hello there",
        shared_token="shared-secret",
    )

    assert ok is True
    assert len(captured) == 1
    request = captured[0]
    headers = {key.lower(): value for key, value in request["headers"].items()}
    assert request["url"] == "http://10.0.0.9:8766/protocol/message"
    assert headers["x-hashi-auth-scheme"] == AUTH_SCHEME
    assert headers["x-hashi-from-instance"] == "HASHI1"
    assert request["body"]["body"]["text"] == "hello there"
    assert request["body"]["to_agent"] == "lily"
    assert request["body"]["idempotency_key"] == (
        f"hchat:{request['body']['message_id']}"
    )
    output = capsys.readouterr().out
    assert "Protocol message queued" in output
    assert "Message:\nhello there" in output


def test_protocol_confirmation_terminal_is_handled_without_transport_failover(
    monkeypatch, capsys
):
    monkeypatch.setattr(
        protocol_send.urllib_request,
        "urlopen",
        lambda req, timeout=0: _signed_response(
            req, {"ok": True, "state": "discarded"}
        ),
    )
    monkeypatch.setattr(
        protocol_send,
        "_load_config",
        lambda: {"global": {"instance_id": "HASHI1"}},
    )
    monkeypatch.setattr(protocol_send, "_load_instances", lambda: {})
    monkeypatch.setattr(
        protocol_send,
        "_find_remote_instance",
        lambda *args, **kwargs: {
            "remote_host": "10.0.0.9",
            "remote_port": 8766,
        },
    )
    monkeypatch.setattr(
        protocol_send, "_probe_remote_http", lambda host, port: True
    )

    handled = protocol_send.send_protocol_message(
        "lily@HASHI9",
        "zelda",
        "old instruction",
        shared_token="shared-secret",
    )

    assert handled is True
    captured = capsys.readouterr()
    assert "discarded without execution" in captured.err


def test_protocol_send_attaches_only_explicit_private_proofs(monkeypatch, capsys):
    from orchestrator import private_authorization

    captured = {}
    synthetic_secret = "synthetic-secret-must-not-be-on-wire"

    def fake_urlopen(req, timeout=0):
        del timeout
        captured.update(json.loads((req.data or b"{}").decode("utf-8")))
        return _signed_response(req, {"ok": True, "state": "accepted"}, token="network-secret", target="HASHI2")

    def build_proofs(_root, *, credential_ids, binding):
        assert credential_ids == ["finance", "marketing"]
        assert binding["resources"] == ["user:synthetic"]
        assert binding["content_sha256"] == (
            private_authorization.authorization_content_sha256(
                "synthetic report request"
            )
        )
        return [
            {
                "type": "hashi.private-authorization-proof",
                "version": 1,
                "credential_id": credential_id,
                "digest": "a" * 64,
            }
            for credential_id in credential_ids
        ]

    monkeypatch.setattr(protocol_send.urllib_request, "urlopen", fake_urlopen)
    monkeypatch.setattr(
        protocol_send,
        "_load_config",
        lambda: {"global": {"instance_id": "HASHI1"}},
    )
    monkeypatch.setattr(protocol_send, "_load_instances", lambda: {})
    monkeypatch.setattr(
        protocol_send,
        "_find_remote_instance",
        lambda *args, **kwargs: {"remote_host": "10.0.0.9", "remote_port": 8766},
    )
    monkeypatch.setattr(protocol_send, "_probe_remote_http", lambda host, port: True)
    monkeypatch.setattr(private_authorization, "build_configured_proofs", build_proofs)

    assert protocol_send.send_protocol_message(
        "sunny@HASHI2",
        "sender",
        "synthetic report request",
        shared_token="network-secret",
        private_credential_ids=["finance", "marketing"],
        authorization_resources=["user:synthetic"],
    )

    assert [
        item["credential_id"]
        for item in captured["private_authorization_proofs"]
    ] == ["finance", "marketing"]
    assert captured["authorization_resources"] == ["user:synthetic"]
    assert synthetic_secret not in json.dumps(captured)
    capsys.readouterr()


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        ({"ok": True, "state": "accepted"}, "queued"),
        ({"ok": True, "state": "delivered_to_local_queue"}, "queued"),
        ({"ok": True, "state": "reply_delivered_locally"}, "queued"),
        ({"ok": True, "state": "reply_sent"}, "queued"),
        ({"ok": True, "state": "completed"}, "queued"),
        (
            {"ok": True, "state": "awaiting_confirmation"},
            "pending_confirmation",
        ),
        ({"ok": True, "state": "confirming"}, "pending_confirmation"),
        ({"ok": True, "state": "discarded"}, "discarded"),
        (
            {"ok": True, "state": "confirmation_expired"},
            "confirmation_expired",
        ),
        (
            {"ok": True, "state": "confirmation_superseded"},
            "confirmation_superseded",
        ),
        ({"ok": False, "state": "reply_failed"}, "failed"),
        ({"ok": True, "state": "reply_failed"}, "failed"),
        ({"ok": True, "state": "timed_out"}, "failed"),
    ],
)
def test_protocol_delivery_disposition_keeps_protocol_lifecycle_success_queued(result, expected):
    assert protocol_send._delivery_disposition(result) == expected


@pytest.mark.parametrize("state", ["reply_sent", "completed"])
def test_protocol_lifecycle_terminal_receipt_stays_queued_and_shows_original_body(
    state, capsys
):
    protocol_send._print_delivery_receipt(
        result={"ok": True, "state": state},
        from_agent="rika",
        target="zelda@HASHI1",
        text="Done.\nEvidence retained.",
    )

    output = capsys.readouterr().out
    assert "Protocol message queued" in output
    assert "Protocol terminal message sent" not in output
    assert "Message:\nDone.\nEvidence retained." in output


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ("awaiting_confirmation", "waiting for confirmation"),
        ("discarded", "discarded without execution"),
        ("confirmation_expired", "confirmation expired"),
        ("confirmation_superseded", "superseded without execution"),
    ],
)
def test_protocol_confirmation_receipt_names_the_real_state(
    state, expected, capsys
):
    protocol_send._print_delivery_receipt(
        result={"ok": True, "state": state},
        from_agent="rika",
        target="zelda@HASHI1",
        text="Delayed instruction",
    )

    captured = capsys.readouterr()
    assert expected in (captured.out + captured.err)


def test_send_protocol_message_failure_state_overrides_ok(monkeypatch, tmp_path, capsys):
    def fake_urlopen(req, timeout=0):
        return _signed_response(
            req, {
                "ok": True,
                "state": "reply_failed",
                "error": "peer could not return the reply",
            }
        )

    monkeypatch.setattr(protocol_send.urllib_request, "urlopen", fake_urlopen)
    monkeypatch.setattr(
        protocol_send,
        "_load_config",
        lambda: {"global": {"instance_id": "HASHI1"}},
    )
    monkeypatch.setattr(protocol_send, "_load_instances", lambda: {})
    monkeypatch.setattr(
        protocol_send,
        "_find_remote_instance",
        lambda *args, **kwargs: {
            "remote_host": "10.0.0.9",
            "remote_port": 8766,
        },
    )
    monkeypatch.setattr(protocol_send, "_probe_remote_http", lambda host, port: True)
    monkeypatch.setattr(
        protocol_send,
        "fetch_remote_protocol_capabilities",
        lambda base_url, timeout=5: ({"message_freshness_v1", "hchat_delivery_status_v1", "message_attachments_v1"}, None),
    )

    ok = protocol_send.send_protocol_message(
        "lily@HASHI9",
        "zelda",
        "Please report the result.",
        shared_token="shared-secret",
    )

    assert ok is False
    captured = capsys.readouterr()
    assert "Protocol message failed" in captured.err
    assert "Message:\nPlease report the result." in captured.err
    assert "Protocol message queued" not in captured.out
    assert "Protocol terminal message sent" not in captured.out
    state_path = tmp_path / ".hashi-remote" / "protocol_outbound_hashi1.json"
    data = json.loads(state_path.read_text(encoding="utf-8"))
    [item] = list(data["messages"].values())
    assert item["state"] == "failed"


def test_send_protocol_message_records_outbound_correlation(monkeypatch, tmp_path):
    def fake_urlopen(req, timeout=0):
        return _signed_response(req, {"ok": True, "state": "delivered_to_local_queue"})

    monkeypatch.setattr(protocol_send.urllib_request, "urlopen", fake_urlopen)
    monkeypatch.setattr(protocol_send, "_load_config", lambda: {"global": {"instance_id": "HASHI1"}})
    monkeypatch.setattr(protocol_send, "_load_instances", lambda: {})
    monkeypatch.setattr(
        protocol_send,
        "_find_remote_instance",
        lambda *args, **kwargs: {"remote_host": "10.0.0.9", "remote_port": 8766},
    )
    monkeypatch.setattr(protocol_send, "_probe_remote_http", lambda host, port: True)
    monkeypatch.setattr(
        protocol_send,
        "fetch_remote_protocol_capabilities",
        lambda base_url, timeout=5: ({"message_freshness_v1", "hchat_delivery_status_v1", "message_attachments_v1"}, None),
    )

    assert protocol_send.send_protocol_message("lily@HASHI9", "zelda", "hello", shared_token="shared-secret")

    state_path = tmp_path / ".hashi-remote" / "protocol_outbound_hashi1.json"
    data = json.loads(state_path.read_text(encoding="utf-8"))
    [item] = list(data["messages"].values())
    assert item["from_instance"] == "HASHI1"
    assert item["from_agent"] == "zelda"
    assert item["to_instance"] == "HASHI9"
    assert item["to_agent"] == "lily"
    assert item["state"] == "delivered_to_local_queue"


def test_send_protocol_message_prefers_local_sidecar_outbound_registration(monkeypatch, tmp_path):
    captured: list[dict] = []

    def fake_urlopen(req, timeout=0):
        body = json.loads((req.data or b"{}").decode("utf-8"))
        captured.append({"url": req.full_url, "body": body, "timeout": timeout})
        if req.full_url.endswith("/protocol/outbound"):
            return _FakeResponse({"ok": True})
        if req.full_url.endswith("/protocol/message"):
            return _signed_response(req, {"ok": True, "state": "delivered_to_local_queue"})
        raise AssertionError(f"unexpected request url: {req.full_url}")

    monkeypatch.setattr(protocol_send.urllib_request, "urlopen", fake_urlopen)
    monkeypatch.setattr(protocol_send, "_load_config", lambda: {"global": {"instance_id": "HASHI1"}})
    monkeypatch.setattr(protocol_send, "_load_instances", lambda: {"hashi1": {"remote_port": 8766}})
    monkeypatch.setattr(
        protocol_send,
        "_find_remote_instance",
        lambda *args, **kwargs: {"remote_host": "10.0.0.9", "remote_port": 8767},
    )
    monkeypatch.setattr(protocol_send, "_probe_remote_http", lambda host, port: True)
    monkeypatch.setattr(
        protocol_send,
        "fetch_remote_protocol_capabilities",
        lambda base_url, timeout=5: ({"message_freshness_v1", "hchat_delivery_status_v1", "message_attachments_v1"}, None),
    )

    assert protocol_send.send_protocol_message("lily@HASHI9", "zelda", "hello", shared_token="shared-secret")

    assert [item["url"] for item in captured] == [
        "http://127.0.0.1:8766/protocol/outbound",
        "http://10.0.0.9:8767/protocol/message",
        "http://127.0.0.1:8766/protocol/outbound",
    ]
    assert captured[0]["body"]["state"] == "sending"
    assert captured[2]["body"]["state"] == "delivered_to_local_queue"
    assert not (tmp_path / ".hashi-remote" / "protocol_outbound_hashi1.json").exists()


def test_send_protocol_message_falls_back_to_file_when_local_sidecar_unavailable(monkeypatch, tmp_path):
    captured: list[str] = []

    def fake_urlopen(req, timeout=0):
        captured.append(req.full_url)
        if req.full_url.endswith("/protocol/outbound"):
            raise protocol_send.URLError("sidecar unavailable")
        if req.full_url.endswith("/protocol/message"):
            return _signed_response(req, {"ok": True, "state": "delivered_to_local_queue"})
        raise AssertionError(f"unexpected request url: {req.full_url}")

    monkeypatch.setattr(protocol_send.urllib_request, "urlopen", fake_urlopen)
    monkeypatch.setattr(protocol_send, "_load_config", lambda: {"global": {"instance_id": "HASHI1"}})
    monkeypatch.setattr(protocol_send, "_load_instances", lambda: {"hashi1": {"remote_port": 8766}})
    monkeypatch.setattr(
        protocol_send,
        "_find_remote_instance",
        lambda *args, **kwargs: {"remote_host": "10.0.0.9", "remote_port": 8767},
    )
    monkeypatch.setattr(protocol_send, "_probe_remote_http", lambda host, port: True)
    monkeypatch.setattr(
        protocol_send,
        "fetch_remote_protocol_capabilities",
        lambda base_url, timeout=5: ({"message_freshness_v1", "hchat_delivery_status_v1", "message_attachments_v1"}, None),
    )

    assert protocol_send.send_protocol_message("lily@HASHI9", "zelda", "hello", shared_token="shared-secret")

    assert captured == [
        "http://127.0.0.1:8766/protocol/outbound",
        "http://10.0.0.9:8767/protocol/message",
        "http://127.0.0.1:8766/protocol/outbound",
    ]
    state_path = tmp_path / ".hashi-remote" / "protocol_outbound_hashi1.json"
    data = json.loads(state_path.read_text(encoding="utf-8"))
    [item] = list(data["messages"].values())
    assert item["state"] == "delivered_to_local_queue"


def test_send_protocol_message_uploads_attachments_then_commits(monkeypatch, tmp_path, capsys):
    attachment = tmp_path / "report.txt"
    attachment.write_text("hello", encoding="utf-8")
    captured: list[dict] = []

    def fake_urlopen(req, timeout=0):
        body = json.loads((req.data or b"{}").decode("utf-8"))
        captured.append(
            {
                "url": req.full_url,
                "headers": dict(req.header_items()),
                "body": body,
                "timeout": timeout,
            }
        )
        if req.full_url.endswith("/attachments/upload"):
            return _FakeResponse(
                {
                    "ok": True,
                    "attachment": {
                        "pending_upload_id": "pu-1",
                        "size_bytes": 5,
                    },
                }
            )
        if req.full_url.endswith("/protocol/message-with-attachments"):
            return _signed_response(req, {"ok": True, "state": "delivered_to_local_queue"})
        raise AssertionError(f"unexpected request url: {req.full_url}")

    monkeypatch.setattr(protocol_send.urllib_request, "urlopen", fake_urlopen)
    monkeypatch.setattr(protocol_send, "_load_config", lambda: {"global": {"instance_id": "HASHI1"}})
    monkeypatch.setattr(protocol_send, "_load_instances", lambda: {})
    monkeypatch.setattr(
        protocol_send,
        "_find_remote_instance",
        lambda *args, **kwargs: {"remote_host": "10.0.0.9", "remote_port": 8766},
    )
    monkeypatch.setattr(protocol_send, "_probe_remote_http", lambda host, port: True)
    monkeypatch.setattr(
        protocol_send,
        "fetch_remote_protocol_capabilities",
        lambda base_url, timeout=5: ({"message_freshness_v1", "hchat_delivery_status_v1", "message_attachments_v1"}, None),
    )

    ok = protocol_send.send_protocol_message(
        "lily@HASHI9",
        "zelda",
        "please review",
        attachments=[attachment],
        shared_token="shared-secret",
    )

    assert ok is True
    assert [item["url"] for item in captured] == [
        "http://10.0.0.9:8766/attachments/upload",
        "http://10.0.0.9:8766/protocol/message-with-attachments",
    ]
    upload_body = captured[0]["body"]
    assert upload_body["filename"] == "report.txt"
    assert upload_body["from_instance"] == "HASHI1"
    commit_body = captured[1]["body"]
    assert commit_body["attachments"][0]["pending_upload_id"] == "pu-1"
    assert commit_body["attachments"][0]["filename"] == "report.txt"
    assert commit_body["body"]["text"] == "please review"
    assert "attachments: 1" in capsys.readouterr().out


def test_send_protocol_message_streams_v2_chunks_before_atomic_commit(
    monkeypatch, tmp_path, capsys
):
    attachment = tmp_path / "archive.bin"
    attachment.write_bytes(b"0123456789abcdefghij")
    captured: list[dict] = []

    def fake_urlopen(req, timeout=0):
        raw = req.data or b""
        captured.append(
            {
                "url": req.full_url,
                "headers": dict(req.header_items()),
                "raw": raw,
                "timeout": timeout,
            }
        )
        if req.full_url.endswith("/attachments/v2/begin"):
            return _FakeResponse(
                {
                    "ok": True,
                    "attachment": {"pending_upload_id": "pu-stream-1"},
                }
            )
        if "/attachments/v2/chunk/pu-stream-1?offset=" in req.full_url:
            return _FakeResponse({"ok": True, "received_bytes": len(raw)})
        if req.full_url.endswith("/attachments/v2/finish"):
            return _FakeResponse({"ok": True, "attachment": {"complete": True}})
        if req.full_url.endswith("/protocol/message-with-attachments"):
            return _signed_response(req, {"ok": True, "state": "delivered_to_local_queue"})
        raise AssertionError(f"unexpected request url: {req.full_url}")

    monkeypatch.setattr(protocol_send.urllib_request, "urlopen", fake_urlopen)
    monkeypatch.setattr(protocol_send, "STREAM_ATTACHMENT_CHUNK_BYTES", 7)
    monkeypatch.setattr(protocol_send, "_load_config", lambda: {"global": {"instance_id": "HASHI1"}})
    monkeypatch.setattr(protocol_send, "_load_instances", lambda: {})
    monkeypatch.setattr(
        protocol_send,
        "_find_remote_instance",
        lambda *args, **kwargs: {"remote_host": "10.0.0.9", "remote_port": 8766},
    )
    monkeypatch.setattr(protocol_send, "_probe_remote_http", lambda host, port: True)
    monkeypatch.setattr(
        protocol_send,
        "fetch_remote_protocol_capabilities",
        lambda base_url, timeout=5: ({"message_freshness_v1", "hchat_delivery_status_v1", "message_attachments_v2_streaming"}, None),
    )

    ok = protocol_send.send_protocol_message(
        "lily@HASHI9",
        "zelda",
        "stream this",
        attachments=[attachment],
        shared_token="shared-secret",
    )

    assert ok is True
    assert [item["url"] for item in captured] == [
        "http://10.0.0.9:8766/attachments/v2/begin",
        "http://10.0.0.9:8766/attachments/v2/chunk/pu-stream-1?offset=0",
        "http://10.0.0.9:8766/attachments/v2/chunk/pu-stream-1?offset=7",
        "http://10.0.0.9:8766/attachments/v2/chunk/pu-stream-1?offset=14",
        "http://10.0.0.9:8766/attachments/v2/finish",
        "http://10.0.0.9:8766/protocol/message-with-attachments",
    ]
    chunks = captured[1:4]
    assert b"".join(item["raw"] for item in chunks) == attachment.read_bytes()
    assert all(len(item["raw"]) <= 7 for item in chunks)
    assert all(item["headers"]["X-hashi-auth-scheme"] == AUTH_SCHEME for item in chunks)
    assert "attachments: 1" in capsys.readouterr().out


def test_send_protocol_message_cancels_staged_uploads_after_partial_failure(monkeypatch, tmp_path):
    first = tmp_path / "one.txt"
    second = tmp_path / "two.txt"
    first.write_text("one", encoding="utf-8")
    second.write_text("two", encoding="utf-8")
    captured: list[dict] = []

    def fake_urlopen(req, timeout=0):
        body = json.loads((req.data or b"{}").decode("utf-8"))
        captured.append({"url": req.full_url, "body": body})
        if req.full_url.endswith("/attachments/upload"):
            if body["filename"] == "one.txt":
                return _FakeResponse({"ok": True, "attachment": {"pending_upload_id": "pu-1", "size_bytes": 3}})
            return _FakeResponse({"ok": False, "error": "boom"})
        if req.full_url.endswith("/attachments/upload/cancel"):
            return _FakeResponse({"ok": True, "removed": 1})
        raise AssertionError(f"unexpected request url: {req.full_url}")

    monkeypatch.setattr(protocol_send.urllib_request, "urlopen", fake_urlopen)
    monkeypatch.setattr(protocol_send, "_load_config", lambda: {"global": {"instance_id": "HASHI1"}})
    monkeypatch.setattr(protocol_send, "_load_instances", lambda: {})
    monkeypatch.setattr(
        protocol_send,
        "_find_remote_instance",
        lambda *args, **kwargs: {"remote_host": "10.0.0.9", "remote_port": 8766},
    )
    monkeypatch.setattr(protocol_send, "_probe_remote_http", lambda host, port: True)
    monkeypatch.setattr(
        protocol_send,
        "fetch_remote_protocol_capabilities",
        lambda base_url, timeout=5: ({"message_freshness_v1", "hchat_delivery_status_v1", "message_attachments_v1"}, None),
    )

    ok = protocol_send.send_protocol_message(
        "lily@HASHI9",
        "zelda",
        "please review",
        attachments=[first, second],
        shared_token="shared-secret",
    )

    assert ok is False
    assert [item["url"] for item in captured] == [
        "http://10.0.0.9:8766/attachments/upload",
        "http://10.0.0.9:8766/attachments/upload",
        "http://10.0.0.9:8766/attachments/upload/cancel",
    ]
    assert captured[-1]["body"]["pending_upload_ids"] == ["pu-1"]


def test_send_protocol_message_cancels_staged_uploads_after_commit_failure(monkeypatch, tmp_path, capsys):
    attachment = tmp_path / "report.txt"
    attachment.write_text("hello", encoding="utf-8")
    captured: list[dict] = []

    def fake_urlopen(req, timeout=0):
        body = json.loads((req.data or b"{}").decode("utf-8"))
        captured.append({"url": req.full_url, "body": body})
        if req.full_url.endswith("/attachments/upload"):
            return _FakeResponse({"ok": True, "attachment": {"pending_upload_id": "pu-1", "size_bytes": 5}})
        if req.full_url.endswith("/protocol/message-with-attachments"):
            return _signed_response(req, {"ok": False, "error": "commit boom"})
        if req.full_url.endswith("/attachments/upload/cancel"):
            return _FakeResponse({"ok": False, "error": "cancel boom"})
        raise AssertionError(f"unexpected request url: {req.full_url}")

    monkeypatch.setattr(protocol_send.urllib_request, "urlopen", fake_urlopen)
    monkeypatch.setattr(protocol_send, "_load_config", lambda: {"global": {"instance_id": "HASHI1"}})
    monkeypatch.setattr(protocol_send, "_load_instances", lambda: {})
    monkeypatch.setattr(
        protocol_send,
        "_find_remote_instance",
        lambda *args, **kwargs: {"remote_host": "10.0.0.9", "remote_port": 8766},
    )
    monkeypatch.setattr(protocol_send, "_probe_remote_http", lambda host, port: True)
    monkeypatch.setattr(
        protocol_send,
        "fetch_remote_protocol_capabilities",
        lambda base_url, timeout=5: ({"message_freshness_v1", "hchat_delivery_status_v1", "message_attachments_v1"}, None),
    )

    ok = protocol_send.send_protocol_message(
        "lily@HASHI9",
        "zelda",
        "please review",
        attachments=[attachment],
        shared_token="shared-secret",
    )

    assert ok is False
    assert [item["url"] for item in captured] == [
        "http://10.0.0.9:8766/attachments/upload",
        "http://10.0.0.9:8766/protocol/message-with-attachments",
        "http://10.0.0.9:8766/attachments/upload/cancel",
    ]
    assert "Failed to cancel staged uploads on server" in capsys.readouterr().err


def test_send_protocol_message_attempts_attachment_route_when_flags_are_absent(monkeypatch, tmp_path, capsys):
    attachment = tmp_path / "report.txt"
    attachment.write_text("hello", encoding="utf-8")

    monkeypatch.setattr(protocol_send, "_load_config", lambda: {"global": {"instance_id": "HASHI1"}})
    monkeypatch.setattr(protocol_send, "_load_instances", lambda: {})
    monkeypatch.setattr(
        protocol_send,
        "_find_remote_instance",
        lambda *args, **kwargs: {"remote_host": "10.0.0.9", "remote_port": 8766},
    )
    monkeypatch.setattr(protocol_send, "_probe_remote_http", lambda host, port: True)
    monkeypatch.setattr(
        protocol_send,
        "fetch_remote_protocol_capabilities",
        lambda base_url, timeout=5: ({"message_freshness_v1", "hchat_delivery_status_v1", "protocol_message_v1"}, None),
    )

    calls = []
    def unavailable(req, timeout=0):
        calls.append(req.full_url)
        return _FakeResponse({"ok": False, "error": "actual upload endpoint unavailable"})
    monkeypatch.setattr(protocol_send.urllib_request, "urlopen", unavailable)

    ok = protocol_send.send_protocol_message(
        "lily@HASHI9",
        "zelda",
        "please review",
        attachments=[attachment],
        shared_token="shared-secret",
    )

    assert ok is False
    assert calls == ["http://10.0.0.9:8766/attachments/upload"]
    assert "actual upload endpoint unavailable" in capsys.readouterr().err


def test_send_protocol_message_prints_operator_delivery_result(monkeypatch, capsys):
    def fake_urlopen(req, timeout=0):
        return _signed_response(
            req, {
                "ok": False,
                "message_type": "error",
                "body": {
                    "code": "target_agent_not_found",
                    "message": "Target agent 'lily' not found",
                },
            }
        )

    monkeypatch.setattr(protocol_send.urllib_request, "urlopen", fake_urlopen)
    monkeypatch.setattr(protocol_send, "_load_config", lambda: {"global": {"instance_id": "HASHI1"}})
    monkeypatch.setattr(protocol_send, "_load_instances", lambda: {})
    monkeypatch.setattr(
        protocol_send,
        "_find_remote_instance",
        lambda *args, **kwargs: {"remote_host": "10.0.0.9", "remote_port": 8766},
    )
    monkeypatch.setattr(protocol_send, "_probe_remote_http", lambda host, port: True)

    ok = protocol_send.send_protocol_message(
        "lily@HASHI9",
        "zelda",
        "hello there",
        shared_token="shared-secret",
    )

    assert ok is False
    err = capsys.readouterr().err
    assert "Delivery result: target_not_found:" in err
    assert "Target agent 'lily' not found" in err
    assert "Message:\nhello there" in err


@pytest.mark.parametrize("mode", ["valid", "missing", "tampered", "wrong_target", "wrong_status", "wrong_nonce"])
def test_protocol_text_receipt_must_match_authenticated_peer_and_request(monkeypatch, mode):
    from remote.security.shared_token import build_response_auth

    class Response:
        status = 202
        def __init__(self, body): self.body = body
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self): return json.dumps(self.body).encode("utf-8")

    def respond(request, timeout=0):
        nonce = dict((key.lower(), value) for key, value in request.header_items())["x-hashi-nonce"]
        body = {
            "ok": True, "state": "accepted",
            "responder_instance": "HASHI8" if mode == "wrong_target" else "HASHI9",
            "response_status": 200 if mode == "wrong_status" else 202,
        }
        if mode != "missing":
            body["response_auth"] = build_response_auth(
                shared_token="shared-secret",
                request_nonce="other-nonce" if mode == "wrong_nonce" else nonce,
                payload=body,
            )
        if mode == "tampered":
            body["state"] = "delivered"
        return Response(body)

    monkeypatch.setattr(protocol_send.urllib_request, "urlopen", respond)
    args = dict(
        payload={"text": "fixture"}, shared_token="shared-secret",
        from_instance="HASHI1", expected_response_instance="HASHI9",
    )
    if mode == "valid":
        result = protocol_send._request_json("http://127.0.0.1:1234/protocol/message", **args)
        assert result["ok"] is True and result["responder_instance"] == "HASHI9"
    else:
        with pytest.raises(protocol_send.UnverifiedProtocolReceipt):
            protocol_send._request_json("http://127.0.0.1:1234/protocol/message", **args)

def test_unsigned_receipt_is_unconfirmed_and_never_replayed(monkeypatch, capsys):
    from orchestrator.hchat_submit_result import capture_hchat_submission

    requests = []
    def fake_urlopen(req, timeout=0):
        requests.append(req.full_url)
        return _FakeResponse({"ok": True, "state": "accepted"})

    monkeypatch.setattr(protocol_send.urllib_request, "urlopen", fake_urlopen)
    monkeypatch.setattr(protocol_send, "_load_config", lambda: {"global": {"instance_id": "HASHI1"}})
    monkeypatch.setattr(protocol_send, "_load_instances", lambda: {})
    monkeypatch.setattr(protocol_send, "_find_remote_instance", lambda *a, **k: {"remote_host": "127.0.0.1", "remote_port": 8766})
    monkeypatch.setattr(protocol_send, "_probe_remote_http", lambda *a: True)
    monkeypatch.setattr(protocol_send, "_local_sidecar_base_url", lambda *a: None)
    monkeypatch.setattr(protocol_send, "_record_outbound_correlation", lambda *a, **k: None)
    result = capture_hchat_submission(
        protocol_send.send_protocol_message, "lily@HASHI9", "zelda", "hello",
        shared_token="shared-secret",
    )

    assert result.handled is False
    assert result.status == "unconfirmed"
    assert result.accepted is None
    assert requests == ["http://127.0.0.1:8766/protocol/message"]
    captured = capsys.readouterr()
    assert "could not be verified" in captured.err
    assert "Protocol message queued" not in captured.out


def test_unsigned_attachment_commit_is_unconfirmed_and_never_cancelled_or_replayed(monkeypatch, tmp_path, capsys):
    from orchestrator.hchat_submit_result import capture_hchat_submission

    attachment = tmp_path / "report.txt"
    attachment.write_bytes(b"selected data")
    requests = []
    def fake_urlopen(req, timeout=0):
        requests.append(req.full_url)
        if req.full_url.endswith("/attachments/upload"):
            return _FakeResponse({"ok": True, "attachment": {"pending_upload_id": "pu-1", "size_bytes": 13}})
        if req.full_url.endswith("/protocol/message-with-attachments"):
            return _FakeResponse({"ok": True, "state": "accepted"})
        pytest.fail(f"unexpected extra operation: {req.full_url}")

    monkeypatch.setattr(protocol_send.urllib_request, "urlopen", fake_urlopen)
    monkeypatch.setattr(protocol_send, "_load_config", lambda: {"global": {"instance_id": "HASHI1"}})
    monkeypatch.setattr(protocol_send, "_load_instances", lambda: {})
    monkeypatch.setattr(protocol_send, "_find_remote_instance", lambda *a, **k: {"remote_host": "127.0.0.1", "remote_port": 8766})
    monkeypatch.setattr(protocol_send, "_probe_remote_http", lambda *a: True)
    monkeypatch.setattr(protocol_send, "_local_sidecar_base_url", lambda *a: None)
    monkeypatch.setattr(protocol_send, "_record_outbound_correlation", lambda *a, **k: None)
    result = capture_hchat_submission(
        protocol_send.send_protocol_message, "lily@HASHI9", "zelda", "hello",
        attachments=[attachment], shared_token="shared-secret",
    )
    assert not result.handled and result.accepted is None and result.status == "unconfirmed"
    assert requests == [
        "http://127.0.0.1:8766/attachments/upload",
        "http://127.0.0.1:8766/protocol/message-with-attachments",
    ]
    assert "Protocol message queued" not in capsys.readouterr().out


@pytest.mark.parametrize("mode", ["valid_without_flags", "wrong_peer", "unsigned", "tampered"])
def test_attachment_upload_authenticates_target_without_capability_veto(monkeypatch, tmp_path, mode):
    from orchestrator.hchat_submit_result import capture_hchat_submission
    from remote.security.shared_token import build_response_auth

    selected = tmp_path / "private-report.txt"
    selected.write_bytes(b"selected attachment")
    requests = []
    def respond(req, timeout=0):
        requests.append(req.full_url)
        if req.full_url.endswith("/protocol/handshake"):
            nonce = {key.lower(): value for key, value in req.header_items()}["x-hashi-nonce"]
            body = {
                "status": "handshake_accept",
                "instance_id": "HASHI8" if mode == "wrong_peer" else "HASHI9",
                "capabilities": [],
            }
            if mode != "unsigned":
                body["response_auth"] = build_response_auth(
                    shared_token="shared-secret", request_nonce=nonce, payload=body,
                )
            if mode == "tampered":
                body["instance_id"] = "HASHI8"
            return _FakeResponse(body)
        if req.full_url.endswith("/attachments/upload"):
            return _FakeResponse({"ok": True, "attachment": {"pending_upload_id": "pu-1", "size_bytes": 19}})
        if req.full_url.endswith("/protocol/message-with-attachments"):
            return _signed_response(req, {"ok": True, "state": "accepted"})
        pytest.fail(f"unexpected request: {req.full_url}")

    # Undo the common verified-peer stub: this case must exercise the actual handshake.
    monkeypatch.undo()
    monkeypatch.setattr(protocol_send.urllib_request, "urlopen", respond)
    monkeypatch.setattr(protocol_send, "_load_config", lambda: {"global": {"instance_id": "HASHI1"}})
    monkeypatch.setattr(protocol_send, "_load_instances", lambda: {})
    monkeypatch.setattr(protocol_send, "_find_remote_instance", lambda *a, **k: {"remote_host": "127.0.0.1", "remote_port": 8766})
    monkeypatch.setattr(protocol_send, "_probe_remote_http", lambda *a: True)
    monkeypatch.setattr(protocol_send, "_local_sidecar_base_url", lambda *a: None)
    monkeypatch.setattr(protocol_send, "_record_outbound_correlation", lambda *a, **k: None)
    result = capture_hchat_submission(
        protocol_send.send_protocol_message, "lily@HASHI9", "zelda", "hello",
        attachments=[selected], shared_token="shared-secret",
    )
    assert requests[0] == "http://127.0.0.1:8766/protocol/handshake"
    if mode == "valid_without_flags":
        assert result.handled and result.accepted is True
        assert requests[1:] == [
            "http://127.0.0.1:8766/attachments/upload",
            "http://127.0.0.1:8766/protocol/message-with-attachments",
        ]
    else:
        assert not result.handled and result.accepted is False
        assert requests == ["http://127.0.0.1:8766/protocol/handshake"]


@pytest.mark.parametrize("mode", ["valid", "wrong_peer", "unsigned"])
def test_plain_text_authenticates_target_before_disclosure(monkeypatch, mode):
    from orchestrator.hchat_submit_result import capture_hchat_submission
    from remote.security.shared_token import build_response_auth

    requests = []
    def respond(req, timeout=0):
        requests.append(req.full_url)
        if req.full_url.endswith("/protocol/handshake"):
            nonce = {key.lower(): value for key, value in req.header_items()}["x-hashi-nonce"]
            body = {
                "status": "handshake_accept",
                "instance_id": "HASHI8" if mode == "wrong_peer" else "HASHI9",
                "capabilities": [],
            }
            if mode != "unsigned":
                body["response_auth"] = build_response_auth(
                    shared_token="shared-secret", request_nonce=nonce, payload=body,
                )
            return _FakeResponse(body)
        if req.full_url.endswith("/protocol/message"):
            return _signed_response(req, {"ok": True, "state": "accepted"})
        pytest.fail(f"unexpected request: {req.full_url}")

    monkeypatch.undo()
    monkeypatch.setattr(protocol_send.urllib_request, "urlopen", respond)
    monkeypatch.setattr(protocol_send, "_load_config", lambda: {"global": {"instance_id": "HASHI1"}})
    monkeypatch.setattr(protocol_send, "_load_instances", lambda: {})
    monkeypatch.setattr(protocol_send, "_find_remote_instance", lambda *a, **k: {"remote_host": "127.0.0.1", "remote_port": 8766})
    monkeypatch.setattr(protocol_send, "_probe_remote_http", lambda *a: True)
    monkeypatch.setattr(protocol_send, "_local_sidecar_base_url", lambda *a: None)
    monkeypatch.setattr(protocol_send, "_record_outbound_correlation", lambda *a, **k: None)
    result = capture_hchat_submission(
        protocol_send.send_protocol_message, "lily@HASHI9", "zelda", "private message",
        shared_token="shared-secret",
    )
    assert requests[0] == "http://127.0.0.1:8766/protocol/handshake"
    if mode == "valid":
        assert result.handled and result.accepted is True
        assert requests[1:] == ["http://127.0.0.1:8766/protocol/message"]
    else:
        assert not result.handled and result.accepted is False
        assert requests == ["http://127.0.0.1:8766/protocol/handshake"]
