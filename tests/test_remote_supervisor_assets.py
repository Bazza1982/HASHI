from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.platform


def test_linux_remote_supervisor_script_is_valid_bash():
    script = ROOT / "bin" / "hashi-remote-ctl.sh"

    result = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True, check=False)

    assert result.returncode == 0, result.stderr


def _install_linux_unit(tmp_path: Path, *, root_name: str, instance_id: str) -> tuple[Path, str]:
    root = tmp_path / root_name
    root.mkdir()
    (root / "agents.json").write_text(
        json.dumps({"global": {"instance_id": instance_id}}),
        encoding="utf-8",
    )
    (root / "remote").mkdir()
    (root / "remote" / "supervisor_identity.py").write_text(
        (ROOT / "remote" / "supervisor_identity.py").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir(exist_ok=True)
    fake_systemctl = fake_bin / "systemctl"
    fake_systemctl.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    fake_systemctl.chmod(0o755)
    config_home = tmp_path / "config"
    env = {
        **os.environ,
        "HASHI_ROOT": str(root),
        "HOME": str(tmp_path / "home"),
        "XDG_CONFIG_HOME": str(config_home),
        "PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}",
    }
    result = subprocess.run(
        ["bash", str(ROOT / "bin" / "hashi-remote-ctl.sh"), "install"],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    service_name = subprocess.run(
        ["bash", str(ROOT / "bin" / "hashi-remote-ctl.sh"), "service-name"],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert service_name.returncode == 0, service_name.stderr
    return config_home / "systemd" / "user" / service_name.stdout.strip(), result.stdout


def test_linux_remote_supervisor_units_are_isolated_per_instance(tmp_path):
    hashi1_unit, hashi1_output = _install_linux_unit(
        tmp_path,
        root_name="hashi one",
        instance_id="HASHI1",
    )
    hashi2_unit, hashi2_output = _install_linux_unit(
        tmp_path,
        root_name="hashi two",
        instance_id="HASHI2",
    )

    assert hashi1_unit.name == "hashi-remote-hashi1.service"
    assert hashi2_unit.name == "hashi-remote-hashi2.service"
    assert hashi1_unit != hashi2_unit
    assert hashi1_unit.exists()
    assert hashi2_unit.exists()
    assert "hashi one" in hashi1_unit.read_text(encoding="utf-8")
    assert "hashi two" in hashi2_unit.read_text(encoding="utf-8")
    assert "Instance HASHI1 (agents_json)" in hashi1_output
    assert "Instance HASHI2 (agents_json)" in hashi2_output
