from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestrator.workbench_api import WorkbenchApiServer


class _FakeRuntime:
    name = "hashiko"

    def __init__(self):
        self.sent = []

    def _primary_chat_id(self):
        return 123

    async def _send_text(self, chat_id, text, **kwargs):
        self.sent.append({"chat_id": chat_id, "text": text, **kwargs})


class _FakeRequest:
    can_read_body = True

    def __init__(self, payload: dict, *, token: str = "secret"):
        self._payload = payload
        self.headers = {"X-Workbench-Token": token}

    async def json(self):
        return self._payload


def _server(tmp_path: Path, runtime: _FakeRuntime) -> WorkbenchApiServer:
    config_path = tmp_path / "agents.json"
    config_path.write_text(json.dumps({"agents": [{"name": "hashiko"}]}), encoding="utf-8")
    global_config = SimpleNamespace()
    return WorkbenchApiServer(
        config_path=config_path,
        global_config=global_config,
        runtimes=[runtime],
        secrets={"workbench_admin_token": "secret"},
    )


@pytest.mark.asyncio
async def test_admin_notify_sends_text_to_primary_chat(tmp_path):
    runtime = _FakeRuntime()
    server = _server(tmp_path, runtime)

    response = await server.handle_admin_notify(_FakeRequest({"agent": "hashiko", "text": "restarted"}))

    payload = json.loads(response.text)
    assert response.status == 200
    assert payload["ok"] is True
    assert runtime.sent == [{"chat_id": 123, "text": "restarted"}]


@pytest.mark.asyncio
async def test_admin_notify_requires_auth(tmp_path):
    runtime = _FakeRuntime()
    server = _server(tmp_path, runtime)

    response = await server.handle_admin_notify(
        _FakeRequest({"agent": "hashiko", "text": "restarted"}, token="wrong")
    )

    payload = json.loads(response.text)
    assert response.status == 403
    assert payload["ok"] is False
    assert runtime.sent == []


@pytest.mark.asyncio
async def test_admin_notify_requires_text(tmp_path):
    runtime = _FakeRuntime()
    server = _server(tmp_path, runtime)

    response = await server.handle_admin_notify(_FakeRequest({"agent": "hashiko"}))

    payload = json.loads(response.text)
    assert response.status == 400
    assert payload["error"] == "text is required"
