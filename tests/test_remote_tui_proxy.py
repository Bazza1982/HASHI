from __future__ import annotations

import json
from types import SimpleNamespace

from fastapi.testclient import TestClient

import remote.api.server as remote_server
from orchestrator.frontend_delivery import tui_run_delivery_policy
from remote.api.server import ProtocolTuiRequest, create_app
from remote.security.pairing import PairingManager
from remote.security.shared_token import build_auth_headers
from remote.terminal.executor import TerminalExecutor


class _Registry:
    def __init__(self, peer):
        self.peer = peer

    def get_peer(self, instance_id: str):
        return self.peer if instance_id.upper() == self.peer.instance_id else None

    def get_peers(self):
        return [self.peer]


class _Protocol:
    display_handle = "@local"

    def __init__(self, peer):
        self.peer = peer

    def get_protocol_status(self):
        return {"protocol_version": "2.0", "capabilities": ["tui_proxy_v1"]}

    def get_peer_view(self, peer):
        return peer.__dict__

    def get_local_agents_snapshot(self):
        return []

    def get_local_agent_directory_state(self):
        return {"directory_state": "fresh"}

    def resolve_forward_urls(self, instance_id: str, path: str):
        return [f"http://peer.invalid:8767{path}"] if instance_id == self.peer.instance_id else []


def _peer(*, handshake: str = "handshake_accepted", live: str = "online"):
    return SimpleNamespace(
        instance_id="HASHI2",
        capabilities=["handshake_v2", "tui_proxy_v1"],
        properties={"handshake_state": handshake, "live_status": live},
    )


def _client(tmp_path, peer=None):
    token = "shared-secret"
    (tmp_path / "secrets.json").write_text(
        json.dumps({"hashi_remote_shared_token": token}),
        encoding="utf-8",
    )
    peer = peer or _peer()
    registry = _Registry(peer)
    protocol = _Protocol(peer)
    app = create_app(
        {"instance_id": "HASHI1", "display_name": "Local", "remote_port": 8766},
        PairingManager(storage_dir=tmp_path / "pairing", lan_mode=False),
        TerminalExecutor(),
        peer_registry=registry,
        protocol_manager=protocol,
        hashi_root=str(tmp_path),
        workbench_port=18800,
    )
    return TestClient(app, client=("127.0.0.1", 50123)), token


def test_protocol_tui_requires_hmac_and_accepted_handshake(tmp_path, monkeypatch):
    client, token = _client(tmp_path)
    monkeypatch.setattr(
        remote_server,
        "_local_workbench_tui_request",
        lambda payload: (200, {"ok": True, "instance_id": "HASHI1"}),
    )
    payload = {"from_instance": "HASHI2", "operation": "health"}

    unsigned = client.post("/protocol/tui", json=payload)
    assert unsigned.status_code == 401

    body = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    headers.update(
        build_auth_headers(
            shared_token=token,
            method="POST",
            path="/protocol/tui",
            from_instance="HASHI2",
            body_bytes=body,
        )
    )
    response = client.post("/protocol/tui", content=body, headers=headers)

    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "target_instance": "HASHI1",
        "result": {"ok": True, "instance_id": "HASHI1"},
    }


def test_protocol_tui_rejects_peer_without_completed_handshake(tmp_path):
    client, token = _client(tmp_path, _peer(handshake="handshake_pending"))
    payload = {"from_instance": "HASHI2", "operation": "agents"}
    body = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    headers.update(
        build_auth_headers(
            shared_token=token,
            method="POST",
            path="/protocol/tui",
            from_instance="HASHI2",
            body_bytes=body,
        )
    )

    response = client.post("/protocol/tui", content=body, headers=headers)

    assert response.status_code == 409
    assert response.json()["error"] == "handshake_required"


def test_loopback_tui_proxy_forwards_only_to_verified_identity(tmp_path, monkeypatch):
    client, _token = _client(tmp_path)
    monkeypatch.setattr(
        remote_server,
        "_post_json_with_optional_hmac",
        lambda url, payload, timeout=15: {
            "ok": True,
            "target_instance": "HASHI2",
            "result": {"ok": True, "agents": []},
        },
    )

    response = client.post(
        "/tui/proxy",
        json={"target_instance": "HASHI2", "operation": "agents"},
    )

    assert response.status_code == 200
    assert response.json()["target_instance"] == "HASHI2"


def test_loopback_tui_proxy_rejects_identity_mismatch(tmp_path, monkeypatch):
    client, _token = _client(tmp_path)
    monkeypatch.setattr(
        remote_server,
        "_post_json_with_optional_hmac",
        lambda url, payload, timeout=15: {
            "ok": True,
            "target_instance": "HASHI9",
            "result": {"ok": True},
        },
    )

    response = client.post(
        "/tui/proxy",
        json={"target_instance": "HASHI2", "operation": "health"},
    )

    assert response.status_code == 502
    assert response.json()["error"] == "target_identity_mismatch"


def test_tui_proxy_allowlist_rejects_arbitrary_workbench_operation(tmp_path):
    client, _token = _client(tmp_path)

    response = client.post(
        "/tui/proxy",
        json={"target_instance": "HASHI2", "operation": "admin_delete"},
    )

    assert response.status_code == 400
    assert response.json()["error"] == "operation_not_allowed"


def test_tui_proxy_forwards_typed_run_delivery_policy_without_text_inference(
    tmp_path,
    monkeypatch,
):
    client, _token = _client(tmp_path)
    captured = {}

    def _forward(_url, payload, timeout=15):
        captured.update(payload)
        return {
            "ok": True,
            "target_instance": "HASHI2",
            "result": {"ok": True, "request_id": "req-1"},
        }

    monkeypatch.setattr(remote_server, "_post_json_with_optional_hmac", _forward)
    policy = tui_run_delivery_policy(
        telegram_mirror=False,
        client_id="tui-window-1",
    )

    response = client.post(
        "/tui/proxy",
        json={
            "target_instance": "HASHI2",
            "operation": "chat",
            "agent": "akane",
            "text": "the words do not encode policy",
            "client_id": "tui-window-1",
            "ui_locale": "zh-CN",
            "delivery_policy": policy,
        },
    )

    assert response.status_code == 200
    assert captured["delivery_policy"] == policy
    assert captured["client_id"] == "tui-window-1"
    assert captured["text"] == "the words do not encode policy"


def test_tui_proxy_rejects_invalid_policy_and_accepts_typed_run_identity():
    invalid = ProtocolTuiRequest(
        from_instance="HASHI1",
        operation="chat",
        agent="akane",
        text="hello",
        client_id="tui-window-1",
        delivery_policy={"telegram": {"mirror": False}},
    )
    valid_run = ProtocolTuiRequest(
        from_instance="HASHI1",
        operation="run_info",
        session_id="session_123",
        run_id="run_456",
    )

    assert remote_server._validate_tui_proxy_payload(invalid) == (
        False,
        "invalid_delivery_policy",
    )
    assert remote_server._validate_tui_proxy_payload(valid_run) == (True, "ok")


def test_tui_proxy_accepts_only_agent_scoped_sidepanel_reads():
    for operation in ("agent_overview", "scheduler_jobs", "background_jobs"):
        missing_agent = ProtocolTuiRequest(
            from_instance="HASHI1",
            operation=operation,
        )
        valid = ProtocolTuiRequest(
            from_instance="HASHI1",
            operation=operation,
            agent="akane",
            limit=7,
        )

        assert remote_server._validate_tui_proxy_payload(missing_agent) == (
            False,
            "invalid_agent",
        )
        assert remote_server._validate_tui_proxy_payload(valid) == (True, "ok")


def test_sidepanel_proxy_maps_to_read_only_workbench_paths(monkeypatch):
    captured = []

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit):
            return b'{"ok":true}'

    def urlopen(request, timeout):
        captured.append((request.get_method(), request.full_url, timeout))
        return Response()

    monkeypatch.setattr(remote_server, "local_http_hosts", lambda: ("127.0.0.1",))
    monkeypatch.setattr(remote_server.urllib_request, "urlopen", urlopen)

    for operation in ("agent_overview", "scheduler_jobs", "background_jobs"):
        status, result = remote_server._local_workbench_tui_request(
            ProtocolTuiRequest(
                from_instance="HASHI1",
                operation=operation,
                agent="agent name",
                limit=7,
            )
        )
        assert status == 200
        assert result == {"ok": True}

    assert [item[0] for item in captured] == ["GET", "GET", "GET"]
    assert captured[0][1].endswith("/api/agents/agent%20name/overview")
    assert captured[1][1].endswith("/api/agents/agent%20name/scheduler/jobs")
    assert captured[2][1].endswith("/api/background-jobs?agent=agent%20name&limit=7")
