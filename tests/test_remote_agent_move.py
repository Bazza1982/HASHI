from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from orchestrator.agent_move.package import create_agent_move_package
from orchestrator.agent_move.remote_client import (
    AgentMoveRemoteClient,
    AgentMoveRemoteError,
    _candidate_base_urls,
    _request_json,
)
from orchestrator.agent_move.transport_crypto import (
    ENVELOPE_SCHEME,
    encrypt_package_transport,
)
from orchestrator.pcm import render_pcm_document
from remote.api import server as remote_server
from remote.api.server import create_app
from remote.security.pairing import PairingManager
from remote.security.shared_token import (
    HEADER_NONCE,
    build_auth_headers,
    verify_response_auth,
)
from remote.terminal.executor import TerminalExecutor

TOKEN = "shared-agent-move-secret"


def _write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def _root(tmp_path: Path, name: str, instance: str, *, with_agent: bool) -> Path:
    root = tmp_path / name
    root.mkdir()
    agents = []
    if with_agent:
        workspace = root / "workspaces" / "zelda"
        workspace.mkdir(parents=True)
        (workspace / "agent.md").write_text(
            render_pcm_document(
                persona="Zelda", system="Follow policy", memory="Memory"
            ),
            encoding="utf-8",
        )
        (workspace / "memory.md").write_text("remember", encoding="utf-8")
        agents.append(
            {
                "name": "zelda",
                "type": "flex",
                "workspace_dir": "workspaces/zelda",
                "active_backend": "codex-cli",
                "allowed_backends": [{"engine": "codex-cli", "model": "gpt-5.5"}],
                "is_active": True,
            }
        )
    _write_json(
        root / "agents.json", {"global": {"instance_id": instance}, "agents": agents}
    )
    _write_json(
        root / "tasks.json", {"version": 1, "heartbeats": [], "crons": [], "nudges": []}
    )
    secrets = {"hashi_remote_shared_token": TOKEN}
    if with_agent:
        secrets["zelda"] = "telegram-token"
    _write_json(root / "secrets.json", secrets)
    return root


def _client(target: Path) -> TestClient:
    app = create_app(
        {"instance_id": "HASHI2", "display_name": "Target", "remote_port": 8767},
        PairingManager(storage_dir=target / "pairing", lan_mode=True),
        TerminalExecutor(),
        hashi_root=str(target),
    )
    return TestClient(app)


def _headers(method: str, path: str, body: bytes = b"") -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    headers.update(
        build_auth_headers(
            shared_token=TOKEN,
            method=method,
            path=path,
            from_instance="HASHI1",
            body_bytes=body,
        )
    )
    return headers


def _post(client: TestClient, path: str, payload: dict):
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )
    return client.post(path, content=body, headers=_headers("POST", path, body))


def test_remote_agent_move_requires_hmac_even_in_legacy_lan_mode(tmp_path):
    target = _root(tmp_path, "target", "HASHI2", with_agent=False)
    client = _client(target)

    response = client.get("/agent-move/v1/capabilities")

    assert response.status_code == 401
    assert response.json()["ok"] is False


def test_remote_receiver_uses_live_identity_with_legacy_list_config(tmp_path):
    source = _root(tmp_path, "source", "HASHI1", with_agent=True)
    target = _root(tmp_path, "target", "HASHI2", with_agent=False)
    _write_json(target / "agents.json", [])
    package_path = tmp_path / "zelda.hashi-agent"
    create_agent_move_package(
        source,
        "zelda",
        package_path,
        source_instance="HASHI1",
    )
    client = _client(target)

    capabilities = client.get(
        "/agent-move/v1/capabilities",
        headers=_headers("GET", "/agent-move/v1/capabilities"),
    )
    assert capabilities.status_code == 200
    assert capabilities.json()["instance_id"] == "HASHI2"

    raw = package_path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    envelope = encrypt_package_transport(
        raw,
        shared_token=TOKEN,
        source_instance="HASHI1",
        target_instance="HASHI2",
        package_sha256=digest,
    )
    staged = _post(
        client,
        "/agent-move/v1/stage",
        {
            "from_instance": "HASHI1",
            "encryption": ENVELOPE_SCHEME,
            "package_b64": base64.b64encode(envelope).decode("ascii"),
            "sha256": digest,
        },
    )
    assert staged.status_code == 200
    assert staged.json()["target_instance"] == "HASHI2"


def test_remote_agent_move_stage_commit_activate_protocol(tmp_path):
    source = _root(tmp_path, "source", "HASHI1", with_agent=True)
    target = _root(tmp_path, "target", "HASHI2", with_agent=False)
    package_path = tmp_path / "zelda.hashi-agent"
    package = create_agent_move_package(
        source,
        "zelda",
        package_path,
        source_instance="HASHI1",
        include_agent_secrets=True,
        secret_passphrase=TOKEN,
    )
    client = _client(target)

    capability_headers = _headers("GET", "/agent-move/v1/capabilities")
    capabilities = client.get(
        "/agent-move/v1/capabilities",
        headers=capability_headers,
    )
    assert capabilities.status_code == 200
    capability_payload = capabilities.json()
    response_auth = capability_payload.pop("response_auth")
    assert capability_payload["capability"] == "agent_move_receive_v1"
    assert capability_payload["authenticated_response_proof"] is True
    assert ENVELOPE_SCHEME in capability_payload["package_encryption"]
    assert verify_response_auth(
        shared_token=TOKEN,
        request_nonce=capability_headers[HEADER_NONCE],
        payload=capability_payload,
        response_auth=response_auth,
    )

    raw = package_path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    envelope = encrypt_package_transport(
        raw,
        shared_token=TOKEN,
        source_instance="HASHI1",
        target_instance="HASHI2",
        package_sha256=digest,
    )
    staged = _post(
        client,
        "/agent-move/v1/stage",
        {
            "from_instance": "HASHI1",
            "encryption": ENVELOPE_SCHEME,
            "package_b64": base64.b64encode(envelope).decode("ascii"),
            "sha256": digest,
        },
    )
    assert staged.status_code == 200
    assert staged.json()["status"] == "staged"

    committed = _post(
        client,
        "/agent-move/v1/commit",
        {"from_instance": "HASHI1", "package_id": package.package_id},
    )
    assert committed.status_code == 200
    assert committed.json()["status"] == "committed_inactive"

    activated = _post(
        client,
        "/agent-move/v1/activate",
        {"from_instance": "HASHI1", "package_id": package.package_id},
    )
    assert activated.status_code == 200
    assert activated.json()["status"] == "activated_pending_reboot"
    assert (
        json.loads((target / "agents.json").read_text())["agents"][0]["is_active"]
        is True
    )


def test_remote_clone_resolve_lifecycle_and_finalize_without_telegram(
    tmp_path,
    monkeypatch,
):
    source = _root(tmp_path, "source", "HASHI1", with_agent=True)
    target = _root(tmp_path, "target", "HASHI2", with_agent=True)
    source_secrets = json.loads((source / "secrets.json").read_text())
    source_secrets["zelda_api_key"] = "safe-agent-key"
    _write_json(source / "secrets.json", source_secrets)
    package_path = tmp_path / "zelda-clone.hashi-agent"
    package = create_agent_move_package(
        source,
        "zelda",
        package_path,
        source_instance="HASHI1",
        operation="clone",
        include_agent_secrets=True,
        include_telegram_secret=False,
        secret_passphrase=TOKEN,
    )
    lifecycle_calls = []
    monkeypatch.setattr(
        remote_server,
        "_request_workbench_agent_lifecycle",
        lambda agent, *, action: lifecycle_calls.append((agent, action))
        or {"ok": True, "agent": agent, "action": action},
    )
    monkeypatch.setattr(
        remote_server,
        "_fetch_workbench_health",
        lambda timeout=1.0: {"ok": True, "agents": ["zelda_1"]},
    )
    client = _client(target)

    resolved = _post(
        client,
        "/agent-move/v1/resolve",
        {
            "from_instance": "HASHI1",
            "source_agent_id": "zelda",
            "operation": "clone",
        },
    )
    assert resolved.status_code == 200
    assert resolved.json()["target_agent_id"] == "zelda_1"

    raw = package_path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    envelope = encrypt_package_transport(
        raw,
        shared_token=TOKEN,
        source_instance="HASHI1",
        target_instance="HASHI2",
        package_sha256=digest,
    )
    staged = _post(
        client,
        "/agent-move/v1/stage",
        {
            "from_instance": "HASHI1",
            "encryption": ENVELOPE_SCHEME,
            "package_b64": base64.b64encode(envelope).decode("ascii"),
            "sha256": digest,
            "operation": "clone",
            "target_agent_id": "zelda_1",
        },
    )
    assert staged.status_code == 200
    assert staged.json()["target_agent_id"] == "zelda_1"
    for action in ("commit", "activate", "start", "finalize"):
        response = _post(
            client,
            f"/agent-move/v1/{action}",
            {"from_instance": "HASHI1", "package_id": package.package_id},
        )
        assert response.status_code == 200, response.text
    assert response.json()["status"] == "completed"
    assert lifecycle_calls == [("zelda_1", "start")]

    rows = json.loads((target / "agents.json").read_text())["agents"]
    clone = next(row for row in rows if row["name"] == "zelda_1")
    assert clone["is_active"] is True
    assert clone["telegram_token_key"] == "zelda_1"
    secrets = json.loads((target / "secrets.json").read_text())
    assert "zelda_1" not in secrets
    assert secrets["zelda_1_api_key"] == "safe-agent-key"


def test_remote_agent_move_rejects_authenticated_source_mismatch(tmp_path):
    source = _root(tmp_path, "source", "HASHI1", with_agent=True)
    target = _root(tmp_path, "target", "HASHI2", with_agent=False)
    package_path = tmp_path / "zelda.hashi-agent"
    create_agent_move_package(source, "zelda", package_path, source_instance="HASHI1")
    raw = package_path.read_bytes()
    client = _client(target)
    payload = {
        "from_instance": "HASHI9",
        "encryption": ENVELOPE_SCHEME,
        "package_b64": base64.b64encode(raw).decode("ascii"),
        "sha256": hashlib.sha256(raw).hexdigest(),
    }
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")

    response = client.post(
        "/agent-move/v1/stage",
        content=body,
        headers=_headers("POST", "/agent-move/v1/stage", body),
    )

    assert response.status_code == 401
    assert not (target / "workspaces" / "zelda").exists()


def test_remote_agent_move_rejects_plaintext_package_transport(tmp_path):
    source = _root(tmp_path, "source", "HASHI1", with_agent=True)
    target = _root(tmp_path, "target", "HASHI2", with_agent=False)
    package_path = tmp_path / "zelda.hashi-agent"
    create_agent_move_package(source, "zelda", package_path, source_instance="HASHI1")
    raw = package_path.read_bytes()
    client = _client(target)

    response = _post(
        client,
        "/agent-move/v1/stage",
        {
            "from_instance": "HASHI1",
            "encryption": "none",
            "package_b64": base64.b64encode(raw).decode("ascii"),
            "sha256": hashlib.sha256(raw).hexdigest(),
        },
    )

    assert response.status_code == 400
    assert "Encrypted Agent move transport is required" in response.json()["error"]


@pytest.mark.skipif(
    os.name == "nt",
    reason="agent.md and AGENT.md cannot coexist on a case-insensitive Windows tree",
)
def test_schema2_upload_is_refused_clearly_by_schema1_receiver(tmp_path, monkeypatch):
    source = _root(tmp_path, "source", "HASHI1", with_agent=True)
    (source / "workspaces" / "zelda" / "AGENT.md").write_text(
        "retained historical identity",
        encoding="utf-8",
    )
    package_path = tmp_path / "zelda-schema2.hashi-agent"
    create_agent_move_package(
        source,
        "zelda",
        package_path,
        source_instance="HASHI1",
        schema_version=2,
    )
    client = AgentMoveRemoteClient(
        base_url="http://127.0.0.1:8767",
        target_instance="HASHI2",
        source_instance="HASHI1",
        shared_token=TOKEN,
        capabilities={
            "capability": "agent_move_receive_v1",
            "schema_min": 1,
            "schema_max": 1,
            "max_package_bytes": 256 * 1024 * 1024,
            "package_encryption": [ENVELOPE_SCHEME],
        },
    )
    monkeypatch.setattr(
        AgentMoveRemoteClient,
        "_request",
        lambda *args, **kwargs: pytest.fail("incompatible package must not upload"),
    )

    with pytest.raises(AgentMoveRemoteError, match="schema 2.*schema 1"):
        client.stage(package_path)


def test_remote_agent_move_rejects_tampered_encrypted_transport(tmp_path):
    source = _root(tmp_path, "source", "HASHI1", with_agent=True)
    target = _root(tmp_path, "target", "HASHI2", with_agent=False)
    package_path = tmp_path / "zelda.hashi-agent"
    create_agent_move_package(source, "zelda", package_path, source_instance="HASHI1")
    raw = package_path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    envelope = bytearray(
        encrypt_package_transport(
            raw,
            shared_token=TOKEN,
            source_instance="HASHI1",
            target_instance="HASHI2",
            package_sha256=digest,
        )
    )
    envelope[-1] ^= 1

    response = _post(
        _client(target),
        "/agent-move/v1/stage",
        {
            "from_instance": "HASHI1",
            "encryption": ENVELOPE_SCHEME,
            "package_b64": base64.b64encode(envelope).decode("ascii"),
            "sha256": digest,
        },
    )

    assert response.status_code == 400
    assert "authentication failed" in response.json()["error"]
    assert not (target / "workspaces" / "zelda").exists()


def test_remote_client_rejects_unsigned_receiver_success(monkeypatch):
    class _UnsignedResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b'{"ok":true,"status":"activated_pending_reboot"}'

    monkeypatch.setattr(
        "orchestrator.agent_move.remote_client.urllib_request.urlopen",
        lambda *_args, **_kwargs: _UnsignedResponse(),
    )

    with pytest.raises(AgentMoveRemoteError, match="response authentication failed"):
        _request_json(
            "http://127.0.0.1:8767/agent-move/v1/activate",
            method="POST",
            payload={"from_instance": "HASHI1", "package_id": "move-id"},
            shared_token=TOKEN,
            from_instance="HASHI1",
        )


def test_agent_move_tls_configuration_does_not_silently_downgrade():
    assert _candidate_base_urls(
        {"host": "example.test", "use_tls": True},
        8767,
    ) == ["https://example.test:8767"]
    assert _candidate_base_urls(
        {
            "host": "example.test",
            "use_tls": True,
            "allow_http_fallback": True,
        },
        8767,
    ) == ["https://example.test:8767", "http://example.test:8767"]


def test_streamed_move_authenticates_before_staging_and_cleans_failed_upload(tmp_path):
    from urllib.parse import urlencode
    from orchestrator.agent_move.transport_crypto import encrypt_package_file
    source = _root(tmp_path, "source", "HASHI1", with_agent=True)
    target = _root(tmp_path, "target", "HASHI2", with_agent=False)
    package_path = tmp_path / "stream.hashi-agent"
    create_agent_move_package(source, "zelda", package_path, source_instance="HASHI1")
    digest = hashlib.sha256(package_path.read_bytes()).hexdigest()
    encrypted = tmp_path / "wire.enc"
    encrypt_package_file(package_path, encrypted, shared_token=TOKEN,
                         source_instance="HASHI1", target_instance="HASHI2", package_sha256=digest)
    wire = encrypted.read_bytes()
    path = "/agent-move/v1/stage-stream?" + urlencode(sorted({
        "from_instance": "HASHI1", "sha256": digest, "operation": "move", "target_agent_id": "zelda"}.items()))
    client = _client(target)
    assert client.post(path, content=wire).status_code == 401
    for broken in (wire[:-1], wire[:-1] + bytes([wire[-1] ^ 1])):
        response = client.post(path, content=broken, headers=_headers("POST", path))
        assert response.status_code == 400
        assert json.loads((target / "agents.json").read_text())["agents"] == []
        assert not list((target / "state" / "agent_moves").glob("incoming/*/state.json"))
    response = client.post(path, content=iter([wire[:31], wire[31:]]), headers=_headers("POST", path))
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "staged"
    assert json.loads((target / "agents.json").read_text())["agents"] == []


def test_streaming_client_uses_file_body_and_verifies_receiver_proof(tmp_path, monkeypatch):
    from urllib.parse import urlsplit
    from orchestrator.agent_move.service import receiver_capabilities
    source = _root(tmp_path, "source", "HASHI1", with_agent=True)
    target = _root(tmp_path, "target", "HASHI2", with_agent=False)
    path = tmp_path / "stream.hashi-agent"
    create_agent_move_package(source, "zelda", path, source_instance="HASHI1")
    client = _client(target)

    class Response:
        def __init__(self, raw): self.raw = raw
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, limit): return self.raw[:limit]

    def send(request, **kwargs):
        assert hasattr(request.data, "read")
        url = urlsplit(request.full_url)
        def chunks():
            while chunk := request.data.read(127):
                yield chunk
        response = client.post(url.path + "?" + url.query, content=chunks(), headers=dict(request.header_items()))
        assert response.status_code == 200, response.text
        return Response(response.content)

    monkeypatch.setattr("orchestrator.agent_move.remote_client.urllib_request.urlopen", send)
    remote = AgentMoveRemoteClient("http://receiver", "HASHI2", "HASHI1", TOKEN, receiver_capabilities(target))
    assert remote.stage(path, operation="move", target_agent_id="zelda")["status"] == "staged"
