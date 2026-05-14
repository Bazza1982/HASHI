from __future__ import annotations

import json
import time
from pathlib import Path

from fastapi.testclient import TestClient

from remote.api.server import create_app
from remote.security.pairing import PairingManager
from remote.security.shared_token import build_auth_headers
from remote.terminal.executor import TerminalExecutor


class _ProtocolStub:
    def __init__(self):
        self.messages: list[dict] = []

    def get_peer_view(self, peer):
        return {"instance_id": peer.instance_id}

    def get_protocol_status(self):
        return {"capabilities": ["handshake_v2", "message_attachments_v1"]}

    async def handle_protocol_message(self, payload: dict):
        self.messages.append(payload)
        return 202, {"ok": True, "accepted": True, "state": "delivered_to_local_queue"}


def _write_shared_token(tmp_path: Path, token: str = "shared-secret") -> str:
    (tmp_path / "secrets.json").write_text(
        json.dumps({"hashi_remote_shared_token": token}),
        encoding="utf-8",
    )
    return token


def _client(tmp_path: Path, *, lan_mode: bool = False):
    protocol = _ProtocolStub()
    app = create_app(
        {"instance_id": "HASHI_LOCAL", "display_name": "Local", "remote_port": 8766},
        PairingManager(storage_dir=tmp_path / "pairing", lan_mode=lan_mode),
        TerminalExecutor(),
        protocol_manager=protocol,
        hashi_root=str(tmp_path),
        workbench_port=18800,
    )
    return TestClient(app), protocol


def _signed_headers(token: str, *, method: str, path: str, from_instance: str, body: bytes):
    headers = {"Content-Type": "application/json"}
    headers.update(
        build_auth_headers(
            shared_token=token,
            method=method,
            path=path,
            from_instance=from_instance,
            body_bytes=body,
            timestamp=int(time.time()),
            nonce=f"nonce-{method.lower()}-{path.replace('/', '-')}",
        )
    )
    return headers


def test_attachment_upload_and_commit_delivers_via_existing_protocol_path(tmp_path):
    token = _write_shared_token(tmp_path)
    client, protocol = _client(tmp_path, lan_mode=False)

    upload_payload = {
        "message_id": "msg-1",
        "from_instance": "HASHI2",
        "attachment_id": "att-1",
        "filename": "report.txt",
        "mime_type": "text/plain",
        "content_b64": "aGVsbG8=",
        "sha256": "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824",
    }
    upload_body = json.dumps(upload_payload).encode("utf-8")
    upload_response = client.post(
        "/attachments/upload",
        content=upload_body,
        headers=_signed_headers(token, method="POST", path="/attachments/upload", from_instance="HASHI2", body=upload_body),
    )

    assert upload_response.status_code == 200
    pending_upload_id = upload_response.json()["attachment"]["pending_upload_id"]

    commit_payload = {
        "message_id": "msg-1",
        "conversation_id": "conv-1",
        "from_instance": "HASHI2",
        "from_agent": "zhaojun",
        "to_instance": "HASHI_LOCAL",
        "to_agent": "lily",
        "body": {"text": "please review"},
        "attachments": [
            {
                "attachment_id": "att-1",
                "pending_upload_id": pending_upload_id,
                "filename": "report.txt",
                "mime_type": "text/plain",
                "caption": "latest report",
            }
        ],
    }
    commit_body = json.dumps(commit_payload).encode("utf-8")
    commit_response = client.post(
        "/protocol/message-with-attachments",
        content=commit_body,
        headers=_signed_headers(
            token,
            method="POST",
            path="/protocol/message-with-attachments",
            from_instance="HASHI2",
            body=commit_body,
        ),
    )

    assert commit_response.status_code == 202
    body = commit_response.json()
    assert body["ok"] is True
    assert body["attachments"][0]["filename"] == "report.txt"
    assert len(protocol.messages) == 1
    delivered = protocol.messages[0]
    assert delivered["body"]["attachments"][0]["attachment_id"] == "att-1"
    assert "[Remote attachments]" in delivered["body"]["text"]
    assert "report.txt" in delivered["body"]["text"]

    manifest_headers = build_auth_headers(
        shared_token=token,
        method="GET",
        path="/attachments/message/msg-1",
        from_instance="HASHI2",
        body_bytes=b"",
        timestamp=int(time.time()),
        nonce="nonce-get-manifest",
    )
    manifest_response = client.get("/attachments/message/msg-1", headers=manifest_headers)

    assert manifest_response.status_code == 200
    manifest = manifest_response.json()["manifest"]
    assert manifest["message_id"] == "msg-1"
    stored_path = Path(manifest["attachments"][0]["stored_path"])
    assert stored_path.exists()
    assert stored_path.read_text(encoding="utf-8") == "hello"


def test_attachment_commit_rejects_missing_pending_upload(tmp_path):
    token = _write_shared_token(tmp_path)
    client, protocol = _client(tmp_path, lan_mode=False)

    commit_payload = {
        "message_id": "msg-2",
        "conversation_id": "conv-2",
        "from_instance": "HASHI2",
        "from_agent": "zhaojun",
        "to_instance": "HASHI_LOCAL",
        "to_agent": "lily",
        "body": {"text": "missing upload"},
        "attachments": [
            {
                "attachment_id": "att-missing",
                "pending_upload_id": "pu-msg-2-att-missing",
            }
        ],
    }
    commit_body = json.dumps(commit_payload).encode("utf-8")
    response = client.post(
        "/protocol/message-with-attachments",
        content=commit_body,
        headers=_signed_headers(
            token,
            method="POST",
            path="/protocol/message-with-attachments",
            from_instance="HASHI2",
            body=commit_body,
        ),
    )

    assert response.status_code == 400
    assert "pending upload not found" in response.json()["error"]
    assert protocol.messages == []
