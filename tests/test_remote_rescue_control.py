from __future__ import annotations

import json
import os

from fastapi.testclient import TestClient

from remote.api.server import create_app
from remote.local_http import local_http_url
from remote.protocol_manager import ProtocolManager, build_default_capabilities
from remote.security.pairing import PairingManager
from remote.security.shared_token import build_auth_headers
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


def test_hashi_rescue_status_accepts_shared_token_hmac_when_lan_mode_off(tmp_path):
    (tmp_path / "secrets.json").write_text('{"hashi_remote_shared_token":"test-secret"}', encoding="utf-8")
    app = create_app(
        {"instance_id": "HASHI_TEST"},
        PairingManager(storage_dir=tmp_path / "pairing", lan_mode=False),
        TerminalExecutor(),
        hashi_root=str(tmp_path),
        workbench_port=1,
    )
    client = TestClient(app, raise_server_exceptions=False)
    headers = build_auth_headers(
        shared_token="test-secret",
        method="GET",
        path="/control/hashi/status",
        from_instance="HASHI1",
        body_bytes=b"",
    )

    response = client.get("/control/hashi/status", headers=headers)

    assert response.status_code == 200
    assert response.json()["ok"] is True


def test_hashi_rescue_start_accepts_shared_token_hmac_when_lan_mode_off(tmp_path):
    (tmp_path / "secrets.json").write_text('{"hashi_remote_shared_token":"test-secret"}', encoding="utf-8")
    (tmp_path / ".bridge_u_f.pid").write_text(str(os.getpid()), encoding="utf-8")
    app = create_app(
        {"instance_id": "HASHI_TEST"},
        PairingManager(storage_dir=tmp_path / "pairing", lan_mode=False),
        TerminalExecutor(max_allowed_level=AuthLevel.L3_RESTART),
        hashi_root=str(tmp_path),
        workbench_port=1,
    )
    client = TestClient(app, raise_server_exceptions=False)
    body = b'{"reason":"shared token"}'
    headers = build_auth_headers(
        shared_token="test-secret",
        method="POST",
        path="/control/hashi/start",
        from_instance="HASHI1",
        body_bytes=body,
    )
    headers["Content-Type"] = "application/json"

    response = client.post("/control/hashi/start", content=body, headers=headers)

    assert response.status_code == 200
    assert response.json()["already_running"] is True
    audit_path = tmp_path / "logs" / "remote_rescue_audit.jsonl"
    record = json.loads(audit_path.read_text(encoding="utf-8").splitlines()[-1])
    assert record["requester"] == "HASHI1"


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
    assert body["requested_tail"] == 2
    assert body["effective_tail"] == 2
    assert body["tail_truncated"] is False
    assert body["lines"] == ["two", "three"]


def test_hashi_rescue_logs_caps_tail_at_1000(tmp_path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    payload = "".join(f"line-{idx}\n" for idx in range(1205))
    (log_dir / "remote_rescue_hashi_start.log").write_text(payload, encoding="utf-8")
    client = _client(tmp_path)

    response = client.get("/control/hashi/logs?name=start&tail=5000")

    assert response.status_code == 200
    body = response.json()
    assert body["requested_tail"] == 5000
    assert body["effective_tail"] == 1000
    assert body["tail_truncated"] is True
    assert len(body["lines"]) == 1000
    assert body["lines"][0] == "line-205"
    assert body["lines"][-1] == "line-1204"


def test_hashi_rescue_logs_rejects_non_positive_tail(tmp_path):
    client = _client(tmp_path)

    response = client.get("/control/hashi/logs?name=start&tail=0")

    assert response.status_code == 400
    assert "positive integer" in response.json()["error"]


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
    assert record["reason_truncated"] is False
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
    assert record["status_state"] == "offline"
    assert "launcher" in record["error"]


def test_hashi_rescue_start_sanitizes_and_truncates_reason_in_audit(tmp_path):
    (tmp_path / ".bridge_u_f.pid").write_text(str(os.getpid()), encoding="utf-8")
    client = _client(tmp_path, max_level=AuthLevel.L3_RESTART)
    reason = ("first line\nsecond line\r\n" + ("x" * 600))

    response = client.post("/control/hashi/start", json={"reason": reason})

    assert response.status_code == 200
    body = response.json()
    assert body["started"] is False
    assert body["reason_truncated"] is True
    assert "\n" not in body["reason"]
    assert len(body["reason"]) == 500
    audit_path = tmp_path / "logs" / "remote_rescue_audit.jsonl"
    record = json.loads(audit_path.read_text(encoding="utf-8").splitlines()[-1])
    assert record["requester"] == "lan-client"
    assert record["reason_truncated"] is True
    assert record["reason_original_length"] > 500
    assert "\n" not in record["reason"]
    assert "\r" not in record["reason"]
    assert len(record["reason"]) == 500


def test_hashi_rescue_start_returns_structured_windows_launcher_fields(tmp_path, monkeypatch):
    client = _client(tmp_path, max_level=AuthLevel.L3_RESTART)
    sleep_calls = {"count": 0}

    monkeypatch.setattr("remote.api.server.platform.system", lambda: "Windows")
    monkeypatch.setattr(
        "remote.api.server._start_hashi_process",
        lambda: {
            "pid": 4242,
            "command": [
                "powershell.exe",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(tmp_path / "bin" / "bridge_ctl.ps1"),
                "-Action",
                "start",
                "-Resume",
            ],
            "log_path": str(tmp_path / "logs" / "remote_rescue_hashi_start.log"),
            "launcher_kind": "powershell.exe",
            "platform": "windows",
        },
    )
    monkeypatch.setattr(
        "remote.api.server._hashi_control_status",
        lambda: {
            "ok": True,
            "state": "offline",
            "hashi_running": False,
            "pid_file_exists": False,
            "pid": None,
            "pid_alive": False,
            "workbench_url": "http://127.0.0.1:1/api/health",
            "workbench_health": None,
        },
    )
    async def fake_sleep(*_args, **_kwargs):
        sleep_calls["count"] += 1
        return None

    monkeypatch.setattr("remote.api.server.asyncio.sleep", fake_sleep)

    response = client.post("/control/hashi/start", json={"reason": "windows start"})

    assert response.status_code == 200
    body = response.json()
    assert body["started"] is True
    assert body["pid"] == 4242
    assert body["launcher_kind"] == "powershell.exe"
    assert body["platform"] == "windows"
    assert body["command"][0] == "powershell.exe"
    assert "bridge_ctl.ps1" in " ".join(body["command"])
    assert sleep_calls["count"] >= 1


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
