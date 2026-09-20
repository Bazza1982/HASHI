from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
WINDOWS_PACKAGING = ROOT / "packaging" / "windows"
LAUNCHER = WINDOWS_PACKAGING / "start-wsl-hashi-user-runtime.ps1"
INSTALLER = WINDOWS_PACKAGING / "install-wsl-hashi-user-runtime.ps1"
PORTABLE_COMMON = (
    ROOT
    / "packaging"
    / "portable_windows"
    / "templates"
    / "launcher"
    / "Common.ps1"
)
BRIDGE_U = ROOT / "bin" / "bridge-u.sh"
REMOTE_CTL = ROOT / "bin" / "hashi_remote_ctl.ps1"
FIXTURES = ROOT / "tests" / "fixtures" / "windows"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _read_powershell_log(path: Path) -> str:
    payload = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-16", "cp1252"):
        try:
            return payload.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise AssertionError(f"cannot decode PowerShell log: {path}")


def test_wsl_launcher_uses_exit_code_authority_and_separate_stream_logs():
    launcher = _read(LAUNCHER)

    assert "Start-Process" in launcher
    assert "return [int]$process.ExitCode" in launcher
    assert "-RedirectStandardOutput $stdoutLogPath" in launcher
    assert "-RedirectStandardError $stderrLogPath" in launcher
    assert "*>>" not in launcher
    assert "NativeCommandError records" in launcher
    assert "user-runtime.stdout.log" in launcher
    assert "user-runtime.stderr.log" in launcher
    assert "Archive-PreviousStreamLog" in launcher
    assert "Test-LogContainsNullByte" in launcher
    assert launcher.count("Invoke-WslNative -Arguments") == 2


def test_wsl_task_installer_is_parameterized_and_hardens_task_lifetime():
    installer = _read(INSTALLER)

    for machine_specific_value in (
        "HASHI1",
        "HASHI2",
        "Ubuntu-22.04",
        "/home/lily",
        "A9_MAX",
    ):
        assert machine_specific_value not in installer

    assert "Invoke-WslProbe" in installer
    assert "$nativeExitCode = [int]$LASTEXITCODE" in installer
    assert "[IO.File]::Replace" in installer
    assert "-LogonType Interactive" in installer
    assert "-RunLevel Highest" in installer
    assert "-AllowStartIfOnBatteries" in installer
    assert "-DontStopIfGoingOnBatteries" in installer
    assert "-StartWhenAvailable" in installer
    assert "-ExecutionTimeLimit ([TimeSpan]::Zero)" in installer
    assert "-MultipleInstances IgnoreNew" in installer
    assert "-NonInteractive" in installer
    assert "-WindowStyle', 'Hidden'" in installer


def test_native_windows_and_wsl_entry_points_keep_their_platform_contracts():
    portable = _read(PORTABLE_COMMON)
    bridge_u = _read(BRIDGE_U)

    assert "Quote-ProcessArgument" in portable
    assert "-RedirectStandardOutput $stdout" in portable
    assert "-RedirectStandardError $stderr" in portable
    assert "set -euo pipefail" in bridge_u
    assert 'python3 main.py --bridge-home "$BRIDGE_HOME" $py_args' in bridge_u


def test_native_windows_remote_supervisor_has_no_task_time_limit():
    controller = _read(REMOTE_CTL)
    registration = controller.split(
        "function Register-HashiRemoteSupervisor", 1
    )[1].split("function Get-RemotePort", 1)[0]

    assert "-ExecutionTimeLimit ([TimeSpan]::Zero)" in registration
    assert "-MultipleInstances IgnoreNew" in registration
    assert "-StartWhenAvailable" in registration
    assert "-DontStopOnIdleEnd" in registration


@pytest.mark.skipif(sys.platform != "win32", reason="requires Windows PowerShell")
@pytest.mark.parametrize(
    ("fixture_name", "expected_exit"),
    (("fake-wsl-success.cmd", 0), ("fake-wsl-main-failure.cmd", 23)),
)
def test_windows_powershell_launcher_tolerates_stderr_and_propagates_exit_code(
    tmp_path: Path,
    fixture_name: str,
    expected_exit: int,
):
    powershell = shutil.which("powershell.exe") or shutil.which("powershell")
    assert powershell is not None
    identity = subprocess.check_output(
        ["whoami"], text=True, encoding="utf-8", errors="replace"
    ).strip()
    runtime_base = tmp_path / "runtime"
    log_dir = runtime_base / "TEST1" / "logs"
    log_dir.mkdir(parents=True)
    (log_dir / "user-runtime.log").write_bytes(
        "legacy mixed stream".encode("utf-16-le")
    )

    completed = subprocess.run(
        [
            powershell,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(LAUNCHER),
            "-InstanceId",
            "TEST1",
            "-ExpectedIdentity",
            identity,
            "-Distro",
            "Test-Distro",
            "-LinuxRoot",
            "/tmp/hashi test",
            "-LinuxPython",
            "/tmp/hashi test/.venv/bin/python3",
            "-RuntimeBase",
            str(runtime_base),
            "-WslExecutable",
            str(FIXTURES / fixture_name),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=30,
    )

    assert completed.returncode == expected_exit, completed.stderr
    lifecycle = (log_dir / "user-runtime.log").read_text(encoding="utf-8")
    stdout = _read_powershell_log(log_dir / "user-runtime.stdout.log")
    stderr = _read_powershell_log(log_dir / "user-runtime.stderr.log")
    assert "\x00" not in lifecycle
    assert "legacy mixed stream" not in lifecycle
    assert len(list(log_dir.glob("user-runtime-*.log"))) == 1
    if expected_exit == 0:
        assert "launcher exited with code 0" in lifecycle
        assert stdout.count("fake stdout") == 1
        assert stderr.count("fake stderr") == 1
        assert "NativeCommandError" not in stderr
    else:
        assert f"launcher exited with code {expected_exit}" in lifecycle
        assert "fake main failure" in stderr
