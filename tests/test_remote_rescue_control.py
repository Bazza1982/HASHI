from __future__ import annotations

import json
import os

from fastapi.testclient import TestClient

from remote.api.server import create_app
from remote.local_http import local_http_url
from remote.protocol_manager import ProtocolManager, build_default_capabilities
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


def test_hashi_rescue_status_requires_auth_when_lan_mode_off(tmp_path):
    app = create_app(
        {"instance_id": "HASHI_TEST"},
        PairingManager(storage_dir=tmp_path / "pairing", lan_mode=False),
        TerminalExecutor(),
        hashi_root=str(tmp_path),
        workbench_port=1,
    )
    client = TestClient(app, raise_server_exceptions=False)

    response = client.get("/control/hashi/status")

    assert response.status_code == 401


def test_hashi_rescue_status_reports_offline_when_workbench_missing(tmp_path):
    client = _client(tmp_path)

    response = client.get("/control/hashi/status")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["hashi_running"] is False
    assert body["pid_alive"] is False
    assert body["pid_file_exists"] is False
    assert body["state"] == "offline"
    assert body["workbench_url"] == local_http_url(1, "/api/health")


def test_hashi_rescue_status_distinguishes_stale_pid(tmp_path):
    (tmp_path / ".bridge_u_f.pid").write_text("99999999", encoding="utf-8")
    client = _client(tmp_path)

    response = client.get("/control/hashi/status")

    assert response.status_code == 200
    body = response.json()
    assert body["state"] == "stale_pid"
    assert body["pid_file_exists"] is True
    assert body["pid"] == 99999999
    assert body["pid_alive"] is False


def test_hashi_rescue_start_requires_l3_restart(tmp_path):
    client = _client(tmp_path, max_level=AuthLevel.L2_WRITE)

    response = client.post("/control/hashi/start", json={"reason": "test"})

    assert response.status_code == 403
    assert response.json()["ok"] is False
    assert "L3_RESTART" in response.json()["error"]


def test_hashi_rescue_logs_returns_bounded_fixed_log_tail(tmp_path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    (log_dir / "remote_rescue_hashi_start.log").write_text("one\ntwo\nthree\n", encoding="utf-8")
    client = _client(tmp_path)

    response = client.get("/control/hashi/logs?name=start&tail=2")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["name"] == "start"
    assert body["exists"] is True
    assert body["lines"] == ["two", "three"]


def test_hashi_rescue_logs_rejects_unknown_log_name(tmp_path):
    client = _client(tmp_path)

    response = client.get("/control/hashi/logs?name=../../secrets")

    assert response.status_code == 400
    assert response.json()["ok"] is False


def test_hashi_rescue_start_writes_audit_when_already_running(tmp_path):
    (tmp_path / ".bridge_u_f.pid").write_text(str(os.getpid()), encoding="utf-8")
    client = _client(tmp_path, max_level=AuthLevel.L3_RESTART)

    response = client.post("/control/hashi/start", json={"reason": "already alive"})

    assert response.status_code == 200
    body = response.json()
    assert body["started"] is False
    audit_path = tmp_path / "logs" / "remote_rescue_audit.jsonl"
    record = json.loads(audit_path.read_text(encoding="utf-8").splitlines()[-1])
    assert record["requester"] == "lan-client"
    assert record["reason"] == "already alive"
    assert record["outcome"] == "already_running"
    assert record["pid"] == os.getpid()


def test_hashi_rescue_start_failure_writes_structured_audit(tmp_path):
    client = _client(tmp_path, max_level=AuthLevel.L3_RESTART)

    response = client.post("/control/hashi/start", json={"reason": "missing launcher"})

    assert response.status_code == 500
    audit_path = tmp_path / "logs" / "remote_rescue_audit.jsonl"
    record = json.loads(audit_path.read_text(encoding="utf-8").splitlines()[-1])
    assert record["requester"] == "lan-client"
    assert record["reason"] == "missing launcher"
    assert record["outcome"] == "failed"
    assert "launcher" in record["error"]


def test_rescue_capabilities_advertise_start_only_when_l3_enabled():
    assert "rescue_control" in build_default_capabilities(rescue_start_enabled=False)
    assert "rescue_start" not in build_default_capabilities(rescue_start_enabled=False)
    assert "rescue_start" in build_default_capabilities(rescue_start_enabled=True)


def test_protocol_status_reports_dynamic_rescue_capabilities(tmp_path):
    protocol = ProtocolManager(
        hashi_root=tmp_path,
        instance_info={
            "instance_id": "HASHI_TEST",
            "remote_supervisor": {"mode": "supervised", "source": "test"},
        },
        peer_registry=None,
        workbench_port=1,
        local_capabilities=build_default_capabilities(rescue_start_enabled=True),
    )
    app = create_app(
        {"instance_id": "HASHI_TEST"},
        PairingManager(storage_dir=tmp_path / "pairing", lan_mode=True),
        TerminalExecutor(max_allowed_level=AuthLevel.L3_RESTART),
        protocol_manager=protocol,
        hashi_root=str(tmp_path),
        workbench_port=1,
    )
    client = TestClient(app)

    response = client.get("/protocol/status")

    assert response.status_code == 200
    body = response.json()
    assert "rescue_control" in body["capabilities"]
    assert "rescue_start" in body["capabilities"]
    assert body["rescue_start_enabled"] is True
    assert body["remote_supervisor"]["mode"] == "supervised"


def test_protocol_status_reports_rescue_start_disabled_at_l2(tmp_path):
    protocol = ProtocolManager(
        hashi_root=tmp_path,
        instance_info={"instance_id": "HASHI_TEST"},
        peer_registry=None,
        workbench_port=1,
        local_capabilities=build_default_capabilities(rescue_start_enabled=False),
    )
    app = create_app(
        {"instance_id": "HASHI_TEST"},
        PairingManager(storage_dir=tmp_path / "pairing", lan_mode=True),
        TerminalExecutor(max_allowed_level=AuthLevel.L2_WRITE),
        protocol_manager=protocol,
        hashi_root=str(tmp_path),
        workbench_port=1,
    )
    client = TestClient(app)

    response = client.get("/protocol/status")

    assert response.status_code == 200
    body = response.json()
    assert body["rescue_start_enabled"] is False
    assert body["rescue_start_requirement"] == "L3_RESTART"
    assert "rescue_start" not in body["capabilities"]
