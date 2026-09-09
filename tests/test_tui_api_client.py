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
