from __future__ import annotations

import base64
import hashlib
import json

from fastapi.testclient import TestClient

from remote.api.server import create_app
from remote.security.pairing import PairingManager
from remote.security.shared_token import HEADER_AUTH_SCHEME
from remote.terminal.executor import TerminalExecutor
from tools.remote_file_transfer import _build_request_headers, _split_remote_path


def _client(tmp_path):
    app = create_app(
        {"instance_id": "HASHI_TEST"},
        PairingManager(storage_dir=tmp_path / "pairing", lan_mode=True),
        TerminalExecutor(),
        hashi_root=str(tmp_path),
    )
    return TestClient(app)


def _payload(dest_path: str, data: bytes, **overrides):
    payload = {
        "dest_path": dest_path,
        "content_b64": base64.b64encode(data).decode("ascii"),
        "sha256": hashlib.sha256(data).hexdigest(),
        "overwrite": False,
        "create_dirs": True,
    }
    payload.update(overrides)
    return payload


def test_file_push_writes_relative_path_atomically_and_stats(tmp_path):
    client = _client(tmp_path)
    data = b"hello remote file transfer"

    response = client.post("/files/push", json=_payload("incoming/report.txt", data))

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["bytes_written"] == len(data)
    assert body["sha256"] == hashlib.sha256(data).hexdigest()
    assert (tmp_path / "incoming" / "report.txt").read_bytes() == data

    stat = client.get("/files/stat", params={"path": "incoming/report.txt"})
    assert stat.status_code == 200
    assert stat.json()["sha256"] == hashlib.sha256(data).hexdigest()


def test_file_push_refuses_overwrite_without_flag(tmp_path):
    client = _client(tmp_path)
    target = tmp_path / "existing.txt"
    target.write_text("old", encoding="utf-8")

    response = client.post("/files/push", json=_payload(str(target), b"new"))

    assert response.status_code == 409
    assert response.json()["ok"] is False
    assert target.read_text(encoding="utf-8") == "old"


def test_file_push_can_overwrite_with_checksum(tmp_path):
    client = _client(tmp_path)
    target = tmp_path / "existing.txt"
    target.write_text("old", encoding="utf-8")

    response = client.post("/files/push", json=_payload(str(target), b"new", overwrite=True))

    assert response.status_code == 200
    assert target.read_bytes() == b"new"


def test_file_push_rejects_sha_mismatch(tmp_path):
    client = _client(tmp_path)

    response = client.post("/files/push", json=_payload("bad.txt", b"data", sha256="0" * 64))

    assert response.status_code == 400
    assert "sha256 mismatch" in response.json()["error"]
    assert not (tmp_path / "bad.txt").exists()


def test_file_push_rejects_relative_traversal(tmp_path):
    client = _client(tmp_path)

    response = client.post("/files/push", json=_payload("../escape.txt", b"data"))

    assert response.status_code == 400
    assert "inside the Hashi root" in response.json()["error"]
    assert not (tmp_path.parent / "escape.txt").exists()


def test_split_remote_path_preserves_windows_drive_colon():
    instance, path = _split_remote_path(r"HASHI9:C:\Users\me\Desktop\report.txt")

    assert instance == "HASHI9"
    assert path == r"C:\Users\me\Desktop\report.txt"


def test_build_request_headers_prefers_bearer_over_shared_token():
    headers = _build_request_headers(
        url="http://127.0.0.1:8766/files/push",
        method="POST",
        data=json.dumps({"hello": "world"}).encode("utf-8"),
        token="legacy-token",
        shared_token="shared-secret",
        from_instance="HASHI1",
    )

    assert headers["Authorization"] == "Bearer legacy-token"
    assert HEADER_AUTH_SCHEME not in headers


def test_build_request_headers_adds_hmac_when_shared_token_selected():
    body = json.dumps({"hello": "world"}).encode("utf-8")

    headers = _build_request_headers(
        url="http://127.0.0.1:8766/files/stat?path=incoming%2Freport.txt",
        method="POST",
        data=body,
        token=None,
        shared_token="shared-secret",
        from_instance="hashi1",
    )

    assert headers[HEADER_AUTH_SCHEME] == "hashi-shared-hmac-v1"
    assert headers["X-Hashi-From-Instance"] == "HASHI1"
    assert headers["X-Hashi-Digest"]


def test_build_request_headers_changes_digest_when_query_changes():
    first = _build_request_headers(
        url="http://127.0.0.1:8766/files/stat?path=one.txt",
        method="GET",
        data=None,
        token=None,
        shared_token="shared-secret",
        from_instance="HASHI1",
    )
    second = _build_request_headers(
        url="http://127.0.0.1:8766/files/stat?path=two.txt",
        method="GET",
        data=None,
        token=None,
        shared_token="shared-secret",
        from_instance="HASHI1",
    )

    assert first["X-Hashi-Digest"] != second["X-Hashi-Digest"]


def test_build_request_headers_requires_sender_identity_for_shared_token(monkeypatch):
    monkeypatch.delenv("HASHI_INSTANCE_ID", raising=False)
    monkeypatch.setattr("tools.remote_file_transfer._load_local_instance_id", lambda: None)

    try:
        _build_request_headers(
            url="http://127.0.0.1:8766/files/stat",
            method="GET",
            data=None,
            token=None,
            shared_token="shared-secret",
            from_instance=None,
        )
    except ValueError as exc:
        assert "shared-token mode requires --from-instance" in str(exc)
    else:
        raise AssertionError("expected ValueError when shared-token sender identity is unavailable")
