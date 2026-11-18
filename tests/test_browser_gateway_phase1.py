from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from browser_gateway.server import BrowserGatewayServer
from browser_gateway.store import BrowserGatewayStore
from orchestrator.workbench_api import WorkbenchApiServer


class _FakeRequest:
    def __init__(self, payload: dict, headers: dict | None = None, match_info: dict | None = None):
        self._payload = payload
        self.headers = headers or {}
        self.match_info = match_info or {}

    async def json(self):
        return self._payload


class _FakeRuntime:
    def __init__(self):
        self.name = "demo"
        self._callbacks = {}

    async def enqueue_api_text(self, text: str, source: str = "api", deliver_to_telegram: bool = True):
        self.last = {
            "text": text,
            "source": source,
            "deliver_to_telegram": deliver_to_telegram,
        }
        return "req-123"

    def register_request_listener(self, request_id: str, callback):
        self._callbacks[request_id] = callback
        result = callback(
            {
                "request_id": request_id,
                "success": True,
                "text": "browser completion ok",
                "error": None,
                "source": "browser:test",
                "summary": "demo summary",
            }
        )
        if asyncio.iscoroutine(result):
            asyncio.create_task(result)
        return result


def test_browser_gateway_store_pair_and_thread(tmp_path: Path):
    store = BrowserGatewayStore(tmp_path / "browser_gateway.sqlite")
    pair = store.create_pair_request("Test Browser")
    paired = store.complete_pair(pair.device_id, pair.pairing_code)
    assert paired is not None
    authed = store.authenticate(paired["access_token"])
    assert authed is not None
    thread = store.create_thread(pair.device_id, "lily", title="Test Thread")
    assert thread["agent_id"] == "lily"
    assert thread["instance_id"] == "HASHI"

    ok = store.set_device_recovery(pair.device_id, "hash123", json.dumps({"wrapped_key_b64": "abc"}))
    assert ok is True
    recovery = store.get_device_recovery(pair.device_id, "hash123")
    assert recovery is not None
    assert json.loads(recovery["recovery_payload_json"])["wrapped_key_b64"] == "abc"

    attachment = store.create_attachment(
        attachment_id="att-test123",
        thread_id=thread["thread_id"],
        device_id=pair.device_id,
        filename="hello.txt",
        mime_type="text/plain",
        plaintext_bytes=5,
        ciphertext_bytes=21,
        storage_relpath="oll_uploads/20260426/att-test123.bin",
        encryption_json=json.dumps({"scheme": "AES-GCM", "iv_b64": "xyz"}),
        note="demo",
    )
    assert attachment is not None
    assert attachment["attachment_id"] == "att-test123"
    assert store.list_attachments(thread["thread_id"], pair.device_id)[0]["filename"] == "hello.txt"


def test_browser_gateway_reads_its_instance_identity_from_local_config(tmp_path: Path):
    (tmp_path / "agents.json").write_text(
        json.dumps({"global": {"instance_id": "LOCAL-BRIDGE"}}),
        encoding="utf-8",
    )

    server = BrowserGatewayServer(
        project_root=tmp_path,
        state_db=tmp_path / "browser_gateway.sqlite",
        audit_log=tmp_path / "oll_gateway.audit.jsonl",
    )

    assert server.instance_id == "LOCAL-BRIDGE"


@pytest.mark.asyncio
async def test_workbench_browser_chat_send_awaits_completion(tmp_path: Path):
    config_path = tmp_path / "agents.json"
    config_path.write_text(
        json.dumps(
            {
                "global": {"workbench_port": 18800},
                "agents": [{"name": "demo", "workspace_dir": "workspaces/demo", "type": "flex"}],
            }
        ),
        encoding="utf-8",
    )
    global_config = SimpleNamespace(
        workbench_port=18800,
        project_root=tmp_path,
        bridge_home=tmp_path,
    )
    runtime = _FakeRuntime()
    server = WorkbenchApiServer(config_path=config_path, global_config=global_config, runtimes=[runtime])

    response = await server.handle_browser_chat_send(
        _FakeRequest({"agent": "demo", "text": "hello", "source": "browser:test", "timeout_s": 5})
    )
    payload = json.loads(response.text)
    assert payload["ok"] is True
    assert payload["request_id"] == "req-123"
    assert payload["text"] == "browser completion ok"
    assert runtime.last["deliver_to_telegram"] is True


@pytest.mark.asyncio
async def test_browser_gateway_file_upload_notifies_agent(tmp_path: Path):
    project_root = tmp_path
    store = BrowserGatewayStore(tmp_path / "browser_gateway.sqlite")
    pair = store.create_pair_request("Upload Browser")
    paired = store.complete_pair(pair.device_id, pair.pairing_code)
    thread = store.create_thread(pair.device_id, "akane", title="Upload Thread")

    server = BrowserGatewayServer(
        project_root=project_root,
        state_db=tmp_path / "browser_gateway.sqlite",
        audit_log=tmp_path / "oll_gateway.audit.jsonl",
    )

    async def _fake_send_to_workbench(*, agent: str, text: str, source: str):
        assert agent == "akane"
        assert "attachment_id:" in text
        return {
            "ok": True,
            "request_id": "req-upload-1",
            "text": "attachment reference received",
            "error": None,
            "source": source,
            "summary": "upload ack",
        }

    server._send_to_workbench = _fake_send_to_workbench  # type: ignore[method-assign]
    payload = {
        "thread_id": thread["thread_id"],
        "filename": "report.pdf",
        "mime_type": "application/pdf",
        "plaintext_bytes": 7,
        "ciphertext_b64": base64.b64encode(b"ciphertext-demo").decode("ascii"),
        "encryption": {"scheme": "AES-GCM", "iv_b64": "demo"},
        "note": "phase2",
        "notify_agent": True,
    }
    response = await server.handle_file_upload(
        _FakeRequest(payload, headers={"Authorization": f"Bearer {paired['access_token']}"})
    )
    body = json.loads(response.text)
    assert body["ok"] is True
    assert body["attachment"]["filename"] == "report.pdf"
    assert body["agent_result"]["text"] == "attachment reference received"

    attachments = store.list_attachments(thread["thread_id"], pair.device_id)
    assert len(attachments) == 1
    stored = project_root / "state" / attachments[0]["storage_relpath"]
    assert stored.exists()
    assert stored.read_bytes() == b"ciphertext-demo"



@pytest.mark.asyncio
async def test_gateway_device_rate_classes_and_recoverable_backpressure(tmp_path: Path):
    from aiohttp.test_utils import TestClient, TestServer

    store = BrowserGatewayStore(tmp_path / "browser_gateway.sqlite")
    pair = store.create_pair_request("Rate test")
    paired = store.complete_pair(pair.device_id, pair.pairing_code)
    other = store.create_pair_request("Other device")
    other_paired = store.complete_pair(other.device_id, other.pairing_code)
    thread = store.create_thread(pair.device_id, "akane", title="Rate test")
    server = BrowserGatewayServer(
        project_root=tmp_path,
        state_db=tmp_path / "browser_gateway.sqlite",
        audit_log=tmp_path / "oll_gateway.audit.jsonl",
        chat_requests_per_minute=2,
        upload_requests_per_minute=10,
        device_rate_limits={pair.device_id: {"upload": 40}},
    )
    now = [100.0]
    server.chat_rate_limiter._clock = lambda: now[0]
    server.upload_rate_limiter._clock = lambda: now[0]
    async def fake_send(*, agent, text, source):
        return {"ok": True, "request_id": "synthetic", "text": "done", "source": source}
    server._send_to_workbench = fake_send
    headers = {"Authorization": f"Bearer {paired['access_token']}"}
    upload = {"thread_id": thread["thread_id"], "filename": "a.bin",
              "ciphertext_b64": base64.b64encode(b"encrypted").decode("ascii"),
              "notify_agent": False}
    async with TestClient(TestServer(server.app, host="127.0.0.1")) as client:
        unauthorized = await client.post("/browser/file/upload", json=upload)
        assert unauthorized.status == 401
        foreign = await client.post("/browser/file/upload", json=upload, headers={
            "Authorization": f"Bearer {other_paired['access_token']}"})
        assert foreign.status == 404
        for _ in range(40):
            response = await client.post("/browser/file/upload", json=upload, headers=headers)
            assert response.status == 200
        limited_upload = await client.post("/browser/file/upload", json=upload, headers=headers)
        assert limited_upload.status == 429
        assert limited_upload.headers["Retry-After"] == "60"
        assert len(store.list_attachments(thread["thread_id"], pair.device_id)) == 40
        invalid = await client.post("/browser/chat/send", json={
            "thread_id": thread["thread_id"], "text": ""}, headers=headers)
        assert invalid.status == 400
        for _ in range(2):
            response = await client.post("/browser/chat/send", json={
                "thread_id": thread["thread_id"], "text": "hello"}, headers=headers)
            assert response.status == 200
        limited = await client.post("/browser/chat/send", json={
            "thread_id": thread["thread_id"], "text": "hello again"}, headers=headers)
        assert limited.status == 429
        assert limited.headers["Retry-After"] == "60"
        assert (await limited.json())["retry_after_seconds"] == 60
        status = await client.get("/browser/device/status", headers=headers)
        effective = (await status.json())["ingress_limits"]
        assert effective["chat_requests"] == 2
        assert effective["upload_requests"] == 40
        assert effective["json_body_bytes"] == 32 * 1024 * 1024
        now[0] += 60
        retried = await client.post("/browser/chat/send", json={
            "thread_id": thread["thread_id"], "text": "retry"}, headers=headers)
        assert retried.status == 200


def test_gateway_rates_are_optional_and_validate_selected_limits():
    from browser_gateway.server import RateLimiter

    limiter = RateLimiter()
    assert all(limiter.admit("device") is None for _ in range(100))
    assert limiter._events == {}
    with pytest.raises(ValueError):
        RateLimiter(-1)
    with pytest.raises(ValueError):
        RateLimiter(per_device={"device": True})
