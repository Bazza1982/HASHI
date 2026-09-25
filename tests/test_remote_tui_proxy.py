from __future__ import annotations

import base64
import hashlib
import json
from types import SimpleNamespace

from fastapi.testclient import TestClient

import remote.api.server as remote_server
from orchestrator.frontend_delivery import (
    TUI_MUTATING_PROXY_OPERATIONS,
    tui_run_delivery_policy,
)
from orchestrator.message_context import verify_connector_evidence
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


class _HealthResponse:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _limit=-1):
        return json.dumps({"ok": True, "instance_id": "HASHI1"}).encode()


def _peer(
    *,
    handshake: str = "handshake_accepted",
    live: str = "online",
    capabilities=None,
):
    return SimpleNamespace(
        instance_id="HASHI2",
        capabilities=(
            ["handshake_v2", "tui_proxy_v1"]
            if capabilities is None
            else capabilities
        ),
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


def test_protocol_tui_uses_valid_hmac_even_when_peer_registry_is_stale(tmp_path, monkeypatch):
    client, token = _client(
        tmp_path,
        _peer(handshake="handshake_pending", live="offline", capabilities=[]),
    )
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


def test_loopback_tui_proxy_requires_completed_target_handshake(tmp_path):
    client, _token = _client(tmp_path, _peer(handshake="handshake_pending"))

    response = client.post(
        "/tui/proxy",
        json={"target_instance": "HASHI2", "operation": "agents"},
    )

    assert response.status_code == 409
    assert response.json()["error"] == "handshake_required"


def test_loopback_tui_proxy_ignores_stale_liveness_and_capability_metadata(
    tmp_path, monkeypatch
):
    client, _token = _client(
        tmp_path,
        _peer(live="offline", capabilities=[]),
    )
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


def test_tui_proxy_does_not_replay_mutating_request_after_unknown_peer_outcome(
    tmp_path, monkeypatch
):
    client, _token = _client(tmp_path)
    attempts = []
    monkeypatch.setattr(
        _Protocol,
        "resolve_forward_urls",
        lambda self, instance_id, path: [
            f"http://first.invalid:8767{path}",
            f"http://second.invalid:8767{path}",
        ],
    )

    def lost_response(url, payload, *, timeout=15):
        attempts.append(url)
        raise TimeoutError("peer may have accepted the Run")

    monkeypatch.setattr(remote_server, "_post_json_with_optional_hmac", lost_response)
    response = client.post(
        "/tui/proxy",
        json={"target_instance": "HASHI2", "operation": "chat", "agent": "akane", "text": "hello"},
    )

    assert response.status_code == 502
    assert response.json()["code"] == "request_outcome_unknown"
    assert attempts == ["http://first.invalid:8767/protocol/tui"]


def test_tui_proxy_allowlist_rejects_arbitrary_workbench_operation(tmp_path):
    client, _token = _client(tmp_path)

    response = client.post(
        "/tui/proxy",
        json={"target_instance": "HASHI2", "operation": "admin_delete"},
    )

    assert response.status_code == 400
    assert response.json()["error"] == "operation_not_allowed"


def test_tui_proxy_mutation_classification_is_within_operation_allowlist():
    assert TUI_MUTATING_PROXY_OPERATIONS <= remote_server.TUI_PROXY_OPERATIONS


def test_tui_log_tail_reads_only_bounded_instance_owned_log(tmp_path, monkeypatch):
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "bridge.log").write_text(
        "old\n" + "\n".join(f"line-{index}" for index in range(250)) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(remote_server, "_hashi_root", str(tmp_path))
    monkeypatch.setattr(remote_server, "_instance_info", {"instance_id": "HASHI3"})

    status, payload = remote_server._local_workbench_tui_request(
        ProtocolTuiRequest(
            from_instance="HASHI1",
            operation="log_tail",
            limit=12,
        )
    )

    assert status == 200
    assert payload["ok"] is True
    assert payload["instance_id"] == "HASHI3"
    assert len(payload["lines"]) == 12
    assert payload["lines"][-1] == "line-249"
    assert "path" not in payload
    assert isinstance(payload["offset"], int)


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


def test_tui_session_run_proxy_rejects_invalid_identity_before_forwarding():
    policy = tui_run_delivery_policy(
        telegram_mirror=False, client_id="tui-window-1"
    )
    invalid = ProtocolTuiRequest(
        from_instance="HASHI1",
        operation="session_run",
        agent="akane",
        session_id="../other-session",
        text="hello",
        client_id="tui-window-1",
        delivery_policy=policy,
        idempotency_key="tui-1",
    )
    assert remote_server._validate_tui_proxy_payload(invalid) == (
        False, "invalid_run_identity"
    )


def test_tui_session_command_proxy_requires_typed_generation_bound_identity():
    valid = ProtocolTuiRequest(
        from_instance="HASHI1",
        operation="session_command_invocation",
        agent="akane",
        session_id="session_123",
        client_id="tui-window-command",
        request_id="command-request-123456",
        command="model",
        arguments=["balanced"],
        context_generation=3,
        ui_locale="en",
    )
    assert remote_server._validate_tui_proxy_payload(valid) == (True, "ok")
    assert remote_server._validate_tui_proxy_payload(
        valid.model_copy(update={"context_generation": 0})
    ) == (False, "invalid_command_identity")
    assert remote_server._validate_tui_proxy_payload(
        valid.model_copy(update={"arguments": ["x" * 4097]})
    ) == (False, "invalid_command_arguments")


def test_tui_proxy_rejects_malformed_attachment_size_without_server_error():
    content = b"data"
    payload = ProtocolTuiRequest(
        from_instance="HASHI1",
        operation="chat_attachment",
        agent="akane",
        text="read",
        attachment={
            "filename": "note.txt",
            "size_bytes": "not-a-number",
            "content_b64": base64.b64encode(content).decode("ascii"),
        },
    )
    assert remote_server._validate_tui_proxy_payload(payload) == (
        False, "invalid_attachment"
    )


def test_tui_proxy_rejects_cross_platform_unsafe_attachment_names():
    encoded = base64.b64encode(b"data").decode("ascii")
    for filename in ("..\\private.txt", "..", "bad\x00name.txt"):
        payload = ProtocolTuiRequest(
            from_instance="HASHI1", operation="chat_attachment",
            agent="akane", text="read",
            attachment={"filename": filename, "content_b64": encoded},
        )
        assert remote_server._validate_tui_proxy_payload(payload) == (
            False, "invalid_attachment"
        )


def test_authenticated_tui_session_run_forwards_only_typed_payload_and_origin(
    tmp_path, monkeypatch
):
    _client(tmp_path)
    captured = {}

    class _Response:
        status = 202

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit=-1):
            return json.dumps(
                {"ok": True, "session_id": "ses-1", "run_id": "run-1"}
            ).encode()

    def _urlopen(request, timeout=15):
        if request.get_method() == "GET":
            return _HealthResponse()
        captured["path"] = request.full_url
        captured["method"] = request.get_method()
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return _Response()

    monkeypatch.setattr(remote_server.urllib_request, "urlopen", _urlopen)
    monkeypatch.setattr(remote_server, "local_http_hosts", lambda: ("127.0.0.1",))
    policy = tui_run_delivery_policy(
        telegram_mirror=False, client_id="tui-origin-test"
    )
    status, _result = remote_server._local_workbench_tui_request(
        ProtocolTuiRequest(
            from_instance="HASHI1",
            operation="session_run",
            agent="akane",
            session_id="ses-1",
            text="origin evidence",
            client_id="tui-origin-test",
            delivery_policy=policy,
            idempotency_key="tui-message-1",
        )
    )

    assert status == 202
    assert captured["method"] == "POST"
    assert captured["path"].endswith("/api/v1/sessions/ses-1/runs")
    body = captured["body"]
    assert body["idempotency_key"] == "tui-message-1"
    assert body["surface"] == "tui"
    assert body["delivery_policy"] == policy
    assert body["message"]["content"] == [
        {"type": "text", "text": "origin evidence"}
    ]
    evidence = body["request_metadata"]["_connector_evidence"]
    claims = verify_connector_evidence(
        tmp_path, evidence=evidence, prompt="origin evidence"
    )
    assert claims["_origin_instance_evidence"]["id"] == "HASHI1"


def test_remote_tui_session_command_posts_typed_body_with_sealed_origin(
    tmp_path, monkeypatch
):
    _client(tmp_path)
    captured = {}

    class _Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit=-1):
            return json.dumps({"ok": True, "slash_command": True}).encode()

    def _urlopen(request, timeout=15):
        if request.get_method() == "GET":
            return _HealthResponse()
        captured["path"] = request.full_url
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return _Response()

    monkeypatch.setattr(remote_server.urllib_request, "urlopen", _urlopen)
    monkeypatch.setattr(remote_server, "local_http_hosts", lambda: ("127.0.0.1",))
    status, result = remote_server._local_workbench_tui_request(
        ProtocolTuiRequest(
            from_instance="HASHI1",
            operation="session_command_invocation",
            agent="akane",
            session_id="session_123",
            client_id="tui-window-command",
            request_id="command-request-123456",
            command="model",
            arguments=["balanced"],
            context_generation=3,
            ui_locale="zh-CN",
        )
    )

    assert status == 200
    assert result["slash_command"] is True
    assert captured["path"].endswith(
        "/api/v1/sessions/session_123/commands"
    )
    body = captured["body"]
    assert body["command"] == "model"
    assert body["arguments"] == ["balanced"]
    assert body["context_generation"] == 3
    assert body["client_id"] == "tui-window-command"
    evidence = body["request_metadata"]["_connector_evidence"]
    prompt_binding = json.dumps(
        {"command": "model", "arguments": ["balanced"]},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    claims = verify_connector_evidence(
        tmp_path, evidence=evidence, prompt=prompt_binding
    )
    assert claims["_message_source_reserved"] == "tui"
    assert claims["_origin_instance_evidence"]["id"] == "HASHI1"


def test_authenticated_tui_proxy_forwards_command_fields_and_marks_attempt(
    tmp_path, monkeypatch
):
    client, _token = _client(tmp_path)
    captured = {}

    def _forward(_url, payload, *, timeout):
        captured["payload"] = payload
        captured["timeout"] = timeout
        return {
            "__http_status": 200,
            "target_instance": "HASHI2",
            "result": {"ok": True, "slash_command": True},
        }

    monkeypatch.setattr(
        remote_server, "_post_json_with_optional_hmac", _forward
    )
    response = client.post(
        "/tui/proxy",
        json={
            "target_instance": "HASHI2",
            "operation": "session_command_invocation",
            "agent": "akane",
            "session_id": "session_123",
            "client_id": "tui-window-command",
            "request_id": "command-request-123456",
            "command": "model",
            "arguments": ["balanced"],
            "context_generation": 3,
            "ui_locale": "en",
        },
    )

    assert response.status_code == 200
    assert captured["payload"]["from_instance"] == "HASHI1"
    assert captured["payload"]["operation"] == "session_command_invocation"
    assert captured["payload"]["command"] == "model"
    assert captured["payload"]["arguments"] == ["balanced"]
    assert captured["payload"]["context_generation"] == 3
    assert captured["timeout"] == 45
    assert response.json()["result"]["canonical_operation_attempted"] is True


def test_remote_tui_local_write_does_not_replay_after_lost_response(tmp_path, monkeypatch):
    _client(tmp_path)
    monkeypatch.setattr(
        remote_server, "local_http_hosts", lambda: ("127.0.0.2", "127.0.0.1")
    )
    attempts = []

    class _Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit=-1):
            return json.dumps({"ok": True, "instance_id": "HASHI1"}).encode()

    def _urlopen(request, timeout=15):
        attempts.append((request.get_method(), request.full_url))
        if request.get_method() == "GET":
            return _Response()
        if "127.0.0.2" in request.full_url:
            raise TimeoutError("Run accepted, response lost")
        return _Response()

    monkeypatch.setattr(remote_server.urllib_request, "urlopen", _urlopen)
    status, result = remote_server._local_workbench_tui_request(
        ProtocolTuiRequest(
            from_instance="HASHI2", operation="session_run", agent="akane",
            session_id="ses-1", text="hello", client_id="tui-1",
            delivery_policy=tui_run_delivery_policy(
                telegram_mirror=False, client_id="tui-1"
            ),
            idempotency_key="tui-1",
        )
    )

    assert status == 502
    assert result["code"] == "request_outcome_unknown"
    assert [url for method, url in attempts if method == "POST"] == [
        "http://127.0.0.2:18800/api/v1/sessions/ses-1/runs"
    ]


def test_remote_tui_local_write_skips_wrong_instance_host(tmp_path, monkeypatch):
    _client(tmp_path)
    monkeypatch.setattr(
        remote_server, "local_http_hosts", lambda: ("127.0.0.2", "127.0.0.1")
    )
    attempts = []

    class _Response:
        status = 200

        def __init__(self, instance_id):
            self.instance_id = instance_id

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit=-1):
            return json.dumps({"ok": True, "instance_id": self.instance_id}).encode()

    def _urlopen(request, timeout=15):
        attempts.append((request.get_method(), request.full_url))
        if request.get_method() == "GET":
            return _Response(
                "HASHI9" if "127.0.0.2" in request.full_url else "HASHI1"
            )
        return _Response("HASHI1")

    monkeypatch.setattr(remote_server.urllib_request, "urlopen", _urlopen)
    status, result = remote_server._local_workbench_tui_request(
        ProtocolTuiRequest(
            from_instance="HASHI2", operation="chat", agent="akane",
            text="hello",
        )
    )
    assert status == 200
    assert result["ok"] is True
    assert [url for method, url in attempts if method == "POST"] == [
        "http://127.0.0.1:18800/api/chat"
    ]


def test_authenticated_cross_instance_tui_seals_origin_evidence(
    tmp_path, monkeypatch
):
    _client(tmp_path)
    captured = {}

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit=-1):
            return json.dumps({"ok": True, "request_id": "req-tui"}).encode()

    def _urlopen(request, timeout=15):
        del timeout
        if request.get_method() == "GET":
            return _HealthResponse()
        captured.update(json.loads(request.data.decode("utf-8")))
        return _Response()

    monkeypatch.setattr(remote_server.urllib_request, "urlopen", _urlopen)
    monkeypatch.setattr(remote_server, "local_http_hosts", lambda: ("127.0.0.1",))
    policy = tui_run_delivery_policy(
        telegram_mirror=False, client_id="tui-origin-test"
    )
    status, _result = remote_server._local_workbench_tui_request(
        ProtocolTuiRequest(
            from_instance="HASHI1",
            operation="chat",
            agent="akane",
            text="origin evidence",
            client_id="tui-origin-test",
            delivery_policy=policy,
        )
    )

    assert status == 200
    evidence = captured["request_metadata"]["_connector_evidence"]
    claims = verify_connector_evidence(
        tmp_path,
        evidence=evidence,
        prompt="origin evidence",
    )
    assert claims["_message_source_reserved"] == "tui"
    assert claims["_origin_instance_evidence"] == {
        "id": "HASHI1",
        "assurance": "shared_network_hmac",
    }


def test_tui_attachment_proxy_forwards_frozen_bytes_with_integrity(
    tmp_path, monkeypatch
):
    _client(tmp_path)
    captured = {}
    content = b"attachment bytes"
    encoded = base64.b64encode(content).decode("ascii")

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit=-1):
            return json.dumps({"ok": True, "request_id": "req-attachment"}).encode()

    def _urlopen(request, timeout=15):
        del timeout
        if request.get_method() == "GET":
            return _HealthResponse()
        captured.update(json.loads(request.data.decode("utf-8")))
        return _Response()

    monkeypatch.setattr(remote_server.urllib_request, "urlopen", _urlopen)
    monkeypatch.setattr(remote_server, "local_http_hosts", lambda: ("127.0.0.1",))
    status, _result = remote_server._local_workbench_tui_request(
        ProtocolTuiRequest(
            from_instance="HASHI1",
            operation="chat_attachment",
            agent="akane",
            text="inspect this",
            attachment={
                "filename": "report.txt",
                "media_type": "text/plain",
                "content_b64": encoded,
                "size_bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            },
        )
    )

    assert status == 200
    assert captured["text"] == "inspect this"
    assert captured["attachment"]["content_b64"] == encoded
    assert captured["attachment"]["sha256"] == hashlib.sha256(content).hexdigest()


def test_tui_attachment_proxy_rejects_corrupt_or_ambiguous_sources():
    content = b"attachment bytes"
    attachment = {
        "filename": "report.txt",
        "content_b64": base64.b64encode(content).decode("ascii"),
        "size_bytes": len(content),
        "sha256": "0" * 64,
    }
    corrupt = ProtocolTuiRequest(
        from_instance="HASHI1",
        operation="chat_attachment",
        agent="akane",
        attachment=attachment,
    )
    ambiguous = ProtocolTuiRequest(
        from_instance="HASHI1",
        operation="chat_attachment",
        agent="akane",
        attachment={**attachment, "sha256": hashlib.sha256(content).hexdigest()},
        workzone_ref="report.txt",
    )

    assert remote_server._validate_tui_proxy_payload(corrupt) == (
        False,
        "attachment_digest_mismatch",
    )
    assert remote_server._validate_tui_proxy_payload(ambiguous) == (
        False,
        "invalid_attachment_source",
    )


def _canonical_attachment_request(*, session_id="ses-1"):
    content = b"remote attachment bytes"
    return ProtocolTuiRequest(
        from_instance="HASHI2", operation="session_attachment_run",
        agent="akane", session_id=session_id, text="read this",
        client_id="tui-window-1", idempotency_key="tui-attachment-1",
        delivery_policy=tui_run_delivery_policy(
            telegram_mirror=False, client_id="tui-window-1"
        ),
        attachment={
            "filename": "note.txt", "media_type": "text/plain",
            "size_bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
            "content_b64": base64.b64encode(content).decode("ascii"),
        },
    )


def test_remote_tui_attachment_requires_typed_session_and_integrity():
    valid = _canonical_attachment_request()
    assert remote_server._validate_tui_proxy_payload(valid) == (True, "ok")
    assert remote_server._validate_tui_proxy_payload(
        valid.model_copy(update={"session_id": "../other"})
    ) == (False, "invalid_run_identity")
    assert remote_server._validate_tui_proxy_payload(
        valid.model_copy(update={"workzone_ref": "note.txt"})
    ) == (False, "invalid_attachment_source")
    assert remote_server._validate_tui_proxy_payload(
        valid.model_copy(update={"attachment": {**valid.attachment, "sha256": "0" * 64}})
    ) == (False, "attachment_digest_mismatch")


def test_remote_tui_attachment_stages_uploads_commits_and_binds_one_run(
    tmp_path, monkeypatch
):
    _client(tmp_path)
    monkeypatch.setattr(remote_server, "local_http_hosts", lambda: ("127.0.0.1",))
    calls = []

    class Response:
        def __init__(self, result, status=200):
            self.result, self.status = result, status

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit=-1):
            return json.dumps(self.result).encode("utf-8")

    def urlopen(request, timeout=15):
        calls.append((request.get_method(), request.full_url, request.data))
        path = request.full_url.split(":18800", 1)[-1]
        if path == "/api/health":
            return Response({"ok": True, "instance_id": "HASHI1"})
        if path == "/api/v1/agents/akane/primary-session":
            return Response({"ok": True, "session": {
                "session_id": "ses-1", "instance_id": "HASHI1", "agent_id": "akane"
            }})
        if path == "/api/v1/sessions/ses-1/attachments":
            return Response({"ok": True, "attachment": {"attachment_id": "att-1"}}, 201)
        if path.endswith("/att-1/content"):
            return Response({"ok": True, "attachment": {"attachment_id": "att-1"}})
        if path.endswith("/att-1/commit"):
            return Response({"ok": True, "attachment": {"attachment_id": "att-1"}})
        if path == "/api/v1/sessions/ses-1/runs":
            return Response({"ok": True, "session_id": "ses-1", "run_id": "run-1"}, 202)
        raise AssertionError(f"unexpected request {path}")

    monkeypatch.setattr(remote_server.urllib_request, "urlopen", urlopen)
    request = _canonical_attachment_request()
    status, result = remote_server._local_workbench_tui_request(request)

    assert status == 202
    assert result["run_id"] == "run-1"
    assert result["canonical_operation_attempted"] is True
    assert [(method, url.split(":18800", 1)[-1]) for method, url, _ in calls] == [
        ("GET", "/api/health"),
        ("GET", "/api/v1/agents/akane/primary-session"),
        ("POST", "/api/v1/sessions/ses-1/attachments"),
        ("PUT", "/api/v1/sessions/ses-1/attachments/att-1/content"),
        ("POST", "/api/v1/sessions/ses-1/attachments/att-1/commit"),
        ("POST", "/api/v1/sessions/ses-1/runs"),
    ]
    stage = json.loads(calls[2][2])
    assert stage["sha256"] == request.attachment["sha256"]
    assert stage["size_bytes"] == request.attachment["size_bytes"]
    assert calls[3][2] == base64.b64decode(request.attachment["content_b64"])
    run = json.loads(calls[-1][2])
    assert run["message"]["content"] == [
        {"type": "text", "text": "read this"},
        {"type": "attachment", "attachment_id": "att-1"},
    ]
    assert run["idempotency_key"] == "tui-attachment-1"
    assert run["delivery_policy"] == request.delivery_policy
    evidence = run["request_metadata"]["_connector_evidence"]
    claims = verify_connector_evidence(
        tmp_path, evidence=evidence, prompt="read this"
    )
    assert claims["_origin_instance_evidence"]["id"] == "HASHI2"


def test_remote_tui_attachment_upload_uncertainty_never_replays_or_runs(
    tmp_path, monkeypatch
):
    _client(tmp_path)
    monkeypatch.setattr(
        remote_server, "local_http_hosts", lambda: ("127.0.0.1", "127.0.0.2")
    )
    calls = []

    class Response:
        status = 200

        def __init__(self, result):
            self.result = result

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit=-1):
            return json.dumps(self.result).encode("utf-8")

    def urlopen(request, timeout=15):
        calls.append((request.get_method(), request.full_url))
        path = request.full_url.split(":18800", 1)[-1]
        if path == "/api/health":
            return Response({"ok": True, "instance_id": "HASHI1"})
        if path.endswith("/primary-session"):
            return Response({"ok": True, "session": {
                "session_id": "ses-1", "instance_id": "HASHI1", "agent_id": "akane"
            }})
        if path.endswith("/attachments"):
            return Response({"ok": True, "attachment": {"attachment_id": "att-1"}})
        if path.endswith("/att-1/content"):
            raise TimeoutError("upload may have completed")
        raise AssertionError(f"operation continued after uncertain upload: {path}")

    monkeypatch.setattr(remote_server.urllib_request, "urlopen", urlopen)
    status, result = remote_server._local_workbench_tui_request(
        _canonical_attachment_request()
    )

    assert status == 502
    assert result["code"] == "request_outcome_unknown"
    assert all("127.0.0.2" not in url for _, url in calls)
    assert [method for method, _ in calls] == ["GET", "GET", "POST", "PUT"]


def test_remote_tui_attachment_rejects_wrong_primary_before_staging(
    tmp_path, monkeypatch
):
    _client(tmp_path)
    monkeypatch.setattr(remote_server, "local_http_hosts", lambda: ("127.0.0.1",))
    calls = []

    class Response:
        status = 200

        def __init__(self, result):
            self.result = result

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit=-1):
            return json.dumps(self.result).encode("utf-8")

    def urlopen(request, timeout=15):
        calls.append((request.get_method(), request.full_url))
        if request.full_url.endswith("/api/health"):
            return Response({"ok": True, "instance_id": "HASHI1"})
        if request.full_url.endswith("/primary-session"):
            return Response({"ok": True, "session": {
                "session_id": "ses-other", "instance_id": "HASHI1",
                "agent_id": "akane",
            }})
        raise AssertionError("attachment may not reach the wrong Session")

    monkeypatch.setattr(remote_server.urllib_request, "urlopen", urlopen)
    status, result = remote_server._local_workbench_tui_request(
        _canonical_attachment_request()
    )
    assert status == 409
    assert result["error"] == "primary_session_mismatch"
    assert result["canonical_operation_attempted"] is True
    assert [method for method, _ in calls] == ["GET", "GET"]


def test_remote_tui_workzone_reference_rejects_traversal():
    valid = _canonical_attachment_request().model_copy(update={
        "operation": "session_workzone_attachment_run",
        "attachment": None,
        "workzone_ref": "reports/weekly.pdf",
    })
    assert remote_server._validate_tui_proxy_payload(valid) == (True, "ok")
    invalid = valid.model_copy(update={"workzone_ref": "../private.txt"})
    assert remote_server._validate_tui_proxy_payload(invalid) == (
        False, "invalid_workzone_ref"
    )


def test_remote_tui_workzone_reference_uses_target_session_asset_and_run(
    tmp_path, monkeypatch
):
    _client(tmp_path)
    monkeypatch.setattr(remote_server, "local_http_hosts", lambda: ("127.0.0.1",))
    calls = []

    class Response:
        status = 200

        def __init__(self, result):
            self.result = result

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit=-1):
            return json.dumps(self.result).encode("utf-8")

    def urlopen(request, timeout=15):
        path = request.full_url.split(":18800", 1)[-1]
        calls.append((request.get_method(), path, request.data))
        if path == "/api/health":
            return Response({"ok": True, "instance_id": "HASHI1"})
        if path.endswith("/primary-session"):
            return Response({"ok": True, "session": {
                "session_id": "ses-1", "instance_id": "HASHI1",
                "agent_id": "akane",
            }})
        if path.endswith("/attachments/from-workzone"):
            return Response({"ok": True, "attachment": {
                "attachment_id": "att-1", "state": "committed"
            }})
        if path.endswith("/runs"):
            return Response({"ok": True, "run_id": "run-1"})
        raise AssertionError(f"unexpected request {path}")

    monkeypatch.setattr(remote_server.urllib_request, "urlopen", urlopen)
    request = _canonical_attachment_request().model_copy(update={
        "operation": "session_workzone_attachment_run",
        "attachment": None,
        "workzone_ref": "reports/weekly.pdf",
    })
    status, result = remote_server._local_workbench_tui_request(request)

    assert status == 200
    assert result["run_id"] == "run-1"
    assert result["canonical_operation_attempted"] is True
    assert [path for _, path, _ in calls] == [
        "/api/health",
        "/api/v1/agents/akane/primary-session",
        "/api/v1/sessions/ses-1/attachments/from-workzone",
        "/api/v1/sessions/ses-1/runs",
    ]
    assert json.loads(calls[2][2]) == {"reference": "reports/weekly.pdf"}
    assert json.loads(calls[3][2])["message"]["content"] == [
        {"type": "text", "text": "read this"},
        {"type": "attachment", "attachment_id": "att-1"},
    ]


def test_tui_speech_proxy_maps_only_to_local_presentation_endpoint(monkeypatch):
    captured = {}
    monkeypatch.setattr(remote_server, "_instance_info", {"instance_id": "HASHI1"})

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit):
            return b'{"ok":true}'

    def urlopen(request, timeout):
        if request.get_method() == "GET":
            return _HealthResponse()
        captured.update(
            method=request.get_method(),
            url=request.full_url,
            body=json.loads(request.data.decode("utf-8")),
            timeout=timeout,
        )
        return Response()

    monkeypatch.setattr(remote_server, "local_http_hosts", lambda: ("127.0.0.1",))
    monkeypatch.setattr(remote_server.urllib_request, "urlopen", urlopen)

    status, result = remote_server._local_workbench_tui_request(
        ProtocolTuiRequest(
            from_instance="HASHI1",
            operation="speech",
            agent="akane",
            text="last visible reply",
            request_id="tui-say-1",
        ),
        timeout=120,
    )

    assert status == 200
    assert result == {"ok": True}
    assert captured["method"] == "POST"
    assert captured["url"].endswith("/api/tui/speech")
    assert captured["body"] == {
        "agent": "akane",
        "text": "last visible reply",
        "request_id": "tui-say-1",
    }
    assert "delivery_policy" not in captured["body"]


def test_tui_speech_proxy_rejects_missing_identity_and_invalid_profile():
    missing_request = ProtocolTuiRequest(
        from_instance="HASHI1",
        operation="speech",
        agent="akane",
        text="hello",
    )
    invalid_profile = ProtocolTuiRequest(
        from_instance="HASHI1",
        operation="voice_profile",
        agent="akane",
        voice_profile="../../voice",
    )

    assert remote_server._validate_tui_proxy_payload(missing_request) == (
        False,
        "invalid_request_id",
    )
    assert remote_server._validate_tui_proxy_payload(invalid_profile) == (
        False,
        "invalid_voice_profile",
    )


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
