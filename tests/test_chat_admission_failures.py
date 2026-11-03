"""Legacy chat responses never invent admission or invite an uncertain replay."""
from __future__ import annotations

import base64
from unittest.mock import AsyncMock

import pytest
from aiohttp import FormData, web
from aiohttp.test_utils import TestClient, TestServer

from tests.test_session_api import _server


def _payload(mode, session_id):
    fields = {"agent": "lily", "session_id": session_id, "text": "admission receipt"}
    if mode == "json-attachment":
        fields["attachment"] = {"filename": "note.txt", "content_b64": base64.b64encode(b"note").decode(),
                                "media_type": "text/plain"}
        return {"json": fields}
    if mode == "json":
        return {"json": fields}
    form = FormData()
    for key, value in fields.items():
        form.add_field(key, value, content_type="text/plain")
    if mode == "multipart-file":
        form.add_field("files", b"note", filename="note.txt", content_type="text/plain")
    return {"data": form}


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["json", "multipart-text", "multipart-file", "json-attachment"])
@pytest.mark.parametrize("returned", [None, "", 17])
async def test_missing_admission_receipt_is_unknown_even_after_actual_commit(tmp_path, mode, returned):
    server, runtime = _server(tmp_path)
    session = server.session_store.resolve_primary_session(owner_id="user:7", agent_id="lily", establish=True)
    async def committed(*args, **kwargs):
        server.session_store.accept_run(session_id=session["session_id"], owner_id="user:7",
            agent_id="lily", request_id="req-committed-no-receipt", text="admission receipt", source="api",
            idempotency_key="lost-receipt-fixture")
        return returned
    enqueue = AsyncMock(side_effect=committed)
    runtime.enqueue_api_text = runtime.enqueue_api_media = enqueue
    app = web.Application()
    app.router.add_post("/api/chat", server.handle_chat)
    async with TestClient(TestServer(app)) as client:
        response = await client.post("/api/chat", **_payload(mode, session["session_id"]))
        data = await response.json()
    assert response.status == 503 and data["ok"] is False
    assert data["accepted"] is None and data["state"] == "unknown" and data["retry_safe"] is False
    assert data["error_code"] == "chat_admission_unconfirmed" and data["cause_code"] == "receipt_unavailable"
    assert data["diagnostic_ref"].startswith("fcerr_")
    assert data["diagnostic_ref"] in data["error"] and "before" in data["error"].lower()
    assert enqueue.await_count == 1
    assert server.session_store.get_run_by_request("req-committed-no-receipt")["state"] == "queued"


@pytest.mark.asyncio
async def test_multipart_missing_second_receipt_preserves_first_and_stops_later_files(tmp_path):
    server, runtime = _server(tmp_path)
    runtime.enqueue_api_media = AsyncMock(side_effect=["req-first", None, "req-never"])
    form = FormData()
    form.add_field("agent", "lily")
    for name in ("first.txt", "second.txt", "third.txt"):
        form.add_field("files", b"note", filename=name, content_type="text/plain")
    app = web.Application()
    app.router.add_post("/api/chat", server.handle_chat)
    async with TestClient(TestServer(app)) as client:
        response = await client.post("/api/chat", data=form)
        data = await response.json()
    assert response.status == 503 and data["ok"] is False
    assert data["request_ids"] == ["req-first"] and data["accepted"] is None
    assert runtime.enqueue_api_media.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["json", "multipart-text", "multipart-file"])
async def test_admission_exception_has_safe_cause_and_no_automatic_second_attempt(tmp_path, mode):
    server, runtime = _server(tmp_path)
    session = server.session_store.resolve_primary_session(owner_id="user:7", agent_id="lily", establish=True)
    enqueue = AsyncMock(side_effect=PermissionError("private-path token=secret-fixture"))
    runtime.enqueue_api_text = runtime.enqueue_api_media = enqueue
    app = web.Application()
    app.router.add_post("/api/chat", server.handle_chat)
    async with TestClient(TestServer(app)) as client:
        response = await client.post("/api/chat", **_payload(mode, session["session_id"]))
        data = await response.json()
    assert response.status == 503 and data["accepted"] is None and data["retry_safe"] is False
    assert data["cause_code"] == "access_denied" and data["failure"]["phase"] == "admit"
    assert "private-" not in str(data) and "secret-fixture" not in str(data)
    assert enqueue.await_count == 1

@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["json", "multipart-text", "multipart-file", "json-attachment"])
async def test_confirmed_admission_is_not_revoked_by_identity_read_failure(tmp_path, monkeypatch, mode):
    server, runtime = _server(tmp_path)
    session = server.session_store.resolve_primary_session(owner_id="user:7", agent_id="lily", establish=True)
    enqueue = AsyncMock(return_value="req-known-admitted")
    runtime.enqueue_api_text = runtime.enqueue_api_media = enqueue
    def inaccessible(*args, **kwargs):
        raise PermissionError("private-path token=secret-fixture")
    monkeypatch.setattr(server.session_store, "get_run_by_request", inaccessible)
    app = web.Application()
    app.router.add_post("/api/chat", server.handle_chat)
    async with TestClient(TestServer(app)) as client:
        response = await client.post("/api/chat", **_payload(mode, session["session_id"]))
        data = await response.json()
    assert response.status == 200 and data["ok"] is True
    assert data["request_id"] == "req-known-admitted"
    assert data["warning_code"] == "chat_identity_read_failed"
    assert data["identity_warning"]["reason"] == "access_denied"
    assert "private-" not in str(data) and "secret-fixture" not in str(data)
    assert enqueue.await_count == 1
