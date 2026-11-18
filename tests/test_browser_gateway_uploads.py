from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from browser_gateway.server import BrowserGatewayServer
from browser_gateway.store import BrowserGatewayStore


def _setup(tmp_path: Path, **options):
    store = BrowserGatewayStore(tmp_path / "gateway.sqlite")
    pair = store.create_pair_request("upload owner")
    owner = store.complete_pair(pair.device_id, pair.pairing_code)
    foreign_pair = store.create_pair_request("other device")
    foreign = store.complete_pair(foreign_pair.device_id, foreign_pair.pairing_code)
    thread = store.create_thread(pair.device_id, "agent", title="upload")
    def server():
        return BrowserGatewayServer(
            project_root=tmp_path, state_db=tmp_path / "gateway.sqlite",
            audit_log=tmp_path / "gateway-audit.jsonl", **options,
        )
    return store, owner, foreign, thread, server


def _headers(owner):
    return {"Authorization": f"Bearer {owner['access_token']}"}


def _metadata(thread, data, *, digest=None, notify=True):
    return {
        "thread_id": thread["thread_id"], "filename": "encrypted.bin",
        "mime_type": "application/octet-stream", "plaintext_bytes": len(data),
        "ciphertext_bytes": len(data),
        "ciphertext_sha256": digest or hashlib.sha256(data).hexdigest(),
        "encryption": {"scheme": "test"}, "notify_agent": notify,
    }


@pytest.mark.asyncio
async def test_large_upload_resumes_after_gateway_recreation_and_notifies_once(tmp_path: Path):
    data = bytes(range(256)) * (100 * 1024) + b"tail"
    store, owner, foreign, thread, make_server = _setup(
        tmp_path, json_body_bytes=1024, upload_chunk_bytes=2 * 1024 * 1024,
    )
    calls = []
    async def send(*, agent, text, source):
        calls.append((agent, text, source))
        return {"ok": True, "request_id": "test-request", "text": "accepted"}
    server = make_server()
    server._send_to_workbench = send
    headers = _headers(owner)
    async with TestClient(TestServer(server.app, host="127.0.0.1")) as client:
        response = await client.post("/browser/file/upload/start",
                                     json=_metadata(thread, data), headers=headers)
        assert response.status == 200
        upload_id = (await response.json())["upload_id"]
        route = f"/browser/file/upload/{upload_id}"
        early = await client.post(route + "/finish", headers=headers)
        assert early.status == 409
        chunk = data[:2 * 1024 * 1024]
        first = await client.put(route, data=chunk, headers={**headers, "Upload-Offset": "0"})
        assert first.status == 200
        offset = (await first.json())["offset"]
        duplicate = await client.put(route, data=chunk, headers={**headers, "Upload-Offset": "0"})
        assert duplicate.status == 409
        assert (await duplicate.json())["offset"] == offset
        foreign_status = await client.get(route, headers=_headers(foreign))
        assert foreign_status.status == 404
        foreign_chunk = await client.put(
            route, data=b"x", headers={**_headers(foreign), "Upload-Offset": str(offset)}
        )
        assert foreign_chunk.status == 404

    server = make_server()
    server._send_to_workbench = send
    async with TestClient(TestServer(server.app, host="127.0.0.1")) as client:
        route = f"/browser/file/upload/{upload_id}"
        status = await client.get(route, headers=headers)
        assert (await status.json())["offset"] == offset
        while offset < len(data):
            chunk = data[offset:offset + 2 * 1024 * 1024]
            response = await client.put(
                route, data=chunk, headers={**headers, "Upload-Offset": str(offset)}
            )
            assert response.status == 200
            offset = (await response.json())["offset"]
        completed = await client.post(route + "/finish", headers=headers)
        assert completed.status == 200
        body = await completed.json()
        assert body["notification_status"] == "sent"
        assert body["attachment"]["ciphertext_bytes"] == len(data)
        stored = tmp_path / "state" / body["attachment"]["storage_relpath"]
        with stored.open("rb") as stream:
            assert hashlib.file_digest(stream, "sha256").hexdigest() == hashlib.sha256(data).hexdigest()
        repeated = await client.post(route + "/finish", headers=headers)
        assert repeated.status == 200
        assert (await repeated.json())["attachment"]["attachment_id"] == body["attachment"]["attachment_id"]
        assert len(store.list_attachments(thread["thread_id"], owner["device_id"])) == 1
        assert len(calls) == 1
        assert "\nattachment_id:" in calls[0][1]
        assert len(data) > 24 * 1024 * 1024
        with stored.open("r+b") as changed:
            changed.write(b"tampered")
        tampered = await client.post(route + "/finish", headers=headers)
        assert tampered.status == 409
        assert len(calls) == 1


@pytest.mark.asyncio
async def test_incomplete_and_tampered_upload_cannot_be_published_and_can_cancel(tmp_path: Path):
    store, owner, foreign, thread, make_server = _setup(tmp_path)
    server = make_server()
    good = b"correct-ciphertext"
    headers = _headers(owner)
    async with TestClient(TestServer(server.app, host="127.0.0.1")) as client:
        response = await client.post("/browser/file/upload/start",
                                     json=_metadata(thread, good, notify=False), headers=headers)
        upload_id = (await response.json())["upload_id"]
        route = f"/browser/file/upload/{upload_id}"
        assert (await client.post(route + "/finish", headers=headers)).status == 409
        tampered = b"x" * len(good)
        assert (await client.put(route, data=tampered,
                                 headers={**headers, "Upload-Offset": "0"})).status == 200
        finish = await client.post(route + "/finish", headers=headers)
        assert finish.status == 409
        assert "hash mismatch" in (await finish.json())["error"]
        assert store.list_attachments(thread["thread_id"], owner["device_id"]) == []
        assert (await client.delete(route, headers=_headers(foreign))).status == 404
        row = server.upload_sessions.get(upload_id, owner["device_id"])
        partial = server.upload_sessions.partial_path(row)
        assert partial.exists()
        assert (await client.delete(route, headers=headers)).status == 200
        assert not partial.exists()
        assert (await client.get(route, headers=headers)).status == 404


@pytest.mark.asyncio
async def test_finalization_recovers_after_file_rename_and_uncertain_notification(tmp_path: Path):
    store, owner, _foreign, thread, make_server = _setup(tmp_path)
    server = make_server()
    headers = _headers(owner)
    data = b"ciphertext"
    calls = []
    async def unknown_delivery(*, agent, text, source):
        calls.append(source)
        raise OSError("synthetic response loss")
    server._send_to_workbench = unknown_delivery
    async with TestClient(TestServer(server.app, host="127.0.0.1")) as client:
        start = await client.post("/browser/file/upload/start",
                                  json=_metadata(thread, data), headers=headers)
        upload_id = (await start.json())["upload_id"]
        route = f"/browser/file/upload/{upload_id}"
        assert (await client.put(route, data=data,
                                 headers={**headers, "Upload-Offset": "0"})).status == 200
        row = server.upload_sessions.get(upload_id, owner["device_id"])
        final_path = server.upload_sessions.final_path(row)
        final_path.parent.mkdir(parents=True, exist_ok=True)
        os.replace(server.upload_sessions.partial_path(row), final_path)
        finish = await client.post(route + "/finish", headers=headers)
        assert finish.status == 200
        assert (await finish.json())["notification_status"] == "unknown"
        repeat = await client.post(route + "/finish", headers=headers)
        assert repeat.status == 200
        assert (await repeat.json())["notification_status"] == "unknown"
        assert len(calls) == 1
        assert len(store.list_attachments(thread["thread_id"], owner["device_id"])) == 1


@pytest.mark.asyncio
async def test_interrupted_chunk_reverts_to_acknowledged_offset(tmp_path: Path):
    _store, owner, _foreign, thread, make_server = _setup(tmp_path)
    server = make_server()
    data = b"1234567890"
    metadata = _metadata(thread, data, notify=False)
    row = server.upload_sessions.create(
        device_id=owner["device_id"], thread_id=thread["thread_id"],
        metadata={key: metadata[key] for key in (
            "filename", "mime_type", "plaintext_bytes", "encryption", "notify_agent"
        )} | {"note": ""},
        ciphertext_bytes=len(data), ciphertext_sha256=metadata["ciphertext_sha256"],
    )
    class Content:
        async def iter_chunked(self, _size):
            yield b"123"
            raise ConnectionResetError("synthetic disconnect")
    class Request:
        headers = {**_headers(owner), "Upload-Offset": "0"}
        match_info = {"upload_id": row["upload_id"]}
        content_length = len(data)
        content = Content()
    with pytest.raises(ConnectionResetError):
        await server.handle_upload_chunk(Request())
    current = server.upload_sessions.get(row["upload_id"], owner["device_id"])
    assert current["received_bytes"] == 0
    assert server.upload_sessions.partial_path(current).stat().st_size == 0
# HASHI-Fingerprint: hfp_ce283a5d38d24e3d81816faa31fc0d40



def test_upload_lock_excludes_other_gateway_process(tmp_path: Path):
    import subprocess
    import sys

    _store, owner, _foreign, thread, make_server = _setup(tmp_path)
    server = make_server()
    data = b"abc"
    metadata = _metadata(thread, data, notify=False)
    row = server.upload_sessions.create(
        device_id=owner["device_id"], thread_id=thread["thread_id"],
        metadata={key: metadata[key] for key in (
            "filename", "mime_type", "plaintext_bytes", "encryption", "notify_agent"
        )} | {"note": ""},
        ciphertext_bytes=len(data), ciphertext_sha256=metadata["ciphertext_sha256"],
    )
    code = chr(10).join([
        "import sys",
        "from pathlib import Path",
        "from browser_gateway.store import BrowserGatewayStore",
        "from browser_gateway.upload_sessions import UploadSessions, UploadBusy",
        "root = Path(sys.argv[1])",
        "service = UploadSessions(BrowserGatewayStore(root / 'gateway.sqlite'), root)",
        "row = service.get(sys.argv[2], sys.argv[3])",
        "try:",
        "    with service.exclusive(row):",
        "        print('acquired')",
        "except UploadBusy:",
        "    print('busy')",
    ])
    with server.upload_sessions.exclusive(row):
        child = subprocess.run(
            [sys.executable, "-c", code, str(tmp_path), row["upload_id"], owner["device_id"]],
            capture_output=True, text=True, check=True,
        )
    assert child.stdout.strip() == "busy"


@pytest.mark.asyncio
async def test_interrupted_notification_is_not_automatically_repeated(tmp_path: Path):
    _store, owner, _foreign, thread, make_server = _setup(tmp_path)
    server = make_server()
    data = b"ciphertext"
    metadata = _metadata(thread, data)
    row = server.upload_sessions.create(
        device_id=owner["device_id"], thread_id=thread["thread_id"],
        metadata={key: metadata[key] for key in (
            "filename", "mime_type", "plaintext_bytes", "encryption", "notify_agent"
        )} | {"note": ""},
        ciphertext_bytes=len(data), ciphertext_sha256=metadata["ciphertext_sha256"],
    )
    server.upload_sessions.partial_path(row).write_bytes(data)
    assert server.upload_sessions.advance(row, len(data))
    row = server.upload_sessions.get(row["upload_id"], owner["device_id"])
    final = server.upload_sessions.final_path(row)
    final.parent.mkdir(parents=True, exist_ok=True)
    os.replace(server.upload_sessions.partial_path(row), final)
    row = server.upload_sessions.finish(row)
    assert server.upload_sessions.claim_notification(row)
    calls = []
    async def send(**kwargs):
        calls.append(kwargs)
        return {"ok": True}
    server._send_to_workbench = send
    route = f"/browser/file/upload/{row['upload_id']}"
    async with TestClient(TestServer(server.app, host="127.0.0.1")) as client:
        status = await client.get(route, headers=_headers(owner))
        assert (await status.json())["notification_status"] == "sending_or_unknown"
        finish = await client.post(route + "/finish", headers=_headers(owner))
        assert (await finish.json())["notification_status"] == "unknown"
        assert calls == []


@pytest.mark.asyncio
async def test_owner_selected_upload_limit_is_enforced_before_staging(tmp_path: Path):
    _store, owner, _foreign, thread, make_server = _setup(tmp_path, upload_max_bytes=3)
    server = make_server()
    async with TestClient(TestServer(server.app, host="127.0.0.1")) as client:
        response = await client.post("/browser/file/upload/start",
                                     json=_metadata(thread, b"four"), headers=_headers(owner))
        assert response.status == 413
        assert (await response.json())["upload_max_bytes"] == 3
        assert list(server.upload_sessions.partial_root.glob("*.part")) == []
