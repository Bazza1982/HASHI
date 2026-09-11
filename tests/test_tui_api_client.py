from __future__ import annotations

import pytest

from tui.api_client import TuiApiClient


@pytest.mark.asyncio
async def test_direct_tui_chat_sends_typed_snapshot(monkeypatch):
    client = TuiApiClient("http://127.0.0.1:18800")
    captured = {}

    async def _request(method, path, *, json_body=None, timeout=10):
        captured.update(
            {"method": method, "path": path, "json": json_body, "timeout": timeout}
        )
        return {"ok": True}

    monkeypatch.setattr(client, "_direct_request", _request)

    await client.send_chat(
        "akane",
        "hello",
        client_id="tui-window-1",
        telegram_mirror=False,
        ui_locale="zh-CN",
    )

    assert captured["method"] == "POST"
    assert captured["path"] == "/api/chat"
    assert captured["json"]["source"] == "tui"
    assert captured["json"]["delivery_policy"]["telegram"]["mirror"] is False
    assert captured["json"]["delivery_policy"]["scope"] == "run"
    assert captured["json"]["ui_locale"] == "zh-CN"


@pytest.mark.asyncio
async def test_direct_tui_attachment_sends_bytes_and_caption_in_one_request(monkeypatch):
    client = TuiApiClient("http://127.0.0.1:18800")
    captured = {}

    async def request(method, path, *, json_body=None, timeout=10):
        captured.update(method=method, path=path, body=json_body, timeout=timeout)
        return {"ok": True, "request_id": "request-1"}

    monkeypatch.setattr(client, "_direct_request", request)
    attachment = {
        "filename": "photo.png",
        "media_type": "image/png",
        "size_bytes": 8,
        "sha256": "digest",
        "content_b64": "iVBORw0KGgo=",
    }

    result = await client.send_chat_attachment(
        "akane",
        "describe it",
        attachment=attachment,
        client_id="tui-1",
        telegram_mirror=False,
    )

    assert result["request_id"] == "request-1"
    assert captured["path"] == "/api/chat"
    assert captured["body"]["text"] == "describe it"
    assert captured["body"]["attachment"] == attachment
    assert captured["body"]["delivery_policy"]["telegram"]["mirror"] is False


@pytest.mark.asyncio
async def test_remote_tui_run_status_uses_typed_proxy_fields(monkeypatch):
    client = TuiApiClient(
        remote_url="http://127.0.0.1:8766",
        target_instance="HASHI2",
    )
    captured = {}

    async def _proxy(operation, **kwargs):
        captured.update({"operation": operation, **kwargs})
        return {"ok": True, "run": {"state": "running"}}

    monkeypatch.setattr(client, "_proxy_request", _proxy)

    result = await client.run_info("session_1", "run_2")

    assert result["run"]["state"] == "running"
    assert captured == {
        "operation": "run_info",
        "session_id": "session_1",
        "run_id": "run_2",
        "timeout": 5,
    }


@pytest.mark.asyncio
async def test_sidepanel_reads_authoritative_direct_endpoints(monkeypatch):
    client = TuiApiClient("http://127.0.0.1:18800")
    calls = []

    async def _request(method, path, *, json_body=None, timeout=10):
        calls.append((method, path, json_body, timeout))
        return {"ok": True}

    monkeypatch.setattr(client, "_direct_request", _request)

    await client.agent_overview("agent name")
    await client.scheduler_jobs("agent name")
    await client.background_jobs("agent name", limit=7)

    assert calls == [
        ("GET", "/api/agents/agent%20name/overview", None, 8),
        ("GET", "/api/agents/agent%20name/scheduler/jobs", None, 8),
        ("GET", "/api/background-jobs?agent=agent%20name&limit=7", None, 8),
    ]


@pytest.mark.asyncio
async def test_sidepanel_reads_use_typed_remote_proxy_operations(monkeypatch):
    client = TuiApiClient(
        remote_url="http://127.0.0.1:8766",
        target_instance="HASHI2",
    )
    calls = []

    async def _proxy(operation, **kwargs):
        calls.append((operation, kwargs))
        return {"ok": True}

    monkeypatch.setattr(client, "_proxy_request", _proxy)

    await client.agent_overview("akane")
    await client.scheduler_jobs("akane")
    await client.background_jobs("akane", limit=7)

    assert calls == [
        ("agent_overview", {"agent": "akane", "timeout": 8}),
        ("scheduler_jobs", {"agent": "akane", "timeout": 8}),
        ("background_jobs", {"agent": "akane", "limit": 7, "timeout": 8}),
    ]
