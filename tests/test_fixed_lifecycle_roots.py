from __future__ import annotations

import json
from pathlib import Path

import pytest

from remote.api import server
from remote.security.pairing import PairingManager
from remote.terminal.executor import TerminalExecutor


@pytest.mark.parametrize("system", ["Windows", "Linux"])
def test_fixed_lifecycle_binds_code_and_private_home_separately(tmp_path, monkeypatch, system):
    code = tmp_path / "program with spaces"
    home = tmp_path / "private instance"
    (code / "bin").mkdir(parents=True)
    home.mkdir()
    (home / "agents.json").write_text(json.dumps({
        "global": {"instance_id": "SPLIT_TEST", "workbench_port": 19980}, "agents": [],
    }), encoding="utf-8")
    for name in ["hashi_restart_ctl.ps1", "bridge_ctl.ps1", "bridge-u.sh"]:
        (code / "bin" / name).write_text("# fixed launcher\n", encoding="utf-8")
    server.create_app(
        {"instance_id": "SPLIT_TEST"},
        PairingManager(storage_dir=tmp_path / "pairing", lan_mode=False),
        TerminalExecutor(),
        hashi_root=str(home),
        control_code_root=str(code),
    )
    monkeypatch.setattr(server.platform, "system", lambda: system)
    selected_python = code / "functions" / "Scripts" / "python.exe"
    selected_python.parent.mkdir(parents=True)
    selected_python.write_bytes(b"launcher")
    monkeypatch.setattr(
        "remote.restart_runtime.selected_restart_python",
        lambda root: str(selected_python),
    )
    runtime_data_root = tmp_path / "runtime catalogue"
    monkeypatch.setattr(
        "remote.restart_runtime.restart_runtime_data_root",
        lambda: str(runtime_data_root),
    )
    command = server._hashi_restart_command()
    assert any(str(code / "bin" / name) in command
               for name in ["hashi_restart_ctl.ps1", "bridge-u.sh"])
    if system == "Windows":
        assert command[command.index("-Python") + 1] == str(selected_python)
        assert command[command.index("-HashiRoot") + 1] == str(home)
        assert command[command.index("-CodeRoot") + 1] == str(code)
        assert command[command.index("-RuntimeDataRoot") + 1] == str(
            runtime_data_root
        )
        start = server._hashi_start_command()
        assert start[start.index("-Python") + 1] == str(selected_python)
        assert start[start.index("-HashiRoot") + 1] == str(home)
        assert start[start.index("-CodeRoot") + 1] == str(code)
        assert start[start.index("-RuntimeDataRoot") + 1] == str(
            runtime_data_root
        )
        assert "start" in start
    else:
        assert command[command.index("--bridge-home") + 1] == str(home)
        start = server._hashi_start_command()
        assert start[start.index("--bridge-home") + 1] == str(home)
    launches = []

    class Process:
        pid = 12345

    def spawn(argv, **kwargs):
        launches.append((argv, kwargs))
        return Process()

    monkeypatch.setattr(server.subprocess, "Popen", spawn)
    result = server._restart_hashi_process()
    assert result["pid"] == 12345
    assert Path(result["log_path"]).is_relative_to(home)
    _, options = launches[0]
    assert options["cwd"] == str(code)
    assert options["env"]["BRIDGE_HOME"] == str(home)
    assert options["env"]["BRIDGE_CODE_ROOT"] == str(code)
    assert not (code / "logs").exists()

    qualified = []
    monkeypatch.setattr(
        "remote.restart_runtime.qualified_restart_runtime",
        lambda root: qualified.append(root) or {"role": "functions"},
    )
    assert server._qualified_restart_runtime() == {"role": "functions"}
    assert qualified == [code]
# HASHI-Fingerprint: hfp_dfa271ec6a0148ba9c9de42d949399ba
