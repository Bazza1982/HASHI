from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
pytestmark = [
    pytest.mark.platform,
    pytest.mark.skipif(sys.platform != "win32", reason="Windows task launch contract"),
]


def _powershell(script: str):
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    return subprocess.run(
        ["powershell.exe", "-NoProfile", "-EncodedCommand", encoded],
        capture_output=True, text=True, timeout=30,
    )


def _ps_string(value):
    return "'" + str(value).replace("'", "''") + "'"


@pytest.mark.parametrize("root_name,exit_code", [("hashi", 0), ("hashi space 测试", 7)])
def test_registered_task_preserves_module_arguments_stderr_and_exit(tmp_path, root_name, exit_code):
    root = tmp_path / root_name
    remote = root / "remote"
    remote.mkdir(parents=True)
    (root / "agents.json").write_text(
        json.dumps({"global": {"instance_id": "SUPERVISOR-TEST"}}), encoding="utf-8"
    )
    shutil.copyfile(ROOT / "remote/supervisor_identity.py", remote / "supervisor_identity.py")
    (remote / "__init__.py").write_text("", encoding="utf-8")
    (remote / "__main__.py").write_text(
        "import json, os, sys\nfrom pathlib import Path\n"
        "Path('received.json').write_text(json.dumps(sys.argv[1:]), encoding='utf-8')\n"
        "print('REMOTE_STDOUT_OK', flush=True)\n"
        "print('REMOTE_STDERR_OK', file=sys.stderr, flush=True)\n"
        "print('REMOTE_AFTER_STDERR_OK', flush=True)\n"
        "sys.exit(int(os.environ['REMOTE_TEST_EXIT']))\n", encoding="utf-8"
    )
    action_path = tmp_path / "action.json"
    # Capture the real controller's action without registering an OS task.
    register = _powershell(f"""
$ErrorActionPreference = 'Stop'
function New-ScheduledTaskAction {{
    param($Execute, $Argument, $WorkingDirectory)
    [pscustomobject]@{{Execute=$Execute; Arguments=$Argument; WorkingDirectory=$WorkingDirectory}}
}}
function New-ScheduledTaskTrigger {{ @{{}} }}
function New-ScheduledTaskSettingsSet {{ @{{}} }}
function New-ScheduledTaskPrincipal {{ @{{}} }}
function New-ScheduledTask {{ param($Action, $Trigger, $Settings, $Principal) $Action }}
function Register-ScheduledTask {{
    param($TaskName, $InputObject, [switch]$Force)
    $InputObject | ConvertTo-Json | Set-Content -Encoding UTF8 -LiteralPath {_ps_string(action_path)}
}}
& {_ps_string(ROOT / 'bin/hashi_remote_ctl.ps1')} register -HashiRoot {_ps_string(root)} -Python {_ps_string(sys.executable)} -Port 18999 -NoTls
""")
    assert register.returncode == 0, register.stderr
    action = json.loads(action_path.read_text(encoding="utf-8-sig"))
    result = subprocess.run(
        subprocess.list2cmdline([action["Execute"]]) + " " + action["Arguments"],
        cwd=root, env={**os.environ, "REMOTE_TEST_EXIT": str(exit_code)},
        capture_output=True, text=True, timeout=30,
    )
    assert (root / "received.json").exists(), result.stderr
    assert json.loads((root / "received.json").read_text(encoding="utf-8")) == [
        "--hashi-root", str(root), "--supervised", "--no-tls", "--port", "18999"
    ]
    assert result.returncode == exit_code, result.stderr
    log = (root / "logs/hashi-remote-supervisor.log").read_bytes()
    text = log.decode("utf-16") if log.startswith(b"\xff\xfe") else log.decode("utf-8-sig")
    for marker in ("REMOTE_STDOUT_OK", "REMOTE_STDERR_OK", "REMOTE_AFTER_STDERR_OK"):
        assert marker in text


def test_legacy_task_runner_keeps_python_module_switch(tmp_path):
    log = tmp_path / "legacy.log"
    result = _powershell(f"""
& {_ps_string(ROOT / 'bin/hashi_remote_task_runner.ps1')} -Python {_ps_string(sys.executable)} -HashiRoot {_ps_string(tmp_path)} -LogPath {_ps_string(log)} -PythonArgs '-m json.tool --help'
exit $LASTEXITCODE
""")
    assert result.returncode == 0, result.stderr
    data = log.read_bytes()
    text = data.decode("utf-16") if data.startswith(b"\xff\xfe") else data.decode("utf-8-sig")
    assert "usage:" in text


def test_restart_retires_only_exact_instance_remote_processes(tmp_path):
    root = tmp_path / "hashi space 测试"
    other = tmp_path / "other instance"
    remote = root / "remote"
    remote.mkdir(parents=True)
    (root / "agents.json").write_text(
        json.dumps({"global": {"instance_id": "SUPERVISOR-TEST"}}), encoding="utf-8"
    )
    shutil.copyfile(ROOT / "remote/supervisor_identity.py", remote / "supervisor_identity.py")
    stopped = tmp_path / "stopped.jsonl"
    started = tmp_path / "started.txt"
    result = _powershell(f"""
$ErrorActionPreference = 'Stop'
$global:RemoteRows = @(
    [pscustomobject]@{{ProcessId=101; ParentProcessId=100; Name='python.exe'; CommandLine='python -m remote --hashi-root "{root}" --supervised'}},
    [pscustomobject]@{{ProcessId=100; ParentProcessId=99; Name='powershell.exe'; CommandLine='powershell -File "{ROOT / "bin/hashi_remote_task_runner.ps1"}" -HashiRoot "{root}"'}},
    [pscustomobject]@{{ProcessId=202; ParentProcessId=201; Name='python.exe'; CommandLine='python -m remote --hashi-root "{other}" --supervised'}}
)
function Get-CimInstance {{ param($ClassName) @($global:RemoteRows) }}
function Stop-ScheduledTask {{ param($TaskName) }}
function Start-ScheduledTask {{ param($TaskName) Set-Content -LiteralPath {_ps_string(started)} -Value $TaskName }}
function Stop-Process {{
    param([int]$Id, [switch]$Force)
    Add-Content -LiteralPath {_ps_string(stopped)} -Value $Id
    $global:RemoteRows = @($global:RemoteRows | Where-Object {{ [int]$_.ProcessId -ne $Id }})
}}
& {_ps_string(ROOT / 'bin/hashi_remote_ctl.ps1')} restart -HashiRoot {_ps_string(root)} -Python {_ps_string(sys.executable)}
""")
    assert result.returncode == 0, result.stderr
    assert stopped.read_text(encoding="utf-8-sig").splitlines() == ["101", "100"]
    assert started.read_text(encoding="utf-8-sig").strip() == "HashiRemote-supervisor-test"


def test_restart_waits_for_force_terminated_process_to_leave_cim(tmp_path):
    root = tmp_path / "hashi delayed exit"
    remote = root / "remote"
    remote.mkdir(parents=True)
    (root / "agents.json").write_text(
        json.dumps({"global": {"instance_id": "SUPERVISOR-DELAYED"}}),
        encoding="utf-8",
    )
    shutil.copyfile(
        ROOT / "remote/supervisor_identity.py", remote / "supervisor_identity.py"
    )
    started = tmp_path / "delayed-started.txt"
    result = _powershell(f"""
$ErrorActionPreference = 'Stop'
$global:SleepCalls = 0
$global:RemoteRows = @(
    [pscustomobject]@{{ProcessId=303; ParentProcessId=302; Name='python.exe'; CommandLine='python -m remote --hashi-root "{root}" --supervised'}}
)
function Get-CimInstance {{
    param($ClassName)
    if ($global:SleepCalls -ge 18) {{ @() }} else {{ @($global:RemoteRows) }}
}}
function Start-Sleep {{
    param([int]$Milliseconds)
    $global:SleepCalls++
}}
function Stop-ScheduledTask {{ param($TaskName) }}
function Start-ScheduledTask {{
    param($TaskName)
    Set-Content -LiteralPath {_ps_string(started)} -Value $TaskName
}}
function Stop-Process {{ param([int]$Id, [switch]$Force) }}
& {_ps_string(ROOT / 'bin/hashi_remote_ctl.ps1')} restart -HashiRoot {_ps_string(root)} -Python {_ps_string(sys.executable)}
""")
    assert result.returncode == 0, result.stderr
    assert started.read_text(encoding="utf-8-sig").strip() == (
        "HashiRemote-supervisor-delayed"
    )
