from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from remote.api import server
from remote.api.server import create_app
from remote.security.pairing import PairingManager
from remote.security.shared_token import (
    HEADER_NONCE,
    build_auth_headers,
    verify_response_auth,
)
from remote.terminal.executor import TerminalExecutor


TOKEN = "version-shared-secret"


def _client(tmp_path: Path) -> TestClient:
    (tmp_path / "secrets.json").write_text(
        json.dumps({"hashi_remote_shared_token": TOKEN}),
        encoding="utf-8",
    )
    app = create_app(
        {
            "instance_id": "HASHI2",
            "display_name": "Target",
            "remote_port": 8767,
        },
        PairingManager(storage_dir=tmp_path / "pairing", lan_mode=True),
        TerminalExecutor(),
        hashi_root=str(tmp_path),
    )
    return TestClient(app)


def _headers() -> dict[str, str]:
    return build_auth_headers(
        shared_token=TOKEN,
        method="GET",
        path="/version/v1",
        from_instance="HASHI1",
        body_bytes=b"",
    )


def test_remote_version_requires_hmac_and_signs_structured_facts(
    tmp_path: Path, monkeypatch
) -> None:
    client = _client(tmp_path)
    facts = {
        "ok": True,
        "schema_version": 1,
        "collected_at": "2026-09-09T12:00:00Z",
        "product": {"name": "HASHI", "version": "4.0.0a2"},
        "instance": {"id": "HASHI2", "environment": "wsl"},
        "running": {"commit": "a" * 40},
        "source": {"commit": "a" * 40},
        "compatibility": {},
        "state": {"code": "running_matches_source", "reasons": []},
    }
    monkeypatch.setattr(
        server,
        "_fetch_workbench_json",
        lambda path, *, timeout: facts if path == "/api/version" else None,
    )

    assert client.get("/version/v1").status_code == 401

    headers = _headers()
    response = client.get("/version/v1", headers=headers)
    assert response.status_code == 200
    payload = response.json()
    proof = payload.pop("response_auth")
    assert payload["instance"]["id"] == "HASHI2"
    assert payload["authenticated_instance"] == "HASHI1"
    assert verify_response_auth(
        shared_token=TOKEN,
        request_nonce=headers[HEADER_NONCE],
        payload=payload,
        response_auth=proof,
    )


def test_remote_version_does_not_fabricate_when_workbench_is_offline(
    tmp_path: Path, monkeypatch
) -> None:
    client = _client(tmp_path)
    monkeypatch.setattr(
        server,
        "_fetch_workbench_json",
        lambda path, *, timeout: None,
    )

    response = client.get("/version/v1", headers=_headers())

    assert response.status_code == 503
    assert response.json()["ok"] is False
    assert "version facts" in response.json()["error"]
