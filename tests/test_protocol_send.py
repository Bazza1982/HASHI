from __future__ import annotations

import json

from remote.security.shared_token import AUTH_SCHEME
from tools import protocol_send


class _FakeResponse:
    def __init__(self, payload: dict):
        self._payload = payload

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


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
        return _FakeResponse({"ok": True, "state": "accepted"})

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

    assert ok is True
    assert len(captured) == 1
    request = captured[0]
    headers = {key.lower(): value for key, value in request["headers"].items()}
    assert request["url"] == "http://10.0.0.9:8766/protocol/message"
    assert headers["x-hashi-auth-scheme"] == AUTH_SCHEME
    assert headers["x-hashi-from-instance"] == "HASHI1"
    assert request["body"]["body"]["text"] == "hello there"
    assert request["body"]["to_agent"] == "lily"
    assert "Protocol message delivered" in capsys.readouterr().out


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
            return _FakeResponse({"ok": True, "state": "delivered_to_local_queue"})
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
            return _FakeResponse({"ok": False, "error": "commit boom"})
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
