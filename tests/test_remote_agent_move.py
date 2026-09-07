from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from orchestrator.agent_move.package import create_agent_move_package
from orchestrator.agent_move.remote_client import (
    AgentMoveRemoteError,
    _candidate_base_urls,
    _request_json,
)
from orchestrator.agent_move.transport_crypto import (
    ENVELOPE_SCHEME,
    encrypt_package_transport,
)
from orchestrator.pcm import render_pcm_document
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
