from __future__ import annotations

import json
import os
import threading

import pytest
from fastapi.testclient import TestClient

from remote.api import server as remote_server
from remote.api.server import _request_workbench_reboot, create_app
from remote.local_http import local_http_url
from remote.protocol_manager import ProtocolManager, build_default_capabilities
from remote.security.pairing import PairingManager
from remote.security.shared_token import build_auth_headers
from remote.terminal.executor import AuthLevel, TerminalExecutor
from orchestrator.pathing import instance_runtime_dir


def _write_hashi_pid(root, value: str) -> None:
    path = instance_runtime_dir(root) / "process.pid"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


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
    _write_hashi_pid(tmp_path, str(os.getpid()))
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
    _write_hashi_pid(tmp_path, "99999999")
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
    _write_hashi_pid(tmp_path, str(os.getpid()))
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
    _write_hashi_pid(tmp_path, str(os.getpid()))
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
    status_calls = {"count": 0}

    monkeypatch.setattr("remote.api.server.platform.system", lambda: "Windows")
    monkeypatch.setattr(
        "remote.api.server._start_hashi_process",
        lambda: {
            "pid": 4242,
            "command": [
                "cmd.exe",
                "/c",
                str(tmp_path / "bin" / "bridge-u.bat"),
                "--resume-last",
                "--no-pause",
            ],
            "log_path": str(tmp_path / "logs" / "remote_rescue_hashi_start.log"),
            "launcher_kind": "cmd.exe",
            "platform": "windows",
        },
    )
    def fake_status():
        status_calls["count"] += 1
        running = status_calls["count"] >= 3
        return {
            "ok": True,
            "state": "online" if running else "offline",
            "hashi_running": running,
            "pid_file_exists": False,
            "pid": None,
            "pid_alive": False,
            "workbench_url": "http://127.0.0.1:1/api/health",
            "workbench_health": None,
        }

    monkeypatch.setattr("remote.api.server._hashi_control_status", fake_status)

    async def fake_sleep(*_args, **_kwargs):
        sleep_calls["count"] += 1
        return None

    monkeypatch.setattr("remote.api.server.asyncio.sleep", fake_sleep)

    response = client.post("/control/hashi/start", json={"reason": "windows start"})

    assert response.status_code == 200
    body = response.json()
    assert body["started"] is True
    assert body["pid"] == 4242
    assert body["launcher_kind"] == "cmd.exe"
    assert body["platform"] == "windows"
    assert body["command"][:2] == ["cmd.exe", "/c"]
    assert "bridge-u.bat" in " ".join(body["command"])
    assert sleep_calls["count"] == 1


def test_rescue_capabilities_advertise_start_only_when_l3_enabled():
    assert "rescue_control" in build_default_capabilities(rescue_start_enabled=False)
    assert "rescue_start" not in build_default_capabilities(rescue_start_enabled=False)
    enabled = build_default_capabilities(rescue_start_enabled=True)
    assert "rescue_start" in enabled
    assert "rescue_restart" in enabled
    assert "rescue_reboot" in enabled


def test_hashi_rescue_restart_uses_fixed_out_of_process_launcher(
    tmp_path, monkeypatch
):
    client = _client(tmp_path, max_level=AuthLevel.L3_RESTART)
    states = iter(
        (
            {
                "ok": True,
                "state": "running",
                "hashi_running": True,
                "pid": 4141,
                "pid_alive": True,
                "workbench_health": {
                    "ok": True,
                    "ready": True,
                    "instance_id": "HASHI_TEST",
                    "runtime": {"core_api": "4", "function_api": "4"},
                    "shared_functions": {"generation_id": "sha256:abc"},
                },
            },
            {
                "ok": True,
                "state": "running",
                "hashi_running": True,
                "pid": 6161,
                "pid_alive": True,
                "workbench_health": {
                    "ok": True,
                    "ready": True,
                    "instance_id": "HASHI_TEST",
                    "runtime": {"core_api": "4", "function_api": "4"},
                    "shared_functions": {"generation_id": "sha256:abc"},
                },
            },
        )
    )
    monkeypatch.setattr(
        "remote.api.server._restart_hashi_process",
        lambda: {
            "pid": 5252,
            "command": [str(tmp_path / "bin" / "bridge-u.sh"), "--force"],
            "log_path": str(tmp_path / "logs" / "remote_rescue_hashi_restart.log"),
            "launcher_kind": "bridge-u.sh",
            "platform": "linux",
        },
    )
    monkeypatch.setattr("remote.api.server._hashi_control_status", lambda: next(states))
    monkeypatch.setattr("remote.api.server._process_exists", lambda pid: pid == 6161)

    response = client.post(
        "/control/hashi/restart",
        json={
            "reason": "stuck provider loop",
            "target_instance": "HASHI_TEST",
            "requester_agent": "hashiko",
            "request_source": "telegram",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["state"] == "completed"
    assert body["target_instance"] == "HASHI_TEST"
    assert body["evidence"]["old_pid"] == 4141
    assert body["evidence"]["old_pid_exited"] is True
    assert body["evidence"]["new_pid"] == 6161
    assert body["evidence"]["new_pid_differs"] is True
    assert body["evidence"]["backend_health_ok"] is True
    assert body["evidence"]["instance_matches"] is True
    assert body["evidence"]["runtime_version_verified"] is True
    assert body["evidence"]["generation_verified"] is True

    receipt = client.get(f"/control/hashi/restarts/{body['restart_id']}")
    assert receipt.status_code == 200
    assert receipt.json()["restart_id"] == body["restart_id"]
    assert receipt.json()["state"] == "completed"

    audit_path = tmp_path / "logs" / "remote_rescue_audit.jsonl"
    record = json.loads(audit_path.read_text(encoding="utf-8").splitlines()[-1])
    assert record["operation"] == "restart"
    assert record["outcome"] == "completed"
    assert record["restart_id"] == body["restart_id"]


def test_restart_evidence_accepts_dependency_and_generation_adoption_with_local_services_ready(
    monkeypatch,
):
    expected_runtime = {
        "python": "3.12.13",
        "platform_abi": ".cp312-win_amd64.pyd",
        "core_api": 3,
        "function_api": 3,
        "worker_model": "per-agent-process",
        "worker_protocol": 1,
        "generation_schema": 2,
        "dependency_digest": "sha256:before",
        "core_source_digest": "sha256:core",
    }
    actual_runtime = {**expected_runtime, "dependency_digest": "sha256:after"}
    status = {
        "hashi_running": True,
        "pid": 6161,
        "workbench_health": {
            "ok": True,
            "ready": False,
            "degraded": True,
            "status": "degraded",
            "instance_id": "HASHI_TEST",
            "runtime": actual_runtime,
            "shared_functions": {"generation_id": "sha256:new-generation"},
            "startup": {
                "services_ready": True,
                "failed_agents": 0,
                "issues": [
                    {
                        "code": "remote_already_running_degraded",
                        "summary": "Remote/HChat is unavailable; local startup will continue.",
                        "unaffected": ["local agents", "Workbench", "API"],
                    }
                ],
            },
        },
    }
    monkeypatch.setattr(remote_server, "_process_exists", lambda pid: pid == 6161)

    evidence = remote_server._restart_evidence(
        status,
        old_pid=4141,
        target_instance="HASHI_TEST",
        expected_runtime=expected_runtime,
        expected_generation="sha256:old-generation",
    )

    assert evidence["backend_health_ok"] is True
    assert evidence["backend_health_state"] == "degraded_local_services_ready"
    assert evidence["runtime_version_verified"] is True
    assert evidence["runtime_version_changed"] is True
    assert evidence["generation_verified"] is True
    assert evidence["generation_changed"] is True
    assert evidence["warning_codes"] == ["remote_already_running_degraded"]


def test_restart_evidence_rejects_unexpected_core_contract_change(monkeypatch):
    before = {
        "python": "3.12.13",
        "platform_abi": ".cp312-win_amd64.pyd",
        "core_api": 3,
        "function_api": 3,
        "worker_model": "per-agent-process",
        "worker_protocol": 1,
        "generation_schema": 2,
        "dependency_digest": "sha256:before",
        "core_source_digest": "sha256:core-before",
    }
    after = {**before, "core_source_digest": "sha256:core-after"}
    status = {
        "hashi_running": True,
        "pid": 6161,
        "workbench_health": {
            "ok": True,
            "ready": True,
            "instance_id": "HASHI_TEST",
            "runtime": after,
            "shared_functions": {"generation_id": "sha256:new-generation"},
        },
    }
    monkeypatch.setattr(remote_server, "_process_exists", lambda pid: pid == 6161)

    evidence = remote_server._restart_evidence(
        status,
        old_pid=4141,
        target_instance="HASHI_TEST",
        expected_runtime=before,
        expected_generation="sha256:old-generation",
    )

    assert evidence["runtime_version_verified"] is False
    assert evidence["runtime_contract_mismatches"] == ["core_source_digest"]


def test_hashi_rescue_restart_keeps_remote_health_responsive(
    tmp_path,
    monkeypatch,
):
    before = {
        "ok": True,
        "state": "running",
        "hashi_running": True,
        "pid": 4141,
        "pid_alive": True,
        "workbench_health": {
            "ok": True,
            "ready": True,
            "instance_id": "HASHI_TEST",
            "runtime": {"core_api": "4", "function_api": "4"},
            "shared_functions": {"generation_id": "sha256:abc"},
        },
    }
    after = {
        **before,
        "pid": 6161,
    }
    poll_started = threading.Event()
    release_poll = threading.Event()
    calls = 0

    def blocking_status():
        nonlocal calls
        calls += 1
        if calls == 1:
            return before
        poll_started.set()
        release_poll.wait(timeout=2.0)
        return after

    monkeypatch.setattr(remote_server, "_hashi_control_status", blocking_status)
    monkeypatch.setattr(remote_server, "_process_exists", lambda pid: pid == 6161)
    monkeypatch.setattr(
        remote_server,
        "_restart_hashi_process",
        lambda: {
            "pid": 5252,
            "command": ["fixed-launcher", "--force"],
            "log_path": str(tmp_path / "logs" / "restart.log"),
            "launcher_kind": "fixed-launcher",
            "platform": "windows",
        },
    )
    response_holder = {}

    with _client(tmp_path, max_level=AuthLevel.L3_RESTART) as client:
        restart_thread = threading.Thread(
            target=lambda: response_holder.setdefault(
                "response",
                client.post(
                    "/control/hashi/restart",
                    json={"reason": "test", "target_instance": "HASHI_TEST"},
                ),
            )
        )
        restart_thread.start()
        assert poll_started.wait(timeout=1.0)

        release_timer = threading.Timer(2.0, release_poll.set)
        release_timer.start()
        health_response = client.get("/health")
        poll_released_before_health = release_poll.is_set()
        release_poll.set()
        release_timer.cancel()
        restart_thread.join(timeout=3.0)

    assert health_response.status_code == 200
    assert poll_released_before_health is False
    assert not restart_thread.is_alive()
    assert response_holder["response"].status_code == 200


def test_windows_restart_uses_only_explicit_configured_service_target(
    tmp_path,
    monkeypatch,
):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "bridge_ctl.ps1").write_text("# fallback\n", encoding="utf-8")
    (bin_dir / "hashi_service_ctl.ps1").write_text("# service\n", encoding="utf-8")
    policy = tmp_path / "state" / "platform" / "live-runtime-protection.json"
    policy.parent.mkdir(parents=True)
    policy.write_text(
        json.dumps({"schema": 1, "service_targets": ["HASHI_TEST"]}),
        encoding="utf-8",
    )
    _client(tmp_path, max_level=AuthLevel.L3_RESTART)
    monkeypatch.setattr(remote_server.platform, "system", lambda: "Windows")

    command = remote_server._hashi_restart_command()

    assert "hashi_service_ctl.ps1" in " ".join(command)
    assert command[-2:] == ["-ServiceName", "HASHI_TEST"]


def test_windows_restart_uses_fixed_actuator_without_service_target(
    tmp_path,
    monkeypatch,
):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "bridge_ctl.ps1").write_text("# fallback\n", encoding="utf-8")
    (bin_dir / "hashi_restart_ctl.ps1").write_text("# actuator\n", encoding="utf-8")
    (bin_dir / "hashi_service_ctl.ps1").write_text("# service\n", encoding="utf-8")
    _client(tmp_path, max_level=AuthLevel.L3_RESTART)
    monkeypatch.setattr(remote_server.platform, "system", lambda: "Windows")

    command = remote_server._hashi_restart_command()

    assert "hashi_restart_ctl.ps1" in " ".join(command)
    assert "trigger" in command
    assert command[-2:] == ["-HashiRoot", str(tmp_path)]
    assert "hashi_service_ctl.ps1" not in " ".join(command)


def test_windows_restart_keeps_bridge_controller_as_legacy_fallback(
    tmp_path,
    monkeypatch,
):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "bridge_ctl.ps1").write_text("# fallback\n", encoding="utf-8")
    _client(tmp_path, max_level=AuthLevel.L3_RESTART)
    monkeypatch.setattr(remote_server.platform, "system", lambda: "Windows")

    command = remote_server._hashi_restart_command()

    assert "bridge_ctl.ps1" in " ".join(command)


def test_windows_service_restart_launcher_avoids_detached_process(
    tmp_path,
    monkeypatch,
):
    _client(tmp_path, max_level=AuthLevel.L3_RESTART)
    captured = {}

    class FakeProcess:
        pid = 9191

    def fake_popen(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return FakeProcess()

    monkeypatch.setattr(remote_server.platform, "system", lambda: "Windows")
    monkeypatch.setattr(remote_server.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(
        remote_server.subprocess,
        "CREATE_NEW_PROCESS_GROUP",
        0x00000200,
        raising=False,
    )
    monkeypatch.setattr(
        remote_server.subprocess,
        "DETACHED_PROCESS",
        0x00000008,
        raising=False,
    )
    monkeypatch.setattr(
        remote_server.subprocess,
        "CREATE_NO_WINDOW",
        0x08000000,
        raising=False,
    )

    remote_server._launch_hashi_process(
        ["powershell.exe", "-File", "hashi_service_ctl.ps1"],
        log_name="service-restart.log",
        detach_on_windows=False,
    )

    assert captured["kwargs"]["creationflags"] == 0x08000000


@pytest.mark.parametrize(
    ("command", "expected_detached"),
    [
        (
            ["powershell.exe", "-File", "C:/HASHI/bin/hashi_service_ctl.ps1"],
            False,
        ),
        (
            ["powershell.exe", "-File", "C:/HASHI/bin/hashi_restart_ctl.ps1"],
            False,
        ),
        (
            ["powershell.exe", "-File", "C:/HASHI/bin/bridge_ctl.ps1"],
            True,
        ),
    ],
)
def test_restart_process_selects_launch_mode_for_service_or_development(
    command,
    expected_detached,
    monkeypatch,
):
    captured = {}
    monkeypatch.setattr(remote_server, "_hashi_restart_command", lambda: command)

    def fake_launch(value, *, log_name, detach_on_windows):
        captured.update(
            command=value,
            log_name=log_name,
            detach_on_windows=detach_on_windows,
        )
        return {"pid": 9292}

    monkeypatch.setattr(remote_server, "_launch_hashi_process", fake_launch)

    assert remote_server._restart_hashi_process() == {"pid": 9292}
    assert captured["command"] == command
    assert captured["detach_on_windows"] is expected_detached


def test_hashi_rescue_restart_rejects_wrong_target_before_launch(tmp_path, monkeypatch):
    client = _client(tmp_path, max_level=AuthLevel.L3_RESTART)
    monkeypatch.setattr(
        "remote.api.server._restart_hashi_process",
        lambda: pytest.fail("wrong target must be rejected before launch"),
    )

    response = client.post(
        "/control/hashi/restart",
        json={"reason": "wrong target", "target_instance": "HASHI_OTHER"},
    )

    assert response.status_code == 409
    assert response.json()["error"] == "restart_target_mismatch"


def test_hashi_rescue_restart_fails_when_terminal_evidence_is_incomplete(
    tmp_path, monkeypatch
):
    client = _client(tmp_path, max_level=AuthLevel.L3_RESTART)
    before = {
        "ok": True,
        "state": "running",
        "hashi_running": True,
        "pid": 4141,
        "pid_alive": True,
        "workbench_health": {
            "ok": True,
            "ready": True,
            "instance_id": "HASHI_TEST",
            "runtime": {"core_api": "4"},
            "shared_functions": {"generation_id": "sha256:abc"},
        },
    }
    after = {
        "ok": True,
        "state": "starting_or_stuck",
        "hashi_running": False,
        "pid": 6161,
        "pid_alive": True,
        "workbench_health": None,
    }
    states = iter((before, after))
    monkeypatch.setattr("remote.api.server._hashi_control_status", lambda: next(states))
    monkeypatch.setattr("remote.api.server.RESTART_VERIFICATION_TIMEOUT_SECONDS", 0.0)
    monkeypatch.setattr("remote.api.server._process_exists", lambda pid: pid == 6161)
    monkeypatch.setattr(
        "remote.api.server._restart_hashi_process",
        lambda: {
            "pid": 5252,
            "command": ["fixed-launcher", "--force"],
            "log_path": str(tmp_path / "logs" / "restart.log"),
            "launcher_kind": "fixed-launcher",
            "platform": "windows",
        },
    )

    response = client.post(
        "/control/hashi/restart",
        json={"reason": "test", "target_instance": "HASHI_TEST"},
    )

    assert response.status_code == 503
    body = response.json()
    assert body["state"] == "failed"
    assert body["phase"] == "terminal_verification"
    assert body["evidence"]["backend_health_ok"] is False
    receipt = client.get(f"/control/hashi/restarts/{body['restart_id']}")
    assert receipt.json()["state"] == "failed"


def test_hashi_restart_receipt_rejects_path_traversal(tmp_path):
    client = _client(tmp_path, max_level=AuthLevel.L3_RESTART)

    response = client.get("/control/hashi/restarts/not-a-restart-id")

    assert response.status_code == 400


def test_request_workbench_reboot_uses_authenticated_admin_endpoint(monkeypatch):
    captured = {}

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        @staticmethod
        def read():
            return b'{"ok":true,"action":"reboot_min"}'

    def urlopen(request, *, timeout):
        captured["url"] = request.full_url
        captured["body"] = json.loads(request.data.decode("utf-8"))
        captured["headers"] = {
            key.casefold(): value for key, value in request.header_items()
        }
        captured["timeout"] = timeout
        return Response()

    monkeypatch.setattr(
        "remote.api.server.local_http_hosts", lambda: ["127.0.0.1"]
    )
    monkeypatch.setattr(
        "remote.api.server._workbench_admin_token", lambda: "admin-secret"
    )
    monkeypatch.setattr("remote.api.server.urllib_request.urlopen", urlopen)

    status, payload = _request_workbench_reboot(
        agent="zhaojun", mode="min", timeout=2.5
    )

    assert status == 200
    assert payload["ok"] is True
    assert captured["url"].endswith("/api/admin/command")
    assert captured["body"] == {
        "agent": "zhaojun",
        "command": "/reboot min",
    }
    assert captured["headers"]["x-workbench-token"] == "admin-secret"
    assert captured["timeout"] == 2.5


@pytest.mark.parametrize(
    ("state", "expected_key"),
    [
        ("completed", "api.restart.completed"),
        ("failed", "api.restart.failed_detail"),
    ],
)
def test_restart_result_notification_sends_user_facing_message_key(
    monkeypatch, state, expected_key
):
    captured = {}

    def forward(**kwargs):
        captured.update(json.loads(kwargs["body_bytes"].decode("utf-8")))
        return 200, b'{"ok":true}', {}

    monkeypatch.setattr(remote_server, "_forward_workbench_gateway_request", forward)

    result = remote_server._notify_restart_result(
        agent="agent1",
        record={
            "state": state,
            "target_instance": "HASHI3",
            "phase": "terminal_verification",
            "restart_id": "rst_internal",
            "evidence": {
                "old_pid": 123,
                "new_pid": 456,
                "generation_id": "sha256:internal",
            },
        },
    )

    assert result["state"] == "delivered"
    expected_args = {"instance": "HASHI3"}
    if state == "failed":
        expected_args.update(
            stage="terminal verification",
            reason=(
                "the previous process did not exit; the new process is not alive; "
                "no replacement process was observed"
            ),
        )
    assert captured == {
        "agent": "agent1",
        "message_key": expected_key,
        "message_args": expected_args,
    }


def test_restart_result_notification_explains_nonblocking_degraded_success(monkeypatch):
    captured = {}

    def forward(**kwargs):
        captured.update(json.loads(kwargs["body_bytes"].decode("utf-8")))
        return 200, b'{"ok":true}', {}

    monkeypatch.setattr(remote_server, "_forward_workbench_gateway_request", forward)

    result = remote_server._notify_restart_result(
        agent="agent1",
        record={
            "state": "completed",
            "target_instance": "HASHI4",
            "status": {
                "workbench_health": {
                    "degraded": True,
                    "issues": [
                        {
                            "code": "remote_already_running_degraded",
                            "summary": "Remote/HChat is unavailable; local startup will continue.",
                        }
                    ],
                }
            },
        },
    )

    assert result["state"] == "delivered"
    assert captured == {
        "agent": "agent1",
        "message_key": "api.restart.completed_degraded",
        "message_args": {
            "instance": "HASHI4",
            "warning": "Remote/HChat is unavailable; local startup will continue.",
        },
    }


def test_restart_result_notification_falls_back_for_older_workbench(monkeypatch):
    captured = []

    def forward(**kwargs):
        payload = json.loads(kwargs["body_bytes"].decode("utf-8"))
        captured.append(payload)
        if len(captured) == 1:
            return 400, b'{"ok":false,"error":"message_key is not supported"}', {}
        return 200, b'{"ok":true,"chat_id":123}', {}

    monkeypatch.setattr(remote_server, "_forward_workbench_gateway_request", forward)

    result = remote_server._notify_restart_result(
        agent="agent1",
        record={
            "state": "failed",
            "target_instance": "HASHI4",
            "phase": "terminal_verification",
            "evidence": {"old_pid_exited": True, "new_pid_alive": False},
        },
    )

    assert result == {"state": "delivered", "agent": "agent1", "chat_id": 123}
    assert captured[0]["message_key"] == "api.restart.failed_detail"
    assert "text" in captured[1]
    assert "HASHI4" in captured[1]["text"]
    assert "new process is not alive" in captured[1]["text"]


def test_hashi_rescue_reboot_prefers_hot_reboot_without_fallback(
    tmp_path, monkeypatch
):
    client = _client(tmp_path, max_level=AuthLevel.L3_RESTART)
    monkeypatch.setattr(
        "remote.api.server._request_workbench_reboot",
        lambda **_kwargs: (200, {"ok": True, "action": "reboot_min"}),
    )
    monkeypatch.setattr(
        "remote.api.server._restart_hashi_process",
        lambda: pytest.fail("healthy Workbench must not trigger hard recovery"),
    )

    response = client.post(
        "/control/hashi/reboot",
        json={"agent": "zhaojun", "mode": "min", "reason": "test"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["hot_reboot_requested"] is True
    assert body["fallback_restart_launched"] is False


def test_hashi_rescue_reboot_can_recover_when_workbench_is_unreachable(
    tmp_path, monkeypatch
):
    client = _client(tmp_path, max_level=AuthLevel.L3_RESTART)

    def unavailable(**_kwargs):
        raise ConnectionError("Workbench timed out")

    monkeypatch.setattr("remote.api.server._request_workbench_reboot", unavailable)
    monkeypatch.setattr(
        "remote.api.server._restart_hashi_process",
        lambda: {
            "pid": 6262,
            "command": [str(tmp_path / "bin" / "bridge-u.sh"), "--force"],
            "log_path": str(tmp_path / "logs" / "remote_rescue_hashi_restart.log"),
            "launcher_kind": "bridge-u.sh",
            "platform": "linux",
        },
    )

    response = client.post(
        "/control/hashi/reboot",
        json={"agent": "zhaojun", "mode": "min", "reason": "stuck core"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["hot_reboot_requested"] is False
    assert body["fallback_restart_launched"] is True
    assert body["pid"] == 6262
    audit_path = tmp_path / "logs" / "remote_rescue_audit.jsonl"
    record = json.loads(audit_path.read_text(encoding="utf-8").splitlines()[-1])
    assert record["operation"] == "reboot"
    assert record["agent"] == "zhaojun"
    assert record["mode"] == "min"
    assert record["fallback_used"] is True


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
