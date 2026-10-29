from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestrator.workbench_api import WorkbenchApiServer


class _FakeRuntime:
    name = "hashiko"

    def __init__(self, *, locale="en"):
        self.sent = []
        self.global_config = SimpleNamespace(
            ui_language=locale,
            authorized_id=123,
        )

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
    runtime.global_config.bridge_home = tmp_path
    global_config = SimpleNamespace(authorized_id=123, instance_id="NOTIFY_TEST", bridge_home=tmp_path, project_root=tmp_path)
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
    server.runtimes = []
    offline = await server.handle_admin_notify(_FakeRequest({"agent": "hashiko", "text": "legacy"}))
    assert offline.status == 404, "legacy notices still require their runtime transport"
    assert runtime.sent == [{"chat_id": 123, "text": "restarted"}]


@pytest.mark.asyncio
@pytest.mark.parametrize("worker_online", [True, False])
async def test_session_notice_retries_after_ack_loss_without_telegram_or_default_fallback(
    tmp_path, worker_online
):
    runtime = _FakeRuntime(locale="en")
    server = _server(tmp_path, runtime)
    server.global_config.authorized_id = 123
    selected = server.session_store.create_session(owner_id="user:123", agent_id="hashiko")
    if not worker_online:
        server.runtimes = []
    origin = {"surface": "workbench", "owner_id": "user:123", "session_id": selected["session_id"],
              "context_generation": 1, "session_channel_key": "default"}
    request = _FakeRequest({"agent": "hashiko", "message_key": "api.restart.completed",
        "message_args": {"instance": "PEER"}, "origin": origin, "ui_locale": "zh-CN",
        "publication_id": "restart:fixture-1"})
    # The first response may be lost. Retrying this publication cannot repeat a
    # restart or create another Message, and needs no Telegram address.
    for _ in range(2):
        response = await server.handle_admin_notify(request)
        assert response.status == 200, response.text
    rows = server.session_store.messages(selected["session_id"], owner_id="user:123")
    assert len(rows) == 1
    assert "PEER 已重启并恢复在线" in rows[0]["text"]
    assert not runtime.sent


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("invalid_scope", "expected_status"),
    [("owner", 403), ("missing_owner", 403), ("agent", 404),
     ("session", 404), ("generation", 409)],
)
async def test_offline_session_notice_validates_scope_without_writes(
    tmp_path, invalid_scope, expected_status
):
    runtime = _FakeRuntime()
    server = _server(tmp_path, runtime)
    server.runtimes = []
    selected = server.session_store.create_session(owner_id="user:123", agent_id="hashiko")
    origin = {"surface": "session-api", "owner_id": "user:123",
              "session_id": selected["session_id"], "context_generation": 1}
    payload = {"agent": "hashiko", "text": "result", "origin": origin,
               "publication_id": "offline-invalid-scope"}
    if invalid_scope == "owner":
        foreign = server.session_store.create_session(owner_id="user:999", agent_id="hashiko")
        origin.update(owner_id="user:999", session_id=foreign["session_id"])
    elif invalid_scope == "missing_owner":
        del origin["owner_id"]
    elif invalid_scope == "agent":
        payload["agent"] = "another-agent"
    elif invalid_scope == "session":
        origin["session_id"] = "ses_missing"
    else:
        origin["context_generation"] = 2
    with server.session_store._connection() as connection:
        before = "\n".join(connection.iterdump())
    response = await server.handle_admin_notify(_FakeRequest(payload))
    assert response.status == expected_status, response.text
    with server.session_store._connection() as connection:
        assert "\n".join(connection.iterdump()) == before
    assert not runtime.sent


@pytest.mark.asyncio
async def test_admin_notify_renders_restart_notice_in_user_locale(tmp_path):
    runtime = _FakeRuntime(locale="zh-CN")
    server = _server(tmp_path, runtime)

    response = await server.handle_admin_notify(
        _FakeRequest(
            {
                "agent": "hashiko",
                "message_key": "api.restart.completed",
                "message_args": {"instance": "HASHI3"},
            }
        )
    )

    payload = json.loads(response.text)
    assert response.status == 200
    assert payload["ok"] is True
    assert runtime.sent == [
        {"chat_id": 123, "text": "✅ HASHI3 已重启并恢复在线。"}
    ]


@pytest.mark.asyncio
async def test_admin_notify_renders_actionable_restart_failure(tmp_path):
    runtime = _FakeRuntime(locale="en")
    server = _server(tmp_path, runtime)

    response = await server.handle_admin_notify(
        _FakeRequest(
            {
                "agent": "hashiko",
                "message_key": "api.restart.failed_detail",
                "message_args": {
                    "instance": "HASHI4",
                    "stage": "terminal verification",
                    "reason": "the new process did not publish a healthy Backend API",
                },
            }
        )
    )

    payload = json.loads(response.text)
    assert response.status == 200
    assert payload["ok"] is True
    assert len(runtime.sent) == 1
    assert "HASHI4" in runtime.sent[0]["text"]
    assert "terminal verification" in runtime.sent[0]["text"]
    assert "the new process did not publish a healthy Backend API" in runtime.sent[0]["text"]


@pytest.mark.asyncio
async def test_admin_notify_renders_degraded_restart_success(tmp_path):
    runtime = _FakeRuntime(locale="en")
    server = _server(tmp_path, runtime)

    response = await server.handle_admin_notify(
        _FakeRequest(
            {
                "agent": "hashiko",
                "message_key": "api.restart.completed_degraded",
                "message_args": {
                    "instance": "HASHI4",
                    "warning": "Remote/HChat discovery is degraded.",
                },
            }
        )
    )

    payload = json.loads(response.text)
    assert response.status == 200
    assert payload["ok"] is True
    assert len(runtime.sent) == 1
    assert "HASHI4" in runtime.sent[0]["text"]
    assert "Local services are online" in runtime.sent[0]["text"]
    assert "Remote/HChat discovery is degraded" in runtime.sent[0]["text"]


@pytest.mark.asyncio
async def test_admin_notify_hides_legacy_restart_diagnostics(tmp_path):
    runtime = _FakeRuntime(locale="zh-CN")
    server = _server(tmp_path, runtime)

    response = await server.handle_admin_notify(
        _FakeRequest(
            {
                "agent": "hashiko",
                "text": (
                    "HASHI restart verified for HASHI3. PID 123 -> 456; "
                    "generation sha256:internal; receipt rst_internal."
                ),
            }
        )
    )

    payload = json.loads(response.text)
    assert response.status == 200
    assert payload["ok"] is True
    assert runtime.sent == [
        {"chat_id": 123, "text": "✅ HASHI3 已重启并恢复在线。"}
    ]


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
