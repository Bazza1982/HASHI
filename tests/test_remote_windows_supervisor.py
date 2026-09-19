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
        [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-EncodedCommand",
            encoded,
        ],
        capture_output=True, text=True, timeout=30,
    )


def _ps_string(value):
    return "'" + str(value).replace("'", "''") + "'"


def test_remote_registration_keeps_network_process_limited_and_actuator_highest(
    tmp_path,
):
    root = tmp_path / "hashi restart boundary"
    remote = root / "remote"
    remote.mkdir(parents=True)
    (root / "agents.json").write_text(
        json.dumps({"global": {"instance_id": "SUPERVISOR-CONTRACT"}}),
        encoding="utf-8",
    )
    shutil.copyfile(
        ROOT / "remote/supervisor_identity.py",
        remote / "supervisor_identity.py",
    )
    captured = tmp_path / "registered-tasks.json"
    result = _powershell(f"""
$ErrorActionPreference = 'Stop'
$global:RegisteredTasks = @()
function New-ScheduledTaskAction {{
    param($Execute, $Argument, $WorkingDirectory)
    [pscustomobject]@{{Execute=$Execute; Arguments=$Argument; WorkingDirectory=$WorkingDirectory}}
}}
function New-ScheduledTaskTrigger {{ [pscustomobject]@{{Kind='logon'}} }}
function New-ScheduledTaskSettingsSet {{ [pscustomobject]@{{}} }}
function New-ScheduledTaskPrincipal {{
    param($UserId, $LogonType, $RunLevel)
    [pscustomobject]@{{UserId=$UserId; LogonType=$LogonType; RunLevel=[string]$RunLevel}}
}}
function New-ScheduledTask {{
    param($Action, $Trigger, $Settings, $Principal)
    [pscustomobject]@{{Actions=@($Action); Trigger=$Trigger; Principal=$Principal}}
}}
function Register-ScheduledTask {{
    param($TaskName, $InputObject, [switch]$Force, $ErrorAction)
    $global:RegisteredTasks += [pscustomobject]@{{
        TaskName=$TaskName
        Action=@($InputObject.Actions)[0]
        Principal=$InputObject.Principal
    }}
}}
& {_ps_string(ROOT / 'bin/hashi_remote_ctl.ps1')} register -HashiRoot {_ps_string(root)} -Python {_ps_string(sys.executable)} -TaskUserId 'NT AUTHORITY\\LOCAL SERVICE'
$global:RegisteredTasks | ConvertTo-Json -Depth 6 | Set-Content -Encoding UTF8 -LiteralPath {_ps_string(captured)}
""")

    assert result.returncode == 0, result.stderr
    tasks = json.loads(captured.read_text(encoding="utf-8-sig"))
    by_name = {task["TaskName"]: task for task in tasks}
    restart = by_name["HashiRestart-supervisor-contract"]
    runtime = by_name["HashiRuntime-supervisor-contract"]
    remote_task = by_name["HashiRemote-supervisor-contract"]
    assert restart["Principal"]["RunLevel"] == "Highest"
    assert runtime["Principal"]["RunLevel"] == "Highest"
    assert remote_task["Principal"]["RunLevel"] == "Limited"
    assert "hashi_restart_task_runner.ps1" in restart["Action"]["Arguments"]
    assert "HashiRuntime-supervisor-contract" in restart["Action"]["Arguments"]
    assert "bridge_ctl.ps1" in runtime["Action"]["Arguments"]
    assert "-Action start" in runtime["Action"]["Arguments"]
    assert str(root) in restart["Action"]["Arguments"]


def test_fixed_restart_actuator_triggers_only_its_registered_definition(tmp_path):
    root = tmp_path / "hashi exact restart"
    remote = root / "remote"
    remote.mkdir(parents=True)
    (root / "agents.json").write_text(
        json.dumps({"global": {"instance_id": "RESTART-EXACT"}}),
        encoding="utf-8",
    )
    shutil.copyfile(
        ROOT / "remote/supervisor_identity.py",
        remote / "supervisor_identity.py",
    )
    started = tmp_path / "started-task.txt"
    result = _powershell(f"""
$ErrorActionPreference = 'Stop'
$global:RegisteredTasks = @{{}}
function New-ScheduledTaskAction {{
    param($Execute, $Argument, $WorkingDirectory)
    [pscustomobject]@{{Execute=$Execute; Arguments=$Argument; WorkingDirectory=$WorkingDirectory}}
}}
function New-ScheduledTaskSettingsSet {{ [pscustomobject]@{{}} }}
function New-ScheduledTaskPrincipal {{
    param($UserId, $LogonType, $RunLevel)
    [pscustomobject]@{{UserId=$UserId; LogonType=$LogonType; RunLevel=[string]$RunLevel}}
}}
function New-ScheduledTask {{
    param($Action, $Settings, $Principal)
    [pscustomobject]@{{Actions=@($Action); Principal=$Principal}}
}}
function Register-ScheduledTask {{
    param($TaskName, $InputObject, [switch]$Force, $ErrorAction)
    $global:RegisteredTasks[$TaskName] = $InputObject
}}
function Get-ScheduledTask {{ param($TaskName, $ErrorAction) $global:RegisteredTasks[$TaskName] }}
function Start-ScheduledTask {{
    param($TaskName, $ErrorAction)
    Set-Content -Encoding UTF8 -LiteralPath {_ps_string(started)} -Value $TaskName
}}
& {_ps_string(ROOT / 'bin/hashi_restart_ctl.ps1')} register -HashiRoot {_ps_string(root)} -Python {_ps_string(sys.executable)}
& {_ps_string(ROOT / 'bin/hashi_restart_ctl.ps1')} trigger -HashiRoot {_ps_string(root)} -Python {_ps_string(sys.executable)}
""")

    assert result.returncode == 0, result.stderr
    assert started.read_text(encoding="utf-8-sig").strip() == "HashiRestart-restart-exact"


def test_restart_runner_stops_core_then_starts_separate_runtime_task(tmp_path):
    root = tmp_path / "hashi restart runner"
    bin_dir = root / "bin"
    bin_dir.mkdir(parents=True)
    (root / "agents.json").write_text(
        json.dumps({"global": {"workbench_port": 18891}}),
        encoding="utf-8",
    )
    stopped = tmp_path / "stopped.txt"
    started = tmp_path / "started.txt"
    log = tmp_path / "restart.log"
    (bin_dir / "bridge_ctl.ps1").write_text(
        "param([string]$Action)\n"
        f"Set-Content -LiteralPath {_ps_string(stopped)} -Value $Action\n"
        "if ($Action -ne 'stop') { exit 9 }\n"
        "exit 0\n",
        encoding="utf-8",
    )

    result = _powershell(f"""
$ErrorActionPreference = 'Stop'
$global:RuntimeState = 'Ready'
function Get-ScheduledTask {{
    param($TaskName, $ErrorAction)
    [pscustomobject]@{{State=$global:RuntimeState}}
}}
function Start-ScheduledTask {{
    param($TaskName, $ErrorAction)
    $global:RuntimeState = 'Running'
    Set-Content -LiteralPath {_ps_string(started)} -Value $TaskName
}}
function Stop-ScheduledTask {{ param($TaskName, $ErrorAction) $global:RuntimeState = 'Ready' }}
function Get-ScheduledTaskInfo {{ [pscustomobject]@{{LastTaskResult=0}} }}
function Start-Sleep {{ param($Seconds, $Milliseconds) }}
function Invoke-RestMethod {{
    param($Uri, $Method, $TimeoutSec)
    [pscustomobject]@{{ready=$true; status='ready'}}
}}
& {_ps_string(ROOT / 'bin/hashi_restart_task_runner.ps1')} `
    -HashiRoot {_ps_string(root)} `
    -LogPath {_ps_string(log)} `
    -RuntimeTaskName 'HashiRuntime-restart-exact'
exit $LASTEXITCODE
""")

    assert result.returncode == 0, result.stderr
    assert stopped.read_text(encoding="utf-8-sig").strip() == "stop"
    assert started.read_text(encoding="utf-8-sig").strip() == (
        "HashiRuntime-restart-exact"
    )
    assert "Backend ready" in log.read_text(encoding="utf-8-sig")


def test_bridge_controller_does_not_swallow_process_termination_errors():
    text = (ROOT / "bin/bridge_ctl.ps1").read_text(encoding="utf-8")
    stop_block = text.split("function Stop-BridgeProcesses", 1)[1].split(
        "function Remove-StaleFiles", 1
    )[0]

    assert "Stop-Process -Id $procId -Force -ErrorAction Stop" in stop_block
    assert "$killFailures +=" in stop_block
    assert "Process termination reported errors" in stop_block
    assert "Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue" not in stop_block


def test_bridge_controller_restarts_fixed_hidden_entrypoint_with_logs():
    text = (ROOT / "bin/bridge_ctl.ps1").read_text(encoding="utf-8")
    start_block = text.split("function Start-Bridge", 1)[1].split(
        "function Show-Status", 1
    )[0]
    restart_block = text.split('"restart" {', 1)[1]

    assert "$MainScript" in start_block
    assert "-FilePath $PythonExe" in start_block
    assert "-WindowStyle Hidden" in start_block
    assert "-RedirectStandardOutput $StdoutLog" in start_block
    assert "-RedirectStandardError $StderrLog" in start_block
    assert "--bridge-home" in start_block
    assert "$LauncherBat" not in start_block
    assert "Test-ApiGatewayWasEnabled" in restart_block
    assert "-ApiGateway:$RestartApiGateway" in restart_block


@pytest.mark.parametrize("root_name,exit_code", [("hashi", 0), ("hashi space 测试", 7)])
def test_registered_task_preserves_module_arguments_stderr_and_exit(tmp_path, root_name, exit_code):
    root = tmp_path / root_name
    remote = root / "remote"
    remote.mkdir(parents=True)
    (root / "agents.json").write_text(
        json.dumps(
            {
                "global": {
                    "instance_id": "SUPERVISOR-TEST",
                    "display_name": "Supervisor Test",
                    "workbench_port": 18891,
                }
            }
        ),
        encoding="utf-8",
    )
    shutil.copyfile(ROOT / "remote/supervisor_identity.py", remote / "supervisor_identity.py")
    (remote / "__init__.py").write_text("", encoding="utf-8")
    (remote / "__main__.py").write_text(
        "import ctypes, json, os, sys\nfrom pathlib import Path\n"
        "kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)\n"
        "kernel32.GetCurrentProcess.restype = ctypes.c_void_p\n"
        "kernel32.GetPriorityClass.argtypes = [ctypes.c_void_p]\n"
        "Path('received.json').write_text(json.dumps(sys.argv[1:]), encoding='utf-8')\n"
        "print('REMOTE_STDOUT_OK', flush=True)\n"
        "print('REMOTE_STDERR_OK', file=sys.stderr, flush=True)\n"
        "print('REMOTE_AFTER_STDERR_OK', flush=True)\n"
        "print('REMOTE_UTF8_🌸_测试', flush=True)\n"
        "print(f'REMOTE_PRIORITY={kernel32.GetPriorityClass(kernel32.GetCurrentProcess())}', flush=True)\n"
        "sys.exit(int(os.environ['REMOTE_TEST_EXIT']))\n", encoding="utf-8"
    )
    action_path = tmp_path / "action.json"
    settings_path = tmp_path / "settings.json"
    # Capture the real controller's action without registering an OS task.
    register = _powershell(f"""
$ErrorActionPreference = 'Stop'
function New-ScheduledTaskAction {{
    param($Execute, $Argument, $WorkingDirectory)
    [pscustomobject]@{{Execute=$Execute; Arguments=$Argument; WorkingDirectory=$WorkingDirectory}}
}}
function New-ScheduledTaskTrigger {{ @{{}} }}
function New-ScheduledTaskSettingsSet {{
    param(
        [switch]$AllowStartIfOnBatteries,
        [switch]$DontStopIfGoingOnBatteries,
        [int]$RestartCount,
        $RestartInterval,
        [int]$Priority
    )
    [pscustomobject]@{{Priority=$Priority}} |
        ConvertTo-Json | Set-Content -Encoding UTF8 -LiteralPath {_ps_string(settings_path)}
    @{{}}
}}
function New-ScheduledTaskPrincipal {{ @{{}} }}
function New-ScheduledTask {{ param($Action, $Trigger, $Settings, $Principal) $Action }}
function Register-ScheduledTask {{
    param($TaskName, $InputObject, [switch]$Force)
    $InputObject | ConvertTo-Json | Set-Content -Encoding UTF8 -LiteralPath {_ps_string(action_path)}
}}
& {_ps_string(ROOT / 'bin/hashi_remote_ctl.ps1')} register -HashiRoot {_ps_string(root)} -Python {_ps_string(sys.executable)} -TaskUserId 'NT AUTHORITY\\LOCAL SERVICE' -Port 18999 -NoTls
""")
    assert register.returncode == 0, register.stderr
    action = json.loads(action_path.read_text(encoding="utf-8-sig"))
    result = subprocess.run(
        subprocess.list2cmdline([action["Execute"]]) + " " + action["Arguments"],
        cwd=root, env={**os.environ, "REMOTE_TEST_EXIT": str(exit_code)},
        capture_output=True, text=True, timeout=30,
        creationflags=subprocess.BELOW_NORMAL_PRIORITY_CLASS,
    )
    assert (root / "received.json").exists(), result.stderr
    assert json.loads((root / "received.json").read_text(encoding="utf-8")) == [
        "--hashi-root", str(root), "--supervised",
        "--instance-id", "SUPERVISOR-TEST",
        "--display-name", "Supervisor Test",
        "--workbench-port", "18891",
        "--no-tls", "--port", "18999",
    ]
    assert result.returncode == exit_code, result.stderr
    log = (root / "logs/hashi-remote-supervisor.log").read_bytes()
    text = log.decode("utf-16") if log.startswith(b"\xff\xfe") else log.decode("utf-8-sig")
    for marker in (
        "REMOTE_STDOUT_OK",
        "REMOTE_STDERR_OK",
        "REMOTE_AFTER_STDERR_OK",
        "REMOTE_UTF8_🌸_测试",
        "REMOTE_PRIORITY=32",
    ):
        assert marker in text
    settings = json.loads(settings_path.read_text(encoding="utf-8-sig"))
    assert settings["Priority"] == 4


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


def test_register_propagates_task_registration_failure(tmp_path):
    root = tmp_path / "hashi denied"
    remote = root / "remote"
    remote.mkdir(parents=True)
    (root / "agents.json").write_text(
        json.dumps({"global": {"instance_id": "SUPERVISOR-DENIED"}}),
        encoding="utf-8",
    )
    shutil.copyfile(
        ROOT / "remote/supervisor_identity.py", remote / "supervisor_identity.py"
    )
    result = _powershell(f"""
$ErrorActionPreference = 'Stop'
function New-ScheduledTaskAction {{ @{{}} }}
function New-ScheduledTaskTrigger {{ @{{}} }}
function New-ScheduledTaskSettingsSet {{ @{{}} }}
function New-ScheduledTaskPrincipal {{ @{{}} }}
function New-ScheduledTask {{ @{{}} }}
function Register-ScheduledTask {{
    param($TaskName, $InputObject, [switch]$Force, $ErrorAction)
    if ([string]$ErrorAction -eq 'Stop') {{ throw 'REGISTRATION_DENIED' }}
}}
& {_ps_string(ROOT / 'bin/hashi_remote_ctl.ps1')} register -HashiRoot {_ps_string(root)} -Python {_ps_string(sys.executable)} -TaskUserId 'NT AUTHORITY\\LOCAL SERVICE'
""")
    assert result.returncode != 0
    assert "Registered and enabled Remote supervisor" not in result.stdout
    assert "REGISTRATION_DENIED" in result.stderr


def test_register_grants_secrets_access_to_distinct_task_principal(tmp_path):
    root = tmp_path / "hashi cross account"
    remote = root / "remote"
    remote.mkdir(parents=True)
    (root / "agents.json").write_text(
        json.dumps({"global": {"instance_id": "SUPERVISOR-ACL"}}),
        encoding="utf-8",
    )
    shutil.copyfile(
        ROOT / "remote/supervisor_identity.py", remote / "supervisor_identity.py"
    )
    secrets_path = root / "secrets.json"
    secrets_path.write_text('{"hashi_remote_shared_token":"test-only"}\n', encoding="utf-8")
    principal_path = tmp_path / "principal.json"

    result = _powershell(f"""
$ErrorActionPreference = 'Stop'
function Get-ScheduledTask {{ $null }}
function New-ScheduledTaskAction {{ @{{}} }}
function New-ScheduledTaskTrigger {{ @{{}} }}
function New-ScheduledTaskSettingsSet {{ @{{}} }}
function New-ScheduledTaskPrincipal {{
    param($UserId, $LogonType, $RunLevel)
    [pscustomobject]@{{UserId=$UserId; LogonType=$LogonType; RunLevel=$RunLevel}}
}}
function New-ScheduledTask {{
    param($Action, $Trigger, $Settings, $Principal)
    $Principal | ConvertTo-Json | Set-Content -Encoding UTF8 -LiteralPath {_ps_string(principal_path)}
    @{{}}
}}
function Register-ScheduledTask {{ @{{}} }}
& {_ps_string(ROOT / 'bin/hashi_remote_ctl.ps1')} register -HashiRoot {_ps_string(root)} -Python {_ps_string(sys.executable)} -TaskUserId 'NT AUTHORITY\\LOCAL SERVICE'
""")

    assert result.returncode == 0, result.stderr
    principal = json.loads(principal_path.read_text(encoding="utf-8-sig"))
    assert principal["UserId"] == "NT AUTHORITY\\LOCAL SERVICE"
    acl = subprocess.run(
        ["icacls.exe", str(secrets_path)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "NT AUTHORITY\\LOCAL SERVICE:(F)" in acl
    assert "Everyone:" not in acl and "BUILTIN\\Users:" not in acl


def test_restart_keeps_existing_readable_secrets_acl_for_current_principal(tmp_path):
    root = tmp_path / "hashi protected secrets"
    remote = root / "remote"
    remote.mkdir(parents=True)
    (root / "agents.json").write_text(
        json.dumps({"global": {"instance_id": "SUPERVISOR-ACL"}}),
        encoding="utf-8",
    )
    (root / "secrets.json").write_text(
        '{"hashi_remote_shared_token":"test-only"}\n',
        encoding="utf-8",
    )
    shutil.copyfile(
        ROOT / "remote/supervisor_identity.py", remote / "supervisor_identity.py"
    )
    staged = tmp_path / "staged"
    (staged / "bin").mkdir(parents=True)
    (staged / "tools").mkdir()
    shutil.copyfile(
        ROOT / "bin/hashi_remote_ctl.ps1",
        staged / "bin/hashi_remote_ctl.ps1",
    )
    (staged / "tools/private_files.py").write_text(
        "raise SystemExit('ACL_HELPER_CALLED')\n",
        encoding="utf-8",
    )

    result = _powershell(f"""
$ErrorActionPreference = 'Stop'
function Get-CimInstance {{ @() }}
function Stop-ScheduledTask {{ param($TaskName) }}
function Start-ScheduledTask {{ param($TaskName) }}
function Invoke-RestMethod {{
    param($Method, $Uri, $TimeoutSec)
    [pscustomobject]@{{
        ok=$true; status='ready';
        instance=[pscustomobject]@{{instance_id='SUPERVISOR-ACL'}};
        discovery=[pscustomobject]@{{
            state='ready_empty'; readiness='ready'; peer_count=0;
            trusted_peer_count=0; trust_state='no_peers';
            backends=@([pscustomobject]@{{advertising=$true; browsing=$true}})
        }}
    }}
}}
& {_ps_string(staged / 'bin/hashi_remote_ctl.ps1')} restart -HashiRoot {_ps_string(root)} -Python {_ps_string(sys.executable)} -TaskUserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name)
""")

    assert result.returncode == 0, result.stderr
    assert "ACL_HELPER_CALLED" not in result.stderr


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
function Invoke-RestMethod {{
    param($Method, $Uri, $TimeoutSec)
    [pscustomobject]@{{
        ok=$true; status='ready';
        instance=[pscustomobject]@{{instance_id='SUPERVISOR-TEST'}};
        discovery=[pscustomobject]@{{
            state='ready_empty'; readiness='ready'; peer_count=0;
            trusted_peer_count=0; trust_state='no_peers';
            backends=@([pscustomobject]@{{advertising=$true; browsing=$true}})
        }}
    }}
}}
function Stop-Process {{
    param([int]$Id, [switch]$Force)
    Add-Content -LiteralPath {_ps_string(stopped)} -Value $Id
    $global:RemoteRows = @($global:RemoteRows | Where-Object {{ [int]$_.ProcessId -ne $Id }})
}}
& {_ps_string(ROOT / 'bin/hashi_remote_ctl.ps1')} restart -HashiRoot {_ps_string(root)} -Python {_ps_string(sys.executable)} -TaskUserId 'NT AUTHORITY\\LOCAL SERVICE'
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
function Invoke-RestMethod {{
    param($Method, $Uri, $TimeoutSec)
    [pscustomobject]@{{
        ok=$true; status='ready';
        instance=[pscustomobject]@{{instance_id='SUPERVISOR-DELAYED'}};
        discovery=[pscustomobject]@{{
            state='ready_empty'; readiness='ready'; peer_count=0;
            trusted_peer_count=0; trust_state='no_peers';
            backends=@([pscustomobject]@{{advertising=$true; browsing=$true}})
        }}
    }}
}}
function Stop-Process {{ param([int]$Id, [switch]$Force) }}
& {_ps_string(ROOT / 'bin/hashi_remote_ctl.ps1')} restart -HashiRoot {_ps_string(root)} -Python {_ps_string(sys.executable)} -TaskUserId 'NT AUTHORITY\\LOCAL SERVICE'
""")
    assert result.returncode == 0, result.stderr
    assert started.read_text(encoding="utf-8-sig").strip() == (
        "HashiRemote-supervisor-delayed"
    )


def test_start_rejects_reachable_remote_with_degraded_discovery(tmp_path):
    root = tmp_path / "hashi degraded"
    remote = root / "remote"
    remote.mkdir(parents=True)
    (root / "agents.json").write_text(
        json.dumps({"global": {"instance_id": "SUPERVISOR-DEGRADED"}}),
        encoding="utf-8",
    )
    shutil.copyfile(
        ROOT / "remote/supervisor_identity.py", remote / "supervisor_identity.py"
    )
    result = _powershell(f"""
$ErrorActionPreference = 'Stop'
function Start-ScheduledTask {{ param($TaskName) }}
function Start-Sleep {{ param([int]$Milliseconds) }}
function Invoke-RestMethod {{
    param($Method, $Uri, $TimeoutSec)
    [pscustomobject]@{{
        ok=$true; status='degraded';
        instance=[pscustomobject]@{{instance_id='SUPERVISOR-DEGRADED'}};
        discovery=[pscustomobject]@{{
            state='degraded'; readiness='degraded'; peer_count=0;
            trusted_peer_count=0; trust_state='no_peers';
            backends=@([pscustomobject]@{{advertising=$false; browsing=$false; last_error='browser failed'}})
        }}
    }}
}}
& {_ps_string(ROOT / 'bin/hashi_remote_ctl.ps1')} start -HashiRoot {_ps_string(root)} -Python {_ps_string(sys.executable)} -TaskUserId 'NT AUTHORITY\\LOCAL SERVICE' -NoTls
""")

    assert result.returncode != 0
    assert "discovery" in result.stderr.lower()


def test_start_allows_trusted_handshake_to_settle_after_initial_degraded_health(tmp_path):
    root = tmp_path / "hashi settling"
    remote = root / "remote"
    remote.mkdir(parents=True)
    (root / "agents.json").write_text(
        json.dumps({"global": {"instance_id": "SUPERVISOR-SETTLING"}}),
        encoding="utf-8",
    )
    shutil.copyfile(
        ROOT / "remote/supervisor_identity.py", remote / "supervisor_identity.py"
    )
    result = _powershell(f"""
$ErrorActionPreference = 'Stop'
$global:HealthCalls = 0
function Start-ScheduledTask {{ param($TaskName) }}
function Start-Sleep {{ param([int]$Milliseconds) }}
function Invoke-RestMethod {{
    param($Method, $Uri, $TimeoutSec)
    $global:HealthCalls++
    $Ready = $global:HealthCalls -ge 30
    [pscustomobject]@{{
        ok=$true; status=$(if ($Ready) {{ 'ready' }} else {{ 'degraded' }});
        instance=[pscustomobject]@{{instance_id='SUPERVISOR-SETTLING'}};
        discovery=[pscustomobject]@{{
            state=$(if ($Ready) {{ 'ready' }} else {{ 'degraded' }});
            readiness=$(if ($Ready) {{ 'ready' }} else {{ 'starting' }});
            peer_count=$(if ($Ready) {{ 1 }} else {{ 0 }});
            trusted_peer_count=$(if ($Ready) {{ 1 }} else {{ 0 }});
            trust_state=$(if ($Ready) {{ 'accepted' }} else {{ 'pending' }});
            backends=@([pscustomobject]@{{advertising=$true; browsing=$true}})
        }}
    }}
}}
& {_ps_string(ROOT / 'bin/hashi_remote_ctl.ps1')} start -HashiRoot {_ps_string(root)} -Python {_ps_string(sys.executable)} -TaskUserId 'NT AUTHORITY\\LOCAL SERVICE' -NoTls
""")

    assert result.returncode == 0, result.stderr
    assert "trusted_peer" in result.stdout


def test_doctor_uses_configured_tls_and_accepts_ready_empty(tmp_path):
    root = tmp_path / "hashi tls"
    remote = root / "remote"
    remote.mkdir(parents=True)
    (root / "agents.json").write_text(
        json.dumps(
            {
                "global": {
                    "instance_id": "SUPERVISOR-TLS",
                    "remote_port": 19002,
                }
            }
        ),
        encoding="utf-8",
    )
    (remote / "config.yaml").write_text(
        "server:\n  port: 19001\n  use_tls: true\n",
        encoding="utf-8",
    )
    shutil.copyfile(
        ROOT / "remote/supervisor_identity.py", remote / "supervisor_identity.py"
    )
    observed_uri = tmp_path / "health-uri.txt"
    result = _powershell(f"""
$ErrorActionPreference = 'Stop'
function Get-NetFirewallRule {{ @() }}
function Get-NetTCPConnection {{ @() }}
function Invoke-RestMethod {{
    param($Method, $Uri, $TimeoutSec)
    Set-Content -LiteralPath {_ps_string(observed_uri)} -Value $Uri
    [pscustomobject]@{{
        ok=$true; status='ready';
        instance=[pscustomobject]@{{instance_id='SUPERVISOR-TLS'}};
        discovery=[pscustomobject]@{{
            state='ready_empty'; readiness='ready'; peer_count=0;
            trusted_peer_count=0; trust_state='no_peers';
            backends=@([pscustomobject]@{{advertising=$true; browsing=$true}})
        }}
    }}
}}
& {_ps_string(ROOT / 'bin/hashi_remote_ctl.ps1')} doctor -HashiRoot {_ps_string(root)} -Python {_ps_string(sys.executable)}
""")

    assert result.returncode == 0, result.stderr
    assert observed_uri.read_text(encoding="utf-8-sig").strip() == "https://127.0.0.1:19002/health"
    assert "RemoteHealthMode" in result.stdout
    assert "ready_empty" in result.stdout
