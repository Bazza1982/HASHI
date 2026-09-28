from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from remote.api.server import _merge_attachment_text, create_app
from remote.attachments import (
    AttachmentStore,
    STREAM_MAX_ATTACHMENTS_PER_MESSAGE,
    STREAM_MAX_TOTAL_ATTACHMENT_BYTES,
)
from remote.security.pairing import PairingManager
from remote.security.shared_token import build_auth_headers
from remote.terminal.executor import TerminalExecutor


class _ProtocolStub:
    def __init__(self, *, status: int = 202, result: dict | None = None):
        self.messages: list[dict] = []
        self.status = status
        self.result = result or {
            "ok": True,
            "accepted": True,
            "state": "delivered_to_local_queue",
        }

    def get_peer_view(self, peer):
        return {"instance_id": peer.instance_id}

    def get_protocol_status(self):
        return {"capabilities": ["handshake_v2", "message_attachments_v1"]}

    async def handle_protocol_message(self, payload: dict):
        self.messages.append(payload)
        return self.status, dict(self.result)


def _write_shared_token(tmp_path: Path, token: str = "shared-secret") -> str:
    (tmp_path / "secrets.json").write_text(
        json.dumps({"hashi_remote_shared_token": token}),
        encoding="utf-8",
    )
    return token


def _client(tmp_path: Path, *, lan_mode: bool = False, protocol=None):
    protocol = protocol or _ProtocolStub()
    app = create_app(
        {"instance_id": "HASHI_LOCAL", "display_name": "Local", "remote_port": 8766},
        PairingManager(storage_dir=tmp_path / "pairing", lan_mode=lan_mode),
        TerminalExecutor(),
        protocol_manager=protocol,
        hashi_root=str(tmp_path),
        workbench_port=18800,
    )
    return TestClient(app), protocol


def _signed_headers(
    token: str,
    *,
    method: str,
    path: str,
    from_instance: str,
    body: bytes,
    nonce: str | None = None,
):
    headers = {"Content-Type": "application/json"}
    headers.update(
        build_auth_headers(
            shared_token=token,
            method=method,
            path=path,
            from_instance=from_instance,
            body_bytes=body,
            timestamp=int(time.time()),
            nonce=nonce or f"nonce-{method.lower()}-{path.replace('/', '-')}",
        )
    )
    return headers


def _stage_streaming_attachment(
    client,
    token: str,
    *,
    message_id: str,
    filename: str,
    payload: bytes,
) -> dict:
    digest = hashlib.sha256(payload).hexdigest()
    begin_payload = {
        "message_id": message_id,
        "from_instance": "HASHI2",
        "attachment_id": "att-1",
        "filename": filename,
        "mime_type": "application/octet-stream",
        "size_bytes": len(payload),
        "sha256": digest,
    }
    begin_body = json.dumps(begin_payload).encode("utf-8")
    begin = client.post(
        "/attachments/v2/begin",
        content=begin_body,
        headers=_signed_headers(
            token,
            method="POST",
            path="/attachments/v2/begin",
            from_instance="HASHI2",
            body=begin_body,
            nonce=f"begin-{message_id}",
        ),
    )
    assert begin.status_code == 200, begin.text
    pending_upload_id = begin.json()["attachment"]["pending_upload_id"]

    offset = 0
    for index, chunk in enumerate((payload[:5], payload[5:])):
        if not chunk:
            continue
        target = f"/attachments/v2/chunk/{pending_upload_id}?offset={offset}"
        uploaded = client.put(
            target,
            content=chunk,
            headers=_signed_headers(
                token,
                method="PUT",
                path=target,
                from_instance="HASHI2",
                body=chunk,
                nonce=f"chunk-{message_id}-{index}",
            ),
        )
        assert uploaded.status_code == 200, uploaded.text
        offset += len(chunk)

    finish_payload = {
        "message_id": message_id,
        "from_instance": "HASHI2",
        "pending_upload_id": pending_upload_id,
    }
    finish_body = json.dumps(finish_payload).encode("utf-8")
    finish = client.post(
        "/attachments/v2/finish",
        content=finish_body,
        headers=_signed_headers(
            token,
            method="POST",
            path="/attachments/v2/finish",
            from_instance="HASHI2",
            body=finish_body,
            nonce=f"finish-{message_id}",
        ),
    )
    assert finish.status_code == 200, finish.text
    return {
        "attachment_id": "att-1",
        "pending_upload_id": pending_upload_id,
        "filename": filename,
        "mime_type": "application/octet-stream",
        "size_bytes": len(payload),
        "sha256": digest,
    }


def test_image_attachment_summary_exposes_opaque_image_reference():
    merged = _merge_attachment_text(
        {"text": "please inspect"},
        [
            {
                "attachment_id": "img-1",
                "filename": "photo.jpg",
                "mime_type": "image/jpeg",
                "size_bytes": 123,
            }
        ],
        message_id="msg-image",
    )

    assert "image_ref=attachment:msg-image:img-1" in merged["text"]
    assert "stored_path" not in merged["text"]


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
    assert "attachment_id=att-1" in delivered["body"]["text"]
    assert "attachment_ref=attachment:msg-1:att-1" in delivered["body"]["text"]
    assert "image_ref=" not in delivered["body"]["text"]

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


def test_attachment_upload_cancel_removes_pending_upload(tmp_path):
    token = _write_shared_token(tmp_path)
    client, _protocol = _client(tmp_path, lan_mode=False)
    upload_payload = {
        "message_id": "msg-cancel",
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
    pending_upload_id = upload_response.json()["attachment"]["pending_upload_id"]

    cancel_payload = {
        "message_id": "msg-cancel",
        "from_instance": "HASHI2",
        "pending_upload_ids": [pending_upload_id],
        "reason": "test cleanup",
    }
    cancel_body = json.dumps(cancel_payload).encode("utf-8")
    cancel_response = client.post(
        "/attachments/upload/cancel",
        content=cancel_body,
        headers=_signed_headers(token, method="POST", path="/attachments/upload/cancel", from_instance="HASHI2", body=cancel_body),
    )

    assert cancel_response.status_code == 200
    assert cancel_response.json()["removed"] == 1

    commit_payload = {
        "message_id": "msg-cancel",
        "conversation_id": "conv-cancel",
        "from_instance": "HASHI2",
        "from_agent": "zhaojun",
        "to_instance": "HASHI_LOCAL",
        "to_agent": "lily",
        "body": {"text": "missing upload"},
        "attachments": [{"attachment_id": "att-1", "pending_upload_id": pending_upload_id}],
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


def test_attachment_upload_uses_random_pending_ids_without_overwrite(tmp_path):
    token = _write_shared_token(tmp_path)
    client, _protocol = _client(tmp_path, lan_mode=False)
    payload = {
        "message_id": "msg-random",
        "from_instance": "HASHI2",
        "attachment_id": "att-1",
        "filename": "report.txt",
        "mime_type": "text/plain",
        "content_b64": "aGVsbG8=",
        "sha256": "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824",
    }
    body = json.dumps(payload).encode("utf-8")
    first = client.post(
        "/attachments/upload",
        content=body,
        headers=_signed_headers(token, method="POST", path="/attachments/upload", from_instance="HASHI2", body=body),
    )
    second_headers = build_auth_headers(
        shared_token=token,
        method="POST",
        path="/attachments/upload",
        from_instance="HASHI2",
        body_bytes=body,
        timestamp=int(time.time()),
        nonce="nonce-post-/attachments/upload-second",
    )
    second_headers["Content-Type"] = "application/json"
    second = client.post(
        "/attachments/upload",
        content=body,
        headers=second_headers,
    )

    first_pending = first.json()["attachment"]["pending_upload_id"]
    second_pending = second.json()["attachment"]["pending_upload_id"]
    assert first_pending != second_pending
    assert Path(first.json()["attachment"]["spool_path"]).exists()
    assert Path(second.json()["attachment"]["spool_path"]).exists()


def test_streaming_v2_accepts_opaque_script_and_delivers_only_after_commit(tmp_path):
    token = _write_shared_token(tmp_path)
    client, protocol = _client(tmp_path, lan_mode=True)
    attachment = _stage_streaming_attachment(
        client,
        token,
        message_id="msg-stream",
        filename="deploy.ps1",
        payload=b"Write-Output 'hello'\n",
    )

    assert protocol.messages == []
    commit_payload = {
        "message_id": "msg-stream",
        "conversation_id": "conv-stream",
        "from_instance": "HASHI2",
        "from_agent": "zhaojun",
        "to_instance": "HASHI_LOCAL",
        "to_agent": "lily",
        "body": {"text": "script attached"},
        "attachments": [attachment],
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
            nonce="commit-msg-stream",
        ),
    )

    assert response.status_code == 202, response.text
    assert len(protocol.messages) == 1
    delivered_attachment = protocol.messages[0]["body"]["attachments"][0]
    assert delivered_attachment["filename"] == "deploy.ps1"
    stored_path = Path(response.json()["attachments"][0]["stored_path"])
    assert stored_path.read_bytes() == b"Write-Output 'hello'\n"


def test_streaming_v2_requires_shared_token_even_when_lan_mode_is_enabled(tmp_path):
    _write_shared_token(tmp_path)
    client, _protocol = _client(tmp_path, lan_mode=True)
    payload = {
        "message_id": "msg-auth",
        "from_instance": "HASHI2",
        "attachment_id": "att-1",
        "filename": "archive.7z",
        "mime_type": "application/octet-stream",
        "size_bytes": 0,
        "sha256": hashlib.sha256(b"").hexdigest(),
    }

    response = client.post("/attachments/v2/begin", json=payload)

    assert response.status_code == 401


def test_streaming_v2_rolls_back_committed_files_when_message_delivery_fails(tmp_path):
    token = _write_shared_token(tmp_path)
    protocol = _ProtocolStub(
        status=503,
        result={"ok": False, "error": "local enqueue failed"},
    )
    client, _protocol = _client(tmp_path, lan_mode=False, protocol=protocol)
    attachment = _stage_streaming_attachment(
        client,
        token,
        message_id="msg-rollback",
        filename="encrypted.pkg",
        payload=b"opaque-ciphertext",
    )
    commit_payload = {
        "message_id": "msg-rollback",
        "conversation_id": "conv-rollback",
        "from_instance": "HASHI2",
        "from_agent": "zhaojun",
        "to_instance": "HASHI_LOCAL",
        "to_agent": "lily",
        "body": {"text": "all or nothing"},
        "attachments": [attachment],
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
            nonce="commit-msg-rollback",
        ),
    )

    assert response.status_code == 503
    assert not (
        tmp_path
        / "state"
        / "remote_attachments"
        / "hashi_local"
        / "messages"
        / "msg-rollback"
    ).exists()


def test_streaming_v2_enforces_ten_files_and_one_gibibyte_without_type_filters(
    tmp_path,
):
    store = AttachmentStore(root=tmp_path, instance_id="HASHI_LOCAL")
    per_file = STREAM_MAX_TOTAL_ATTACHMENT_BYTES // STREAM_MAX_ATTACHMENTS_PER_MESSAGE
    sizes = [per_file] * (STREAM_MAX_ATTACHMENTS_PER_MESSAGE - 1)
    sizes.append(STREAM_MAX_TOTAL_ATTACHMENT_BYTES - sum(sizes))

    for index, size_bytes in enumerate(sizes):
        store.begin_stream_upload(
            message_id="msg-limits",
            from_instance="HASHI2",
            attachment_id=f"att-{index}",
            filename=f"opaque-{index}.pkg",
            mime_type="application/octet-stream",
            size_bytes=size_bytes,
            sha256=hashlib.sha256(f"declared-{index}".encode()).hexdigest(),
        )

    with pytest.raises(ValueError, match="attachment count"):
        store.begin_stream_upload(
            message_id="msg-limits",
            from_instance="HASHI2",
            attachment_id="att-over-count",
            filename="eleventh.sh",
            mime_type="application/x-sh",
            size_bytes=0,
            sha256=hashlib.sha256(b"").hexdigest(),
        )

    store.begin_stream_upload(
        message_id="msg-total",
        from_instance="HASHI2",
        attachment_id="att-full",
        filename="full.enc",
        mime_type="application/octet-stream",
        size_bytes=STREAM_MAX_TOTAL_ATTACHMENT_BYTES,
        sha256=hashlib.sha256(b"declared-full").hexdigest(),
    )
    with pytest.raises(ValueError, match="total attachment size"):
        store.begin_stream_upload(
            message_id="msg-total",
            from_instance="HASHI2",
            attachment_id="att-over-total",
            filename="over.enc",
            mime_type="application/octet-stream",
            size_bytes=1,
            sha256=hashlib.sha256(b"declared-over").hexdigest(),
        )
