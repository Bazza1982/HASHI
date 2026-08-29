from __future__ import annotations

import json
import time
from pathlib import Path

from fastapi.testclient import TestClient

from remote.api import server as remote_server
from remote.api.server import create_app
from remote.security.pairing import PairingManager
from remote.security.shared_token import build_auth_headers, canonical_request_target
from remote.terminal.executor import TerminalExecutor


class _ProtocolStub:
    def get_protocol_status(self):
        return {"capabilities": []}


def _client(tmp_path: Path, *, lan_mode: bool = False) -> tuple[TestClient, str]:
    token = "shared-secret"
    (tmp_path / "secrets.json").write_text(
        json.dumps({"hashi_remote_shared_token": token}),
        encoding="utf-8",
    )
    app = create_app(
        {
            "instance_id": "HASHI1",
            "display_name": "HASHI One",
            "remote_port": 8766,
            "workbench_port": 18800,
        },
        PairingManager(storage_dir=tmp_path / "pairing", lan_mode=lan_mode),
        TerminalExecutor(),
        protocol_manager=_ProtocolStub(),
        hashi_root=str(tmp_path),
        workbench_port=18800,
    )
    return TestClient(app), token


def _signed_headers(
    token: str,
    *,
    method: str,
    path: str,
    query: str = "",
    body: bytes = b"",
) -> dict[str, str]:
    target = canonical_request_target(path, query)
    return build_auth_headers(
        shared_token=token,
        method=method,
        path=target,
        from_instance="WORKBENCH",
        body_bytes=body,
        timestamp=int(time.time()),
    )


def test_gateway_status_requires_shared_token_even_in_lan_mode(tmp_path, monkeypatch):
    client, token = _client(tmp_path, lan_mode=True)
    monkeypatch.setattr(remote_server, "_fetch_workbench_health", lambda timeout=1.0: {"ok": True})

    unauthenticated = client.get("/workbench/v1/status")
    authenticated = client.get(
        "/workbench/v1/status",
        headers=_signed_headers(token, method="GET", path="/workbench/v1/status"),
    )

    assert unauthenticated.status_code == 401
    assert authenticated.status_code == 200
    payload = authenticated.json()
    assert payload["gateway"] == "workbench_v1"
    assert payload["authenticated_instance"] == "WORKBENCH"
    assert payload["instance"]["instance_id"] == "HASHI1"
    assert payload["workbench_online"] is True


def test_gateway_proxy_authenticates_exact_body_and_forwards_api_request(tmp_path, monkeypatch):
    client, token = _client(tmp_path)
    captured: dict = {}

    def fake_forward(**kwargs):
        captured.update(kwargs)
        return 202, b'{"ok":true}', {"content-type": "application/json"}

    monkeypatch.setattr(remote_server, "_forward_workbench_gateway_request", fake_forward)
    body = b'{"agent":"lily","text":"hello"}'
    path = "/workbench/v1/proxy/api/chat"
    query = "z=2&a=hello+world"
    response = client.post(
        f"{path}?{query}",
        content=body,
        headers={
            "Content-Type": "application/json",
            **_signed_headers(
                token,
                method="POST",
                path=path,
                query=query,
                body=body,
            ),
        },
    )

    assert response.status_code == 202
    assert response.json() == {"ok": True}
    assert captured["method"] == "POST"
    assert captured["api_path"] == "chat"
    assert captured["query"] == query
    assert captured["body_bytes"] == body


def test_gateway_proxy_rejects_missing_or_wrong_shared_token(tmp_path, monkeypatch):
    client, token = _client(tmp_path, lan_mode=True)
    monkeypatch.setattr(
        remote_server,
        "_forward_workbench_gateway_request",
        lambda **_kwargs: (200, b"{}", {"content-type": "application/json"}),
    )
    path = "/workbench/v1/proxy/api/agents"

    assert client.get(path).status_code == 401
    wrong = _signed_headers("wrong-token", method="GET", path=path)
    assert client.get(path, headers=wrong).status_code == 401
    valid = _signed_headers(token, method="GET", path=path)
    assert client.get(path, headers=valid).status_code == 200


def test_forwarder_keeps_gateway_auth_out_of_loopback_and_injects_local_admin(monkeypatch):
    captured: dict = {}

    class _Response:
        status = 200
        headers = {"Content-Type": "application/json", "Server": "hidden"}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b'{"ok":true}'

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["method"] = request.get_method()
        captured["body"] = request.data
        captured["headers"] = {key.lower(): value for key, value in request.header_items()}
        captured["timeout"] = timeout
        return _Response()

    monkeypatch.setattr(remote_server, "local_http_hosts", lambda: ("127.0.0.1",))
    monkeypatch.setattr(remote_server, "_workbench_admin_token", lambda: "local-admin")
    monkeypatch.setattr(remote_server.urllib_request, "urlopen", fake_urlopen)

    status, body, headers = remote_server._forward_workbench_gateway_request(
        method="POST",
        api_path="chat",
        query="source=workbench",
        body_bytes=b'{"text":"hello"}',
        request_headers={
            "content-type": "application/json",
            "x-hashi-digest": "must-not-leak",
        },
    )

    assert status == 200
    assert body == b'{"ok":true}'
    assert headers == {"content-type": "application/json"}
    assert captured["url"] == "http://127.0.0.1:18800/api/chat?source=workbench"
    assert captured["body"] == b'{"text":"hello"}'
    assert captured["headers"]["x-workbench-token"] == "local-admin"
    assert "x-hashi-digest" not in captured["headers"]
