from __future__ import annotations

import base64
import hashlib

from fastapi.testclient import TestClient

from remote.api.server import create_app
from remote.security.pairing import PairingManager
from remote.terminal.executor import TerminalExecutor
from tools.remote_file_transfer import _split_remote_path


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
