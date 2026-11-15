from __future__ import annotations

import json
import time

import pytest

from remote.api import server


def _configure_transaction(tmp_path, monkeypatch, receipt):
    root = tmp_path / "instance"
    code = tmp_path / "code"
    root.mkdir()
    code.mkdir()
    operation_id = "a" * 32
    captured = {}

    monkeypatch.setattr(server.platform, "system", lambda: "Windows")
    monkeypatch.setattr(server, "_control_hashi_root", str(root))
    monkeypatch.setattr(server, "_control_code_root", str(code))
    monkeypatch.setattr(
        server,
        "_restart_launch_request",
        {"operation_id": operation_id, "timeout_seconds": 5},
    )
    monkeypatch.setattr(
        server,
        "_restart_controller_environment",
        lambda: {"BRIDGE_HOME": str(root), "BRIDGE_CODE_ROOT": str(code)},
    )

    def command(**kwargs):
        captured.update(kwargs)
        return ["powershell.exe", "-File", "fixture.ps1"]

    monkeypatch.setattr(server, "_hashi_restart_command", command)

    class Process:
        pid = 4321

        def wait(self, timeout):
            value = dict(receipt)
            value.setdefault("operation_id", operation_id)
            captured["receipt_path"].write_text(
                json.dumps(value), encoding="utf-8"
            )
            return int(value.pop("controller_exit_code", 0))

    monkeypatch.setattr(server.subprocess, "Popen", lambda *a, **k: Process())
    return operation_id, captured


def test_cold_restart_returns_admitted_core_pid_not_controller_pid(
    tmp_path, monkeypatch
):
    operation_id, captured = _configure_transaction(
        tmp_path,
        monkeypatch,
        {
            "state": "launch_accepted",
            "instance_id": "HASHI4",
            "evidence": {"core_pid": 8765},
        },
    )

    result = server._restart_hashi_process()

    assert result["pid"] == 8765
    assert result["controller_pid"] == 4321
    assert result["acceptance_receipt"]["operation_id"] == operation_id
    assert captured["operation_id"] == operation_id


def test_known_launcher_rejection_returns_immediately_without_readiness_wait(
    tmp_path, monkeypatch
):
    _configure_transaction(
        tmp_path,
        monkeypatch,
        {
            "state": "failed",
            "failure_stage": "invalid_task_definition",
            "error": "fixed task RuntimeDataRoot mismatch",
            "controller_exit_code": 78,
        },
    )

    started = time.monotonic()
    with pytest.raises(server.RestartLaunchError) as raised:
        server._restart_hashi_process()

    assert time.monotonic() - started < 1.0
    assert raised.value.stage == "invalid_task_definition"
    assert "RuntimeDataRoot mismatch" in str(raised.value)
