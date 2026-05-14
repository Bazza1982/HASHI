from __future__ import annotations

import json
import time
from types import SimpleNamespace
from pathlib import Path

from fastapi.testclient import TestClient

from remote.api.server import create_app
from remote.protocol_manager import ProtocolManager
from remote.security.pairing import PairingManager
from remote.security.shared_token import build_auth_headers
from remote.terminal.executor import TerminalExecutor


class _ProtocolStub:
    def __init__(self):
        self.handshakes: list[dict] = []
        self.messages: list[dict] = []

    def get_peer_view(self, peer):
        return {"instance_id": peer.instance_id}

    def get_protocol_status(self):
        return {"capabilities": ["handshake_v2"]}

    def handle_handshake(self, payload: dict) -> dict:
        self.handshakes.append(payload)
        return {"status": "handshake_accept", "instance_id": "HASHI_LOCAL"}

    async def handle_protocol_message(self, payload: dict):
        self.messages.append(payload)
        return 202, {"ok": True, "accepted": True}


class _PeerRegistryStub:
    def get_peers(self):
        return [SimpleNamespace(instance_id="HASHI2"), SimpleNamespace(instance_id="HASHI9")]


def _write_shared_token(tmp_path, token: str = "shared-secret") -> str:
    (tmp_path / "secrets.json").write_text(
        json.dumps({"hashi_remote_shared_token": token}),
        encoding="utf-8",
    )
    return token


def _client(tmp_path):
    protocol = _ProtocolStub()
    app = create_app(
        {"instance_id": "HASHI_LOCAL", "display_name": "Local", "remote_port": 8766},
        PairingManager(storage_dir=tmp_path / "pairing", lan_mode=True),
        TerminalExecutor(),
        peer_registry=_PeerRegistryStub(),
        protocol_manager=protocol,
        hashi_root=str(tmp_path),
        workbench_port=18800,
    )
    return TestClient(app), protocol


def _client_lan_mode(tmp_path, *, lan_mode: bool):
    protocol = _ProtocolStub()
    app = create_app(
        {"instance_id": "HASHI_LOCAL", "display_name": "Local", "remote_port": 8766},
        PairingManager(storage_dir=tmp_path / "pairing", lan_mode=lan_mode),
        TerminalExecutor(),
        peer_registry=_PeerRegistryStub(),
        protocol_manager=protocol,
        hashi_root=str(tmp_path),
        workbench_port=18800,
    )
    return TestClient(app), protocol


def test_protocol_handshake_requires_auth_when_token_configured(tmp_path):
    _write_shared_token(tmp_path)
    client, protocol = _client(tmp_path)
    payload = {
        "from_instance": "HASHI2",
        "display_handle": "@hashi2",
        "remote_port": 8767,
    }

    response = client.post("/protocol/handshake", json=payload)

    assert response.status_code == 401
    assert response.json()["reason"] == "auth_required"
    assert protocol.handshakes == []


def test_protocol_handshake_accepts_valid_shared_token(tmp_path):
    token = _write_shared_token(tmp_path)
    client, protocol = _client(tmp_path)
    payload = {
        "from_instance": "HASHI2",
        "display_handle": "@hashi2",
        "remote_port": 8767,
    }
    body = json.dumps(payload).encode("utf-8")
    now = int(time.time())
    headers = {"Content-Type": "application/json"}
    headers.update(
        build_auth_headers(
            shared_token=token,
            method="POST",
            path="/protocol/handshake",
            from_instance="HASHI2",
            body_bytes=body,
            timestamp=now,
            nonce="nonce-1",
        )
    )

    response = client.post("/protocol/handshake", content=body, headers=headers)

    assert response.status_code == 200
    assert response.json()["status"] == "handshake_accept"
    assert protocol.handshakes[0]["_client_ip"] is not None


def test_protocol_handshake_rejects_replayed_nonce(tmp_path):
    token = _write_shared_token(tmp_path)
    client, protocol = _client(tmp_path)
    payload = {
        "from_instance": "HASHI2",
        "display_handle": "@hashi2",
        "remote_port": 8767,
    }
    body = json.dumps(payload).encode("utf-8")
    now = int(time.time())
    headers = {"Content-Type": "application/json"}
    headers.update(
        build_auth_headers(
            shared_token=token,
            method="POST",
            path="/protocol/handshake",
            from_instance="HASHI2",
            body_bytes=body,
            timestamp=now,
            nonce="replay-me",
        )
    )

    first = client.post("/protocol/handshake", content=body, headers=headers)
    second = client.post("/protocol/handshake", content=body, headers=headers)

    assert first.status_code == 200
    assert second.status_code == 401
    assert second.json()["reason"] == "auth_failed"
    assert len(protocol.handshakes) == 1


def test_protocol_message_requires_auth_when_token_configured(tmp_path):
    _write_shared_token(tmp_path)
    client, protocol = _client(tmp_path)
    payload = {
        "message_id": "m1",
        "conversation_id": "c1",
        "from_instance": "HASHI2",
        "from_agent": "rika",
        "to_instance": "HASHI_LOCAL",
        "to_agent": "lily",
        "body": {"text": "hello"},
    }

    response = client.post("/protocol/message", json=payload)

    assert response.status_code == 401
    assert response.json()["body"]["code"] == "auth_required"
    assert protocol.messages == []


def test_file_push_requires_auth_when_token_configured_and_lan_off(tmp_path):
    _write_shared_token(tmp_path)
    client, _protocol = _client_lan_mode(tmp_path, lan_mode=False)
    payload = {
        "dest_path": "incoming/report.txt",
        "content_b64": "aGVsbG8=",
        "sha256": "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824",
    }

    response = client.post("/files/push", json=payload)

    assert response.status_code == 401
    body = response.json()
    assert body["ok"] is False
    assert body["code"] == "auth_required"


def test_file_push_accepts_valid_shared_token(tmp_path):
    token = _write_shared_token(tmp_path)
    client, _protocol = _client_lan_mode(tmp_path, lan_mode=False)
    payload = {
        "dest_path": "incoming/report.txt",
        "content_b64": "aGVsbG8=",
        "sha256": "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824",
    }
    body = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    headers.update(
        build_auth_headers(
            shared_token=token,
            method="POST",
            path="/files/push",
            from_instance="HASHI2",
            body_bytes=body,
            timestamp=int(time.time()),
            nonce="file-push-1",
        )
    )

    response = client.post("/files/push", content=body, headers=headers)

    assert response.status_code == 200
    assert response.json()["ok"] is True


def test_file_stat_accepts_valid_shared_token(tmp_path):
    token = _write_shared_token(tmp_path)
    client, _protocol = _client_lan_mode(tmp_path, lan_mode=False)
    target = tmp_path / "incoming" / "report.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("hello", encoding="utf-8")
    headers = build_auth_headers(
        shared_token=token,
        method="GET",
        path="/files/stat",
        from_instance="HASHI2",
        body_bytes=b"",
        timestamp=int(time.time()),
        nonce="file-stat-1",
    )

    response = client.get("/files/stat", params={"path": "incoming/report.txt"}, headers=headers)

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert response.json()["sha256"] == "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"


def test_file_push_invalid_hmac_does_not_fall_back_to_lan(tmp_path):
    token = _write_shared_token(tmp_path)
    client, _protocol = _client_lan_mode(tmp_path, lan_mode=True)
    payload = {
        "dest_path": "incoming/report.txt",
        "content_b64": "aGVsbG8=",
        "sha256": "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824",
    }
    body = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    headers.update(
        build_auth_headers(
            shared_token=token,
            method="POST",
            path="/files/push",
            from_instance="HASHI2",
            body_bytes=body,
            timestamp=int(time.time()),
            nonce="file-push-invalid",
        )
    )
    headers["X-Hashi-Digest"] = "0" * 64

    response = client.post("/files/push", content=body, headers=headers)

    assert response.status_code == 401
    assert response.json()["code"] == "auth_failed"


def test_peers_redacts_without_auth_and_allows_signed_get(tmp_path):
    token = _write_shared_token(tmp_path)
    client, _protocol = _client(tmp_path)

    response = client.get("/peers")
    assert response.status_code == 200
    assert response.json() == {"ok": True, "peers": [], "count": 2, "trusted_view": False}

    headers = build_auth_headers(
        shared_token=token,
        method="GET",
        path="/peers",
        from_instance="HASHI2",
        body_bytes=b"",
        timestamp=int(time.time()),
        nonce="get-peers",
    )
    trusted = client.get("/peers", headers=headers)

    assert trusted.status_code == 200
    assert trusted.json()["count"] == 2
    assert len(trusted.json()["peers"]) == 2


def test_protocol_status_is_redacted_without_auth(tmp_path):
    _write_shared_token(tmp_path)
    client, _protocol = _client(tmp_path)

    response = client.get("/protocol/status")

    assert response.status_code == 200
    body = response.json()
    assert body["trusted_view"] is False
    assert body["protocol_auth_mode"] == "shared-token"
    assert "local_agents" not in body
    assert "peers" not in body


def test_remote_config_defaults_lan_mode_off():
    config_path = Path(__file__).resolve().parent.parent / "remote" / "config.yaml"
    text = config_path.read_text(encoding="utf-8")

    assert "lan_mode: false" in text


def test_protocol_manager_post_json_signs_protocol_requests(tmp_path, monkeypatch):
    _write_shared_token(tmp_path)
    manager = ProtocolManager(
        hashi_root=tmp_path,
        instance_info={"instance_id": "HASHI_LOCAL"},
        peer_registry=None,
        workbench_port=18800,
    )
    seen = {}

    class _Resp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return b"{}"

    def fake_urlopen(req, timeout=None, context=None):
        seen["headers"] = dict(req.header_items())
        return _Resp()

    monkeypatch.setattr("remote.protocol_manager.urllib_request.urlopen", fake_urlopen)

    manager._post_json(
        "http://127.0.0.1:8767/protocol/handshake",
        {"from_instance": "HASHI_LOCAL"},
        timeout=1,
    )

    assert seen["headers"]["X-hashi-auth-scheme"] == "hashi-shared-hmac-v1"
    assert seen["headers"]["X-hashi-from-instance"] == "HASHI_LOCAL"
