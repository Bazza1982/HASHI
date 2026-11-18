from __future__ import annotations

import json
import base64
import logging
import time
from pathlib import Path
from urllib.error import URLError

import pytest

from fastapi.testclient import TestClient

from orchestrator.message_context import (
    CONNECTOR_EVIDENCE_METADATA_KEY,
    NETWORK_INGRESS_SCOPE_CLAIM_KEY,
    REMOTE_WORKBENCH_INGRESS_SCOPE,
    verify_connector_evidence,
)
from remote.api import server as remote_server
from remote.api.server import create_app
from remote.security.pairing import PairingManager
from remote.security.shared_token import (
    HEADER_NONCE,
    build_auth_headers,
    canonical_request_target,
    verify_response_auth,
)
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


def _gateway_events(caplog, event: str | None = None):
    rows = [
        json.loads(record.getMessage().removeprefix("workbench gateway "))
        for record in caplog.records
        if record.getMessage().startswith("workbench gateway ")
    ]
    return [row for row in rows if event is None or row["event"] == event]


def test_gateway_status_requires_shared_token_even_in_lan_mode(tmp_path, monkeypatch):
    client, token = _client(tmp_path, lan_mode=True)
    monkeypatch.setattr(
        remote_server, "_fetch_workbench_health", lambda timeout=1.0: {"ok": True}
    )

    unauthenticated = client.get("/workbench/v1/status")
    headers = _signed_headers(token, method="GET", path="/workbench/v1/status")
    authenticated = client.get("/workbench/v1/status", headers=headers)

    assert unauthenticated.status_code == 401
    assert authenticated.status_code == 200
    payload = authenticated.json()
    response_auth = payload.pop("response_auth")
    assert (
        verify_response_auth(
            shared_token=token,
            request_nonce=headers[HEADER_NONCE],
            payload=payload,
            response_auth=response_auth,
        )
        is True
    )
    assert payload["gateway"] == "workbench_v1"
    assert payload["authenticated_instance"] == "WORKBENCH"
    assert payload["instance"]["instance_id"] == "HASHI1"
    assert payload["workbench_online"] is True


def test_gateway_proxy_authenticates_exact_body_and_forwards_api_request(
    tmp_path, monkeypatch, caplog
):
    caplog.set_level(logging.DEBUG, logger=remote_server.logger.name)
    client, token = _client(tmp_path)
    captured: dict = {}

    def fake_forward(**kwargs):
        captured.update(kwargs)
        return 202, b'{"ok":true}', {"content-type": "application/json"}

    monkeypatch.setattr(
        remote_server, "_forward_workbench_gateway_request", fake_forward
    )
    body = b'{"agent":"lily","text":"hello"}'
    path = "/workbench/v1/proxy/api/chat"
    query = "z=2&a=hello+world"
    response = client.post(
        f"{path}?{query}",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Hashi-Correlation-Id": "call-request-1",
            "X-Hashi-Trace-Id": "call-trace-1",
            "X-Hashi-Span-Id": "workbench-span-1",
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
    assert captured["request_headers"]["x-hashi-correlation-id"] == "call-request-1"
    assert captured["request_headers"]["x-hashi-trace-id"] == "call-trace-1"
    assert response.headers["x-hashi-correlation-id"] == "call-request-1"
    assert response.headers["x-hashi-trace-id"] == "call-trace-1"
    names = [row["event"] for row in _gateway_events(caplog)]
    assert (
        names.index("proxy_received")
        < names.index("proxy_authenticated")
        < names.index("proxy_response_ready")
    )
    assert names.index("proxy_response_ready") < names.index("asgi_response_completed")
    info_names = [
        json.loads(record.getMessage().removeprefix("workbench gateway "))["event"]
        for record in caplog.records
        if record.levelno >= logging.INFO
        and record.getMessage().startswith("workbench gateway ")
    ]
    assert info_names == ["asgi_response_completed"]


def test_gateway_verbose_stage_keeps_failure_at_info(caplog):
    caplog.set_level(logging.INFO, logger=remote_server.logger.name)

    remote_server._workbench_gateway_log(
        "proxy_response_ready", status=503, error_code="workbench_unavailable"
    )

    assert _gateway_events(caplog) == [
        {
            "error_code": "workbench_unavailable",
            "event": "proxy_response_ready",
            "status": 503,
        }
    ]


def test_gateway_proxy_seals_remote_scope_for_session_run_after_authentication(
    tmp_path, monkeypatch
):
    client, token = _client(tmp_path)
    captured: dict = {}

    def fake_forward(**kwargs):
        captured.update(kwargs)
        return 202, b'{"ok":true}', {"content-type": "application/json"}

    monkeypatch.setattr(
        remote_server, "_forward_workbench_gateway_request", fake_forward
    )
    prompt = "delayed remote message"
    payload = {
        "idempotency_key": "remote-delay-idempotency",
        "originated_at": "2026-10-16T00:00:00Z",
        "message": {"content": [{"type": "text", "text": prompt}]},
        "request_metadata": {
            "client_fact": "preserved",
            CONNECTOR_EVIDENCE_METADATA_KEY: {"claims": {"forged": True}},
        },
    }
    body = json.dumps(payload).encode("utf-8")
    path = "/workbench/v1/proxy/api/v1/sessions/ses-1/runs"
    response = client.post(
        path,
        content=body,
        headers={
            "Content-Type": "application/json",
            **_signed_headers(token, method="POST", path=path, body=body),
        },
    )

    assert response.status_code == 202
    forwarded = json.loads(captured["body_bytes"])
    assert forwarded["request_metadata"]["client_fact"] == "preserved"
    evidence = forwarded["request_metadata"][CONNECTOR_EVIDENCE_METADATA_KEY]
    assert evidence != payload["request_metadata"][CONNECTOR_EVIDENCE_METADATA_KEY]
    claims = verify_connector_evidence(tmp_path, evidence=evidence, prompt=prompt)
    assert claims == {
        NETWORK_INGRESS_SCOPE_CLAIM_KEY: REMOTE_WORKBENCH_INGRESS_SCOPE,
        "_origin_instance_evidence": {
            "id": "WORKBENCH",
            "assurance": "shared_network_hmac",
        },
    }


def test_gateway_proxy_authenticates_bridge_transfer_response(tmp_path, monkeypatch):
    client, token = _client(tmp_path)
    captured = {}
    body = b'{"transfer_id":"trf-1"}'
    path = "/workbench/v1/proxy/api/bridge/transfer"
    headers = {
        "Content-Type": "application/json",
        **_signed_headers(token, method="POST", path=path, body=body),
    }

    def forward(**kwargs):
        captured.update(kwargs)
        return (
            200,
            b'{"ok":true,"status":"accepted"}',
            {"content-type": "application/json"},
        )

    monkeypatch.setattr(remote_server, "_forward_workbench_gateway_request", forward)

    response = client.post(path, content=body, headers=headers)

    assert response.status_code == 200
    payload = response.json()
    response_auth = payload.pop("response_auth")
    assert payload == {"ok": True, "status": "accepted"}
    assert (
        verify_response_auth(
            shared_token=token,
            request_nonce=headers[HEADER_NONCE],
            payload=payload,
            response_auth=response_auth,
        )
        is True
    )
    assert captured["timeout"] == remote_server._BRIDGE_HANDOFF_PROXY_TIMEOUT_SECONDS
    assert captured["timeout"] < 3600


def test_gateway_proxy_uses_agent_lifecycle_budget_for_active_route(
    tmp_path, monkeypatch
):
    from orchestrator.agent_lifecycle import AGENT_LIFECYCLE_REQUEST_TIMEOUT_SECONDS

    client, token = _client(tmp_path)
    captured: dict = {}

    def fake_forward(**kwargs):
        captured.update(kwargs)
        return 200, b'{"ok":true}', {"content-type": "application/json"}

    monkeypatch.setattr(
        remote_server, "_forward_workbench_gateway_request", fake_forward
    )
    body = b'{"is_active":true}'
    path = "/workbench/v1/proxy/api/agents/phd_1/active"
    response = client.post(
        path,
        content=body,
        headers={
            "Content-Type": "application/json",
            **_signed_headers(
                token,
                method="POST",
                path=path,
                body=body,
            ),
        },
    )

    assert response.status_code == 200
    assert captured["timeout"] == AGENT_LIFECYCLE_REQUEST_TIMEOUT_SECONDS
    assert captured["timeout"] > remote_server._workbench_gateway_timeout()


def test_gateway_proxy_keeps_generic_requests_on_generic_budget(tmp_path, monkeypatch):
    client, token = _client(tmp_path)
    captured: dict = {}

    def fake_forward(**kwargs):
        captured.update(kwargs)
        return 200, b'{"ok":true}', {"content-type": "application/json"}

    monkeypatch.setattr(
        remote_server, "_forward_workbench_gateway_request", fake_forward
    )
    path = "/workbench/v1/proxy/api/agents"
    response = client.get(
        path,
        headers=_signed_headers(token, method="GET", path=path),
    )

    assert response.status_code == 200
    assert captured["timeout"] is None


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


def test_signed_file_content_survives_the_actual_gateway_and_rejects_policy_tampering(
    tmp_path, monkeypatch
):
    client, token = _client(tmp_path)
    captured = []
    body = json.dumps(
        {
            "filename": "fixture.wav",
            "operation": "transcribe",
            "allow_compute": False,
            "payload": base64.b64encode(b"synthetic audio").decode("ascii"),
        },
        separators=(",", ":"),
    ).encode("utf-8")

    class Response:
        status = 200
        headers = {"Content-Type": "application/json"}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b'{"ok":true}'

    def forward(request, timeout):
        captured.append(
            {
                "body": request.data,
                "headers": {
                    key.lower(): value for key, value in request.header_items()
                },
            }
        )
        return Response()

    monkeypatch.setattr(remote_server, "local_http_hosts", lambda: ("127.0.0.1",))
    monkeypatch.setattr(
        remote_server, "_workbench_admin_token", lambda: "synthetic-admin"
    )
    monkeypatch.setattr(remote_server.urllib_request, "urlopen", forward)
    path = "/workbench/v1/proxy/api/v1/file-content"

    def headers():
        return {
            "Content-Type": "application/json",
            "X-Allow-Configured-Compute": "1",
            **_signed_headers(token, method="POST", path=path, body=body),
        }

    response = client.post(path, content=body, headers=headers())
    assert response.status_code == 200
    assert captured[0]["body"] == body
    assert captured[0]["headers"]["content-type"] == "application/json"
    assert "x-allow-configured-compute" not in captured[0]["headers"]
    tampered = body.replace(b'"allow_compute":false', b'"allow_compute":true')
    assert tampered != body
    response = client.post(path, content=tampered, headers=headers())
    assert response.status_code == 401
    assert len(captured) == 1


def test_file_content_gateway_bounds_body_before_authentication(tmp_path, monkeypatch):
    from orchestrator import file_content_service
    monkeypatch.setenv("HASHI_FILE_CONTENT_REQUEST_TIMEOUT_SECONDS", "350")
    client, token = _client(tmp_path)
    captured = []
    monkeypatch.setattr(file_content_service, "CONTENT_WIRE_MAX_BYTES", 16)
    monkeypatch.setattr(remote_server, "_forward_workbench_gateway_request",
                        lambda **kwargs: (captured.append(kwargs) or 200, b"{}", {}))
    path = "/workbench/v1/proxy/api/v1/file-content"
    oversized = client.post(path, content=b"x" * 17)
    assert oversized.status_code == 413
    assert oversized.json()["error_code"] == "fileshare_content_limit"
    assert captured == []
    body = b"small"
    response = client.post(path, content=body,
                           headers=_signed_headers(token, method="POST", path=path, body=body))
    assert response.status_code == 200
    assert captured[0]["body_bytes"] == body
    assert captured[0]["timeout"] == 350


def test_forwarder_keeps_gateway_auth_out_of_loopback_and_injects_local_admin(
    monkeypatch,
):
    captured: dict = {}

    class _Response:
        status = 200
        headers = {
            "Content-Type": "application/json",
            "Server": "hidden",
            "X-Hashi-Correlation-Id": "call-request-2",
            "X-Hashi-Gateway-Request-Id": "gateway-request-2",
        }

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
        captured["headers"] = {
            key.lower(): value for key, value in request.header_items()
        }
        captured["timeout"] = timeout
        if request.full_url.endswith("/admin/start-agent") and timeout < 35:
            raise TimeoutError("worker still starting after 30 seconds")
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
            "x-hashi-correlation-id": "call-request-2",
            "x-hashi-trace-id": "call-trace-2",
            "x-hashi-span-id": "remote-span-2",
            "x-hashi-digest": "must-not-leak",
            "authorization": "Bearer fixture-user-session",
            "x-workbench-session": "fixture-user-session",
        },
    )

    assert status == 200
    assert body == b'{"ok":true}'
    assert headers == {
        "content-type": "application/json",
        "x-hashi-correlation-id": "call-request-2",
        "x-hashi-gateway-request-id": "gateway-request-2",
    }
    assert captured["url"] == "http://127.0.0.1:18800/api/chat?source=workbench"
    assert captured["body"] == b'{"text":"hello"}'
    assert captured["headers"]["x-workbench-token"] == "local-admin"
    assert captured["headers"]["x-hashi-correlation-id"] == "call-request-2"
    assert captured["headers"]["x-hashi-trace-id"] == "call-trace-2"
    assert captured["headers"]["x-hashi-span-id"] == "remote-span-2"
    assert captured["headers"]["authorization"] == "Bearer fixture-user-session"
    assert captured["headers"]["x-workbench-session"] == "fixture-user-session"
    assert "x-hashi-digest" not in captured["headers"]
    started = remote_server._request_workbench_agent_lifecycle(
        "windows-fixture", action="start"
    )
    assert started["ok"] is True
    assert captured["timeout"] >= 35


def test_forwarder_records_safe_socket_cause_without_endpoint_or_message(
    monkeypatch, caplog
):
    caplog.set_level(logging.INFO, logger=remote_server.logger.name)
    cause = OSError(104, "PRIVATE loopback endpoint")
    cause.code = "ECONNRESET"
    cause.syscall = "read"

    def fail(_request, timeout):
        raise URLError(cause)

    monkeypatch.setattr(remote_server, "local_http_hosts", lambda: ("127.0.0.1",))
    monkeypatch.setattr(remote_server, "_workbench_admin_token", lambda: "local-admin")
    monkeypatch.setattr(remote_server.urllib_request, "urlopen", fail)

    with pytest.raises(ConnectionError):
        remote_server._forward_workbench_gateway_request(
            method="POST",
            api_path="call/operation",
            query="private=SECRET",
            body_bytes=b'{"private":"BODY"}',
            request_headers={
                "content-type": "application/json",
                "x-hashi-correlation-id": "call-request-3",
            },
        )

    failed = _gateway_events(caplog, "local_request_failed")[-1]
    assert failed["correlation_id"] == "call-request-3"
    assert failed["exception_type"] == "URLError"
    # Python specializes OSError(errno) on platforms that recognize that errno.
    assert failed["cause_type"] == type(cause).__name__
    assert failed["cause_errno"] == 104 and failed["cause_code"] == "ECONNRESET"
    assert failed["cause_syscall"] == "read"
    assert "PRIVATE" not in caplog.text and "SECRET" not in caplog.text


def test_desktop_gateway_selects_live_local_api_before_post(monkeypatch):
    class Response:
        status = 200
        headers = {"Content-Type": "application/json"}

        def __init__(self, body):
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, *_args):
            return self.body

    calls = []

    def urlopen(request, timeout):
        calls.append((request.get_method(), request.full_url))
        if request.full_url.startswith("http://10.255.255.254:"):
            raise URLError("connection refused")
        if request.get_method() == "GET":
            return Response(
                b'{"ok":true,"instance_id":"HASHI1","workbench_port":18800,"protocol":"hashi-instance-identity-v1"}'
            )
        return Response(b'{"ok":true,"targets":[{"instance_id":"HASHI1"}]}')

    monkeypatch.setattr(
        remote_server, "local_http_hosts", lambda: ("10.255.255.254", "127.0.0.1")
    )
    monkeypatch.setattr(remote_server, "_instance_info", {"instance_id": "HASHI1"})
    monkeypatch.setattr(remote_server, "_workbench_port", 18800)
    monkeypatch.setattr(
        remote_server, "_workbench_admin_token", lambda: "existing-token"
    )
    monkeypatch.setattr(remote_server.urllib_request, "urlopen", urlopen)

    status, body, _headers = remote_server._forward_workbench_gateway_request(
        method="POST",
        api_path="v1/desktop/operation",
        query="",
        body_bytes=b'{"operation":"targets","client_id":"client-a"}',
        request_headers={"content-type": "application/json"},
    )

    assert status == 200
    assert json.loads(body)["targets"] == [{"instance_id": "HASHI1"}]
    assert [url for method, url in calls if method == "POST"] == [
        "http://127.0.0.1:18800/api/v1/desktop/operation"
    ]


def test_desktop_input_unknown_outcome_is_never_retried(monkeypatch):
    class Response:
        status = 200
        headers = {"Content-Type": "application/json"}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, *_args):
            return b'{"ok":true,"instance_id":"HASHI1","workbench_port":18800,"protocol":"hashi-instance-identity-v1"}'

    posted = []

    def urlopen(request, timeout):
        if request.get_method() == "GET":
            if request.full_url.startswith("http://10.255.255.254:"):
                raise URLError("connection refused")
            return Response()
        posted.append(request.full_url)
        raise TimeoutError("response lost after input")

    monkeypatch.setattr(
        remote_server, "local_http_hosts", lambda: ("10.255.255.254", "127.0.0.1")
    )
    monkeypatch.setattr(remote_server, "_instance_info", {"instance_id": "HASHI1"})
    monkeypatch.setattr(remote_server, "_workbench_port", 18800)
    monkeypatch.setattr(
        remote_server, "_workbench_admin_token", lambda: "existing-token"
    )
    monkeypatch.setattr(remote_server.urllib_request, "urlopen", urlopen)

    with pytest.raises(
        remote_server.WorkbenchGatewayError, match="request_outcome_unknown"
    ) as outcome:
        remote_server._forward_workbench_gateway_request(
            method="POST",
            api_path="v1/desktop/operation",
            query="",
            body_bytes=b'{"operation":"input","client_id":"client-a"}',
            request_headers={"content-type": "application/json"},
        )

    assert outcome.value.accepted is None
    assert outcome.value.retryable is False
    assert posted == ["http://127.0.0.1:18800/api/v1/desktop/operation"]


def test_desktop_identity_preflight_is_small_authenticated_and_exact(monkeypatch):
    class Response:
        status = 200
        headers = {"Content-Type": "application/json"}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, *_args):
            return b'{"ok":true,"instance_id":"HASHI1","workbench_port":18800,"protocol":"hashi-instance-identity-v1"}'

    calls = []

    def urlopen(request, timeout):
        calls.append(request)
        if request.get_method() == "GET":
            assert request.full_url.endswith("/api/v1/instance/identity")
            assert request.get_header("X-workbench-token") == "local-admin"
        return Response()

    monkeypatch.setattr(remote_server, "local_http_hosts", lambda: ("127.0.0.1",))
    monkeypatch.setattr(remote_server, "_workbench_admin_token", lambda: "local-admin")
    monkeypatch.setattr(remote_server.urllib_request, "urlopen", urlopen)
    assert remote_server._desktop_workbench_host("HASHI1", 18800) == "127.0.0.1"
    assert len(calls) == 1


@pytest.mark.parametrize(
    "identity",
    [
        {
            "ok": True,
            "instance_id": "HASHI2",
            "workbench_port": 18800,
            "protocol": "hashi-instance-identity-v1",
        },
        {
            "ok": True,
            "instance_id": "HASHI1",
            "workbench_port": 18801,
            "protocol": "hashi-instance-identity-v1",
        },
        {"ok": True, "instance_id": "HASHI1", "workbench_port": 18800},
    ],
)
def test_desktop_identity_unverified_is_rejected_before_input(monkeypatch, identity):
    class Response:
        status = 200
        headers = {}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, *_args):
            return json.dumps(identity).encode()

    posts = []

    def urlopen(request, timeout):
        if request.get_method() != "GET":
            posts.append(request)
        return Response()

    monkeypatch.setattr(remote_server, "local_http_hosts", lambda: ("127.0.0.1",))
    monkeypatch.setattr(remote_server, "_instance_info", {"instance_id": "HASHI1"})
    monkeypatch.setattr(remote_server, "_workbench_port", 18800)
    monkeypatch.setattr(remote_server, "_workbench_admin_token", lambda: "local-admin")
    monkeypatch.setattr(remote_server.urllib_request, "urlopen", urlopen)
    with pytest.raises(ConnectionError):
        remote_server._forward_workbench_gateway_request(
            method="POST",
            api_path="v1/desktop/operation",
            query="",
            body_bytes=b'{"operation":"input"}',
            request_headers={},
        )
    assert posts == []


def test_desktop_error_envelope_distinguishes_preflight_from_unknown_outcome(
    tmp_path, monkeypatch
):
    client, token = _client(tmp_path)
    path = "/workbench/v1/proxy/api/v1/desktop/operation"
    body = b'{"operation":"input"}'
    for code, accepted, retryable, status in [
        ("local_workbench_identity_unverified", False, True, 503),
        ("request_outcome_unknown", None, False, 502),
    ]:

        def fail(**_kwargs):
            raise remote_server.WorkbenchGatewayError(
                code, accepted=accepted, retryable=retryable, status=status
            )

        monkeypatch.setattr(remote_server, "_forward_workbench_gateway_request", fail)
        response = client.post(
            path,
            content=body,
            headers=_signed_headers(token, method="POST", path=path, body=body),
        )
        assert response.status_code == status
        assert response.json()["code"] == code
        assert response.json()["accepted"] is accepted
        assert response.json()["retryable"] is retryable


@pytest.mark.asyncio
async def test_attachment_relay_streams_before_upstream_completes(tmp_path, monkeypatch):
    import asyncio
    from aiohttp import web
    from aiohttp.test_utils import TestServer

    release = asyncio.Event()
    observed = {}

    async def source(request):
        observed["path"] = request.path_qs
        observed["token"] = request.headers.get("X-Workbench-Token")
        observed["encoding"] = request.headers.get("Accept-Encoding")
        response = web.StreamResponse(headers={
            "Content-Type": "application/octet-stream", "Content-Length": "9",
            "X-Hashi-Attachment-SHA256": "a" * 64, "ETag": '"' + "a" * 64 + '"',
        })
        await response.prepare(request)
        await response.write(b"first")
        await release.wait()
        await response.write(b"tail")
        await response.write_eof()
        return response

    app = web.Application()
    app.router.add_get("/api/transcript/a/attachments/m/f", source)
    async with TestServer(app) as server:
        monkeypatch.setattr(remote_server, "_workbench_port", server.port)
        monkeypatch.setattr(remote_server, "local_http_hosts", lambda: ("127.0.0.1",))
        monkeypatch.setattr(remote_server, "_workbench_admin_token", lambda: "fixture-admin")
        result = await remote_server._forward_workbench_attachment_stream(
            api_path="transcript/a/attachments/m/f", query="download=1", request_headers={},
        )
        try:
            assert result.status_code == 200
            assert result.headers["content-length"] == "9"
            assert result.headers["x-hashi-attachment-sha256"] == "a" * 64
            first = await asyncio.wait_for(anext(result.body_iterator), 3)
            assert first == b"first"
            assert not release.is_set()
            release.set()
            rest = b"".join([part async for part in result.body_iterator])
            assert first + rest == b"firsttail"
            assert observed == {
                "path": "/api/transcript/a/attachments/m/f?download=1",
                "token": "fixture-admin", "encoding": "identity",
            }
        finally:
            release.set()
            await result.body_iterator.aclose()


def test_attachment_relay_still_requires_authenticated_exact_request(tmp_path, monkeypatch):
    from fastapi.responses import StreamingResponse

    client, token = _client(tmp_path)
    calls = []

    async def stream(**kwargs):
        calls.append(kwargs)
        async def chunks():
            yield b"fixture"
        return StreamingResponse(chunks(), headers={"X-Hashi-Attachment-SHA256": "f" * 64})

    monkeypatch.setattr(remote_server, "_forward_workbench_attachment_stream", stream)
    path = "/workbench/v1/proxy/api/transcript/a/attachments/m/f"
    query = "download=1"
    assert client.get(f"{path}?{query}").status_code == 401
    assert not calls
    response = client.get(
        f"{path}?{query}", headers=_signed_headers(token, method="GET", path=path, query=query))
    assert response.status_code == 200
    assert response.content == b"fixture"
    assert response.headers["x-hashi-attachment-sha256"] == "f" * 64
    assert calls[0]["api_path"] == "transcript/a/attachments/m/f"
    assert calls[0]["query"] == query


def test_gateway_session_proof_authenticates_exact_session_and_run_without_private_content(tmp_path, monkeypatch):
    client, token = _client(tmp_path)
    calls = []
    session = {"session_id": "ses-old", "agent_id": "lily", "owner_id": "user:7",
               "context_generation": 3, "status": "active", "private": "NEVER_SEND"}
    run = {"session_id": "ses-old", "run_id": "run-old", "request_id": "req-old",
           "agent_id": "lily", "context_generation": 3, "state": "running",
           "private": "NEVER_SEND"}

    def fake_forward(**kwargs):
        calls.append(kwargs)
        api_path = kwargs["api_path"]
        if api_path == "v1/sessions/ses-old":
            payload = {"ok": True, "session": session}
        elif api_path == "v1/sessions/ses-old/runs/run-old":
            payload = {"ok": True, "run": run}
        else:
            return 404, b'{"ok":false}', {"content-type": "application/json"}
        return 200, json.dumps(payload).encode(), {"content-type": "application/json"}

    monkeypatch.setattr(remote_server, "_forward_workbench_gateway_request", fake_forward)
    path = "/workbench/v1/session-proof"
    body = b'{"session_id":"ses-old","run_id":"run-old"}'
    assert client.post(path, content=body).status_code == 401
    assert not calls
    headers = _signed_headers(token, method="POST", path=path, body=body)
    response = client.post(path, content=body, headers=headers)
    assert response.status_code == 200
    payload = response.json()
    proof = payload.pop("response_auth")
    assert verify_response_auth(shared_token=token, request_nonce=headers[HEADER_NONCE],
                                payload=payload, response_auth=proof)
    assert payload["instance_id"] == "HASHI1"
    assert payload["session"]["session_id"] == "ses-old"
    assert payload["session"]["owner_id"] == "user:7"
    assert payload["run"]["run_id"] == "run-old"
    assert "NEVER_SEND" not in response.text
    assert all(call["method"] == "GET" for call in calls)

    run["session_id"] = "different"
    headers = _signed_headers(token, method="POST", path=path, body=body)
    mismatch = client.post(path, content=body, headers=headers)
    assert mismatch.status_code == 409


def test_gateway_agent_proof_signs_only_exact_read_only_lifecycle_status(tmp_path, monkeypatch):
    client, token = _client(tmp_path)
    agents = [{"id": "lily", "is_active": True, "online": True,
               "status": "online", "private": "NEVER_SEND"}]
    calls = []

    def fake_forward(**kwargs):
        calls.append(kwargs)
        assert kwargs["method"] == "GET"
        assert kwargs["api_path"] == "agents"
        assert kwargs["query"] == "include_inactive=1"
        return 200, json.dumps({"ok": True, "agents": agents}).encode(), {}

    monkeypatch.setattr(remote_server, "_forward_workbench_gateway_request", fake_forward)
    path = "/workbench/v1/agent-proof"
    body = b'{"agent_id":"lily"}'
    assert client.post(path, content=body).status_code == 401
    assert not calls
    headers = _signed_headers(token, method="POST", path=path, body=body)
    response = client.post(path, content=body, headers=headers)
    assert response.status_code == 200
    payload = response.json()
    proof = payload.pop("response_auth")
    assert verify_response_auth(shared_token=token, request_nonce=headers[HEADER_NONCE],
                                payload=payload, response_auth=proof)
    assert payload["instance_id"] == "HASHI1"
    assert payload["agent"]["id"] == "lily"
    assert payload["agent"]["is_active"] is True
    assert "NEVER_SEND" not in response.text

    agents.append(dict(agents[0]))
    headers = _signed_headers(token, method="POST", path=path, body=body)
    assert client.post(path, content=body, headers=headers).status_code == 409


def test_gateway_agent_proof_signs_exact_operation_result_and_preserves_not_found(tmp_path, monkeypatch):
    client, token = _client(tmp_path)
    calls = []
    status = {"ok": True, "operation_id": "fixture-op-123", "agent_id": "lily",
              "desired_active": True, "state": "failed", "error_code": "agent_start_failed",
              "private": "NEVER_SEND"}
    def fake_forward(**kwargs):
        calls.append(kwargs)
        assert kwargs["method"] == "GET"
        if kwargs["api_path"] == "agents":
            return 200, json.dumps({"ok": True, "agents": [
                {"id": "lily", "is_active": False, "online": False, "status": "inactive"}
            ]}).encode(), {}
        assert kwargs["api_path"] == "agents/lily/active/operations/fixture-op-123"
        if status.get("not_found"):
            return 404, b'{"ok":false}', {}
        return 200, json.dumps(status).encode(), {}

    monkeypatch.setattr(remote_server, "_forward_workbench_gateway_request", fake_forward)
    path = "/workbench/v1/agent-proof"
    body = b'{"agent_id":"lily","operation_id":"fixture-op-123"}'
    headers = _signed_headers(token, method="POST", path=path, body=body)
    response = client.post(path, content=body, headers=headers)
    assert response.status_code == 200
    payload = response.json()
    proof = payload.pop("response_auth")
    assert verify_response_auth(shared_token=token, request_nonce=headers[HEADER_NONCE],
                                payload=payload, response_auth=proof)
    assert payload["operation"] == {"operation_id": "fixture-op-123", "agent_id": "lily",
                                    "desired_active": True, "state": "failed",
                                    "error_code": "agent_start_failed"}
    assert "NEVER_SEND" not in response.text
    assert len(calls) == 2

    status["operation_id"] = "different"
    headers = _signed_headers(token, method="POST", path=path, body=body)
    assert client.post(path, content=body, headers=headers).status_code == 409

    status["not_found"] = True
    headers = _signed_headers(token, method="POST", path=path, body=body)
    missing = client.post(path, content=body, headers=headers)
    assert missing.status_code == 200
    missing_payload = missing.json()
    missing_proof = missing_payload.pop("response_auth")
    assert verify_response_auth(shared_token=token, request_nonce=headers[HEADER_NONCE],
                                payload=missing_payload, response_auth=missing_proof)
    assert missing_payload["operation"] is None
