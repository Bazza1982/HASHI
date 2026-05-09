from __future__ import annotations

from fastapi.testclient import TestClient

from remote.api.server import create_app
from remote.security.pairing import PairingManager
from remote.terminal.executor import AuthLevel, TerminalExecutor


def _client(tmp_path, *, max_level=AuthLevel.L2_WRITE):
    app = create_app(
        {"instance_id": "HASHI_TEST"},
        PairingManager(storage_dir=tmp_path / "pairing", lan_mode=True),
        TerminalExecutor(max_allowed_level=max_level),
        hashi_root=str(tmp_path),
        workbench_port=1,
    )
    return TestClient(app)


def test_hashi_rescue_status_reports_offline_when_workbench_missing(tmp_path):
    client = _client(tmp_path)

    response = client.get("/control/hashi/status")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["hashi_running"] is False
    assert body["pid_alive"] is False
    assert body["workbench_url"] == "http://127.0.0.1:1/api/health"


def test_hashi_rescue_start_requires_l3_restart(tmp_path):
    client = _client(tmp_path, max_level=AuthLevel.L2_WRITE)

    response = client.post("/control/hashi/start", json={"reason": "test"})

    assert response.status_code == 403
    assert response.json()["ok"] is False
    assert "L3_RESTART" in response.json()["error"]
