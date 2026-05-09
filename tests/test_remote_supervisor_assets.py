from __future__ import annotations

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_linux_remote_supervisor_script_is_valid_bash():
    script = ROOT / "bin" / "hashi-remote-ctl.sh"

    result = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True, check=False)

    assert result.returncode == 0, result.stderr


def test_linux_remote_supervisor_script_uses_supervised_remote_command():
    script = (ROOT / "bin" / "hashi-remote-ctl.sh").read_text(encoding="utf-8")

    assert "--supervised" in script
    assert "HASHI_REMOTE_SUPERVISED=1" in script
    assert "systemctl --user" in script
    assert "HASHI_REMOTE_MAX_TERMINAL_LEVEL" in script


def test_windows_remote_supervisor_script_uses_task_scheduler_and_supervised_flag():
    script = (ROOT / "bin" / "hashi_remote_ctl.ps1").read_text(encoding="utf-8")

    assert "New-ScheduledTaskAction" in script
    assert "Register-ScheduledTask" in script
    assert "--supervised" in script
    assert "HASHI_REMOTE_MAX_TERMINAL_LEVEL" in script


def test_remote_supervisor_templates_exist_with_expected_placeholders():
    systemd = (ROOT / "packaging" / "systemd" / "hashi-remote.service").read_text(encoding="utf-8")
    windows = (ROOT / "packaging" / "windows" / "hashi-remote-task.xml").read_text(encoding="utf-8")

    assert "HASHI_REMOTE_SUPERVISED=1" in systemd
    assert "%PYTHON%" in systemd
    assert "%HASHI_ROOT%" in systemd
    assert "--supervised" in windows
    assert "%PYTHON%" in windows
    assert "%HASHI_ROOT%" in windows
