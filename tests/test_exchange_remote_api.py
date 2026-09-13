from __future__ import annotations

import json

from fastapi.testclient import TestClient

from remote.api.server import create_app
from remote.security.pairing import PairingManager
from remote.security.shared_token import build_auth_headers
from remote.terminal.executor import TerminalExecutor


class _ExchangeTransportStub:
    def __init__(self):
        self.calls = []

    def status(self):
        return {
            "ok": True,
            "enabled": True,
            "connected": True,
            "state": "ready",
            "authority_id": "authority_1",
            "registered_instance_id": "instance_local",
            "instance_alias": "home",
            "published_agents": ["planner"],
        }

    async def send_message(self, **kwargs):
        self.calls.append(kwargs)
        return {
            "ok": True,
            "state": "accepted",
            "message_id": kwargs.get("message_id") or "generated_message",
            "conversation_id": (
                kwargs.get("conversation_id") or "generated_conversation"
            ),
        }


def _app(tmp_path):
    token = "synthetic-local-shared-token"
    (tmp_path / "secrets.json").write_text(
        json.dumps({"hashi_remote_shared_token": token}),
        encoding="utf-8",
    )
    transport = _ExchangeTransportStub()
    app = create_app(
        {
            "instance_id": "HASHI_LOCAL",
            "display_name": "Local",
            "remote_port": 8766,
        },
        PairingManager(storage_dir=tmp_path / "pairing", lan_mode=True),
        TerminalExecutor(),
        exchange_transport=transport,
        hashi_root=str(tmp_path),
        workbench_port=18800,
    )
    return app, transport, token


def _payload(**overrides):
    value = {
        "from_instance": "HASHI_LOCAL",
        "from_agent": "planner",
        "to_address": "reviewer@server.alice",
        "text": "Please review.",
        "message_id": "message_1",
        "conversation_id": "conversation_1",
        "message_type": "agent_message",
        "in_reply_to": None,
        "expires_in_seconds": 600,
        "authorization_resources": [],
        "private_authorization_proofs": [],
    }
    value.update(overrides)
    return value


def _signed_headers(
    *,
    token: str,
    method: str,
    path: str,
    from_instance: str,
    body: bytes = b"",
):
    headers = {"Content-Type": "application/json"}
    headers.update(
        build_auth_headers(
            shared_token=token,
            method=method,
            path=path,
            from_instance=from_instance,
            body_bytes=body,
        )
    )
    return headers


def test_exchange_message_requires_loopback_and_valid_shared_hmac(tmp_path):
    app, transport, token = _app(tmp_path)
    client = TestClient(app, client=("127.0.0.1", 50123))
    body = json.dumps(_payload()).encode("utf-8")

    unauthenticated = client.post(
        "/exchange/message",
        content=body,
        headers={"Content-Type": "application/json"},
    )
    authenticated = client.post(
        "/exchange/message",
        content=body,
        headers=_signed_headers(
            token=token,
            method="POST",
            path="/exchange/message",
            from_instance="HASHI_LOCAL",
            body=body,
        ),
    )

    assert unauthenticated.status_code == 401
    assert authenticated.status_code == 200
    assert authenticated.json()["state"] == "accepted"
    assert len(transport.calls) == 1
    assert transport.calls[0]["to_address"] == "reviewer@server.alice"


def test_exchange_message_rejects_a_signed_nonlocal_instance(tmp_path):
    app, transport, token = _app(tmp_path)
    client = TestClient(app, client=("127.0.0.1", 50123))
    body = json.dumps(_payload(from_instance="HASHI_OTHER")).encode("utf-8")

    response = client.post(
        "/exchange/message",
        content=body,
        headers=_signed_headers(
            token=token,
            method="POST",
            path="/exchange/message",
            from_instance="HASHI_OTHER",
            body=body,
        ),
    )

    assert response.status_code == 403
    assert transport.calls == []


def test_exchange_message_never_uses_loopback_as_authentication(tmp_path):
    app, transport, token = _app(tmp_path)
    client = TestClient(app, client=("198.51.100.20", 50123))
    body = json.dumps(_payload()).encode("utf-8")

    response = client.post(
        "/exchange/message",
        content=body,
        headers=_signed_headers(
            token=token,
            method="POST",
            path="/exchange/message",
            from_instance="HASHI_LOCAL",
            body=body,
        ),
    )

    assert response.status_code == 403
    assert transport.calls == []


def test_exchange_message_refuses_unnegotiated_private_proof(tmp_path):
    app, transport, token = _app(tmp_path)
    client = TestClient(app, client=("127.0.0.1", 50123))
    body = json.dumps(
        _payload(
            authorization_resources=["finance"],
            private_authorization_proofs=[{"version": 1}],
        )
    ).encode("utf-8")

    response = client.post(
        "/exchange/message",
        content=body,
        headers=_signed_headers(
            token=token,
            method="POST",
            path="/exchange/message",
            from_instance="HASHI_LOCAL",
            body=body,
        ),
    )

    assert response.status_code == 409
    assert response.json()["code"] == "UNSUPPORTED_CAPABILITY"
    assert transport.calls == []


def test_exchange_status_is_also_hmac_protected(tmp_path):
    app, _transport, token = _app(tmp_path)
    client = TestClient(app, client=("127.0.0.1", 50123))

    unauthenticated = client.get("/exchange/status")
    authenticated = client.get(
        "/exchange/status",
        headers=_signed_headers(
            token=token,
            method="GET",
            path="/exchange/status",
            from_instance="HASHI_LOCAL",
        ),
    )

    assert unauthenticated.status_code == 401
    assert authenticated.status_code == 200
    assert authenticated.json()["state"] == "ready"
