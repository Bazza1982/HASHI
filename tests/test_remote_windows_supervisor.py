from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
pytestmark = [
    pytest.mark.platform,
    pytest.mark.skipif(sys.platform != "win32", reason="Windows task launch contract"),
]


def _powershell(script: str):
    # A newly consulted task cmdlet must not autoload a module over scenario
    # doubles and accidentally mutate the real scheduler.
    isolation = r"""
Import-Module ScheduledTasks -ErrorAction Stop
function Get-ScheduledTask { param($TaskName, $ErrorAction) $null }
function Register-ScheduledTask { throw 'Unmocked task registration' }
function Set-ScheduledTask { throw 'Unmocked task update' }
function Start-ScheduledTask { throw 'Unmocked task start' }
function Stop-ScheduledTask { }
function Enable-ScheduledTask { throw 'Unmocked task enable' }
function Disable-ScheduledTask { throw 'Unmocked task disable' }
$global:TaskSecurity = @{}
$global:TaskSecurityWrites = @()
function New-Object {
    [CmdletBinding(DefaultParameterSetName='Net')]
    param(
        [Parameter(Position=0, ParameterSetName='Net')][string]$TypeName,
        [Parameter(Position=1, ParameterSetName='Net')][object[]]$ArgumentList,
        [Parameter(ParameterSetName='Net')][System.Collections.IDictionary]$Property,
        [Parameter(Mandatory=$true, ParameterSetName='Com')][string]$ComObject
    )
    if ($PSCmdlet.ParameterSetName -eq 'Net') {
        return Microsoft.PowerShell.Utility\New-Object @PSBoundParameters
    }
    if ($ComObject -ne 'Schedule.Service') { throw 'Unmocked COM activation' }
    $Folder = [pscustomobject]@{}
    $Folder | Add-Member ScriptMethod GetTask {
        param($Name)
        if (-not $global:TaskSecurity.ContainsKey($Name)) {
            $global:TaskSecurity[$Name] = 'O:BAG:SYD:(A;;FA;;;SY)(A;;FA;;;BA)'
        }
        $Task = [pscustomobject]@{Name=$Name}
        $Task | Add-Member ScriptMethod GetSecurityDescriptor {
            param($Sections)
            $global:TaskSecurity[$this.Name]
        }
        $Task | Add-Member ScriptMethod SetSecurityDescriptor {
            param($Sddl, $Flags)
            $Before = [Security.AccessControl.RawSecurityDescriptor]::new($global:TaskSecurity[$this.Name])
            $Before.DiscretionaryAcl = [Security.AccessControl.RawSecurityDescriptor]::new($Sddl).DiscretionaryAcl
            $global:TaskSecurity[$this.Name] = $Before.GetSddlForm([Security.AccessControl.AccessControlSections]::All)
            $global:TaskSecurityWrites += [pscustomobject]@{Name=$this.Name;Flags=$Flags}
        }
        $Task
    }
    $Scheduler = [pscustomobject]@{Folder=$Folder}
    $Scheduler | Add-Member ScriptMethod Connect {}
    $Scheduler | Add-Member ScriptMethod GetFolder {
        param($Path)
        if ($Path -ne '\') { throw 'Unexpected task folder' }
        $this.Folder
    }
    $Scheduler
}
function Unregister-ScheduledTask { throw 'Unmocked task deletion' }
"""
    encoded = base64.b64encode((isolation + script).encode("utf-16-le")).decode("ascii")
    env = os.environ.copy()
    # Platform contract fixtures own their exact instance/task inputs.  Do not
    # inherit the HASHI instance that happens to be running the test process.
    for name in (
        "HASHI_INSTANCE_ID",
        "HASHI_REMOTE_TASK_USER",
        "HASHI_REMOTE_MAX_TERMINAL_LEVEL",
        "HASHI_REMOTE_DISCOVERY",
        "HASHI_REMOTE_PORT",
        "HASHI_REMOTE_NO_TLS",
    ):
        env.pop(name, None)
    return subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-EncodedCommand",
            encoded,
        ],
        capture_output=True, text=True, timeout=30, env=env,
    )


def _ps_string(value):
    return "'" + str(value).replace("'", "''") + "'"


def _stub_fixed_lifecycle_setup(root):
    # These scenarios exercise Remote process/ACL/discovery, not task deployment.
    # The real actuator, split roots and task ACL upgrade have their own native tests.
    (root / "bin").mkdir(exist_ok=True)
    (root / "bin/hashi_restart_ctl.ps1").write_text(
        "param($Action, $HashiRoot, $CodeRoot, $Python, $TaskUserId)\n"
        "if ($Action -ne 'ensure') { throw 'Unexpected lifecycle operation' }\n",
        encoding="utf-8",
    )


@pytest.mark.parametrize("separate_home", [False, True])
@pytest.mark.parametrize("existing_home", ["missing", "same", "other"])
def test_remote_registration_keeps_network_process_limited_and_actuator_highest(
    tmp_path, separate_home, existing_home,
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
    if separate_home:
        (remote / "supervisor_identity.py").unlink()
    else:
        (root / "bin").mkdir()
        for filename in ("hashi_restart_ctl.ps1", "hashi_restart_task_runner.ps1", "bridge_ctl.ps1"):
            shutil.copyfile(ROOT / "bin" / filename, root / "bin" / filename)
    code_arguments = f" -CodeRoot {_ps_string(ROOT)}" if separate_home else ""
    captured = tmp_path / "registered-tasks.json"
    result = _powershell(f"""
$ErrorActionPreference = 'Stop'
$global:RegisteredTasks = @()
function Get-ScheduledTask {{
    param($TaskName, $ErrorAction)
    if ($TaskName -eq 'HashiRemote-supervisor-contract' -and '{existing_home}' -ne 'missing') {{
        $Directory = if ('{existing_home}' -eq 'same') {{ {_ps_string(root)} }} else {{ {_ps_string(tmp_path / 'other')} }}
        [pscustomobject]@{{Actions=@([pscustomobject]@{{WorkingDirectory=$Directory}})}}
    }}
}}
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
& {_ps_string(ROOT / 'bin/hashi_remote_ctl.ps1')} register -HashiRoot {_ps_string(root)} -Python {_ps_string(sys.executable)} -TaskUserId 'NT AUTHORITY\\LOCAL SERVICE'{code_arguments}
$global:RegisteredTasks | ConvertTo-Json -Depth 6 | Set-Content -Encoding UTF8 -LiteralPath {_ps_string(captured)}
""")

    if existing_home == "other":
        assert result.returncode != 0
        assert not captured.exists()
        assert "does not match this instance home" in result.stderr
        return
    assert result.returncode == 0, result.stderr
    tasks = json.loads(captured.read_text(encoding="utf-8-sig"))
    if isinstance(tasks, dict):
        tasks = [tasks]
    by_name = {task["TaskName"]: task for task in tasks}
    remote_task = by_name["HashiRemote-supervisor-contract"]
    assert remote_task["Action"]["WorkingDirectory"] == str(root)
    assert remote_task["Principal"]["RunLevel"] == "Limited"
    assert set(by_name) == {
        "HashiRemote-supervisor-contract", "HashiRestart-supervisor-contract",
        "HashiRuntime-supervisor-contract",
    }
    if separate_home:
        assert "-CodeRoot" in remote_task["Action"]["Arguments"]
        assert str(ROOT) in remote_task["Action"]["Arguments"]
    restart = by_name["HashiRestart-supervisor-contract"]
    runtime = by_name["HashiRuntime-supervisor-contract"]
    assert restart["Principal"]["RunLevel"] == "Highest"
    assert runtime["Principal"]["RunLevel"] == "Highest"
    assert remote_task["Principal"]["RunLevel"] == "Limited"
    assert "hashi_restart_task_runner.ps1" in restart["Action"]["Arguments"]
    assert "HashiRuntime-supervisor-contract" in restart["Action"]["Arguments"]
    assert "bridge_ctl.ps1" in runtime["Action"]["Arguments"]
    assert "-Action start" in runtime["Action"]["Arguments"]
    assert str(root) in restart["Action"]["Arguments"]


@pytest.mark.parametrize("configure", ["register", "ensure"])
def test_fixed_restart_actuator_triggers_only_its_registered_definition(
    tmp_path, configure,
):
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
    security = tmp_path / "task-security.json"
    result = _powershell(f"""
$ErrorActionPreference = 'Stop'
$global:RegisteredTasks = @{{}}
$global:DurationWrites = 0
function New-ScheduledTaskAction {{
    param($Execute, $Argument, $WorkingDirectory)
    [pscustomobject]@{{Execute=$Execute; Arguments=$Argument; WorkingDirectory=$WorkingDirectory}}
}}
function New-ScheduledTaskSettingsSet {{
    param($ExecutionTimeLimit)
    [pscustomobject]@{{ExecutionTimeLimit=[Xml.XmlConvert]::ToString([TimeSpan]$ExecutionTimeLimit)}}
}}
function New-ScheduledTaskPrincipal {{
    param($UserId, $LogonType, $RunLevel)
    [pscustomobject]@{{UserId=$UserId; LogonType=$LogonType; RunLevel=[string]$RunLevel}}
}}
function New-ScheduledTask {{
    param($Action, $Settings, $Principal)
    [pscustomobject]@{{Actions=@($Action); Principal=$Principal; Settings=$Settings}}
}}
function Register-ScheduledTask {{
    param($TaskName, $InputObject, [switch]$Force, $ErrorAction)
    $global:RegisteredTasks[$TaskName] = $InputObject
}}
function Set-ScheduledTask {{
    param($TaskName, $Settings, $ErrorAction)
    if ($TaskName -ne 'HashiRestart-restart-exact') {{ throw 'Unexpected task settings update' }}
    $global:RegisteredTasks[$TaskName].Settings = $Settings
    $global:DurationWrites++
}}
function Get-ScheduledTask {{ param($TaskName, $ErrorAction) $global:RegisteredTasks[$TaskName] }}
function Start-ScheduledTask {{
    param($TaskName, $ErrorAction)
    Set-Content -Encoding UTF8 -LiteralPath {_ps_string(started)} -Value $TaskName
    if ($TaskName -eq 'HashiRestart-restart-exact') {{
        $RequestPath = Join-Path {_ps_string(root)} 'state/instance/cold-restart/request.json'
        $Request = Get-Content -LiteralPath $RequestPath -Raw | ConvertFrom-Json
        @{{
            schema=1; operation_id=[string]$Request.operation_id;
            instance_id='RESTART-EXACT'; state='launch_accepted';
            evidence=@{{core_pid=1234}}
        }} | ConvertTo-Json -Depth 5 -Compress |
            Set-Content -Encoding UTF8 -LiteralPath ([string]$Request.receipt_path)
    }}
}}
& {_ps_string(ROOT / 'bin/hashi_restart_ctl.ps1')} register -HashiRoot {_ps_string(root)} -Python {_ps_string(sys.executable)}
if ('{configure}' -eq 'ensure') {{
    # Existing valid tasks created elevated may lack the Limited user's ACE.
    $global:TaskSecurity = @{{}}
    $global:TaskSecurityWrites = @()
    $global:RegisteredTasks['HashiRestart-restart-exact'].Settings.ExecutionTimeLimit = 'PT10M'
    & {_ps_string(ROOT / 'bin/hashi_restart_ctl.ps1')} ensure -HashiRoot {_ps_string(root)} -Python {_ps_string(sys.executable)}
}}
$FirstWrites = $global:TaskSecurityWrites.Count
& {_ps_string(ROOT / 'bin/hashi_restart_ctl.ps1')} ensure -HashiRoot {_ps_string(root)} -Python {_ps_string(sys.executable)}
[pscustomobject]@{{
    Sid=[Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    Tasks=@($global:RegisteredTasks.Keys | ForEach-Object {{
        $Name = $_
        $Descriptor = [Security.AccessControl.RawSecurityDescriptor]::new(
            $(if ($global:TaskSecurity.ContainsKey($Name)) {{ $global:TaskSecurity[$Name] }} else {{ 'O:BAG:SYD:(A;;FA;;;SY)(A;;FA;;;BA)' }})
        )
        [pscustomobject]@{{Name=$Name;Owner=$Descriptor.Owner.Value;Group=$Descriptor.Group.Value;Aces=@($Descriptor.DiscretionaryAcl | ForEach-Object {{ [pscustomobject]@{{Sid=$_.SecurityIdentifier.Value;Mask=$_.AccessMask}} }})}}
    }})
    FirstWrites=$FirstWrites
    Writes=@($global:TaskSecurityWrites)
    RestartLimit=$global:RegisteredTasks['HashiRestart-restart-exact'].Settings.ExecutionTimeLimit
    DurationWrites=$global:DurationWrites
}} | ConvertTo-Json -Depth 8 | Set-Content -Encoding UTF8 -LiteralPath {_ps_string(security)}
& {_ps_string(ROOT / 'bin/hashi_restart_ctl.ps1')} trigger -HashiRoot {_ps_string(root)} -Python {_ps_string(sys.executable)}
""")

    assert result.returncode == 0, result.stderr
    assert started.read_text(encoding="utf-8-sig").strip() == "HashiRestart-restart-exact"
    access = json.loads(security.read_text(encoding="utf-8-sig"))
    assert access["RestartLimit"] == "PT5M"
    assert access["DurationWrites"] == (1 if configure == "ensure" else 0)
    assert access["FirstWrites"] == 2
    assert len(access["Writes"]) == 2  # A second ensure must not rewrite the ACL.
    assert {w["Name"] for w in access["Writes"]} == {
        "HashiRestart-restart-exact", "HashiRuntime-restart-exact",
    }
    assert all(w["Flags"] == 16 for w in access["Writes"])
    for task in access["Tasks"]:
        assert task["Owner"] == "S-1-5-32-544" and task["Group"] == "S-1-5-18"
        aces = {a["Sid"]: a["Mask"] for a in task["Aces"]}
        assert aces[access["Sid"]] == 0x120089  # Read delegation, no task editing.
        assert aces["S-1-5-18"] == 0x1F01FF
        assert aces["S-1-5-32-544"] == 0x1F01FF


@pytest.mark.parametrize("health_mode", ["ready", "local-ready", "foreign", "failed-agent"])
def test_restart_runner_stops_core_then_starts_separate_runtime_task(tmp_path, health_mode):
    root = tmp_path / "hashi restart runner"
    bin_dir = root / "bin"
    bin_dir.mkdir(parents=True)
    (root / "agents.json").write_text(
        json.dumps({"global": {"workbench_port": 18891, "instance_id": "RESTART-EXACT"}}),
        encoding="utf-8",
    )
    stopped = tmp_path / "stopped.txt"
    started = tmp_path / "started.txt"
    log = tmp_path / "restart.log"
    receipt = root / "state" / "instance" / "cold-restart" / "receipt.json"
    request = receipt.with_name("request.json")
    request.parent.mkdir(parents=True)
    request.write_text(
        json.dumps(
            {
                "schema": 1,
                "operation_id": "b" * 32,
                "instance_id": "RESTART-EXACT",
                "code_root": str(root),
                "hashi_root": str(root),
                "receipt_path": str(receipt),
                "requested_at": "2026-11-15T00:00:00Z",
                "deadline_epoch_ms": int(time.time() * 1000) + 20_000,
            }
        ),
        encoding="utf-8",
    )
    (bin_dir / "bridge_ctl.ps1").write_text(
        "param([string]$Action, [string]$BridgeHome)\n"
        f"Set-Content -LiteralPath {_ps_string(stopped)} -Value $Action\n"
        "if ($Action -ne 'stop') { exit 9 }\n"
        "exit 0\n",
        encoding="utf-8",
    )

    result = _powershell(f"""
$ErrorActionPreference = 'Stop'
$global:RuntimeTaskObject = [pscustomobject]@{{State=3; LastTaskResult=0}}
$global:TaskFolder = [pscustomobject]@{{}}
$global:TaskFolder | Add-Member -MemberType ScriptMethod -Name GetTask -Value {{
    param($TaskName) $global:RuntimeTaskObject
}}
$global:Scheduler = [pscustomobject]@{{}}
$global:Scheduler | Add-Member -MemberType ScriptMethod -Name Connect -Value {{}}
$global:Scheduler | Add-Member -MemberType ScriptMethod -Name GetFolder -Value {{
    param($Path) $global:TaskFolder
}}
$global:HealthCalls = 0
function New-Object {{ param($ComObject) $global:Scheduler }}
function Start-ScheduledTask {{
    param($TaskName, $ErrorAction)
    $global:RuntimeTaskObject.State = 4
    Set-Content -LiteralPath {_ps_string(started)} -Value $TaskName
    @{{
        schema=1; operation_id=('b' * 32);
        instance_id=$(if ('{health_mode}' -eq 'foreign') {{'OTHER'}} else {{'RESTART-EXACT'}});
        state='launch_accepted'; evidence=@{{core_pid=1234}}
    }} | ConvertTo-Json -Depth 5 -Compress |
        Set-Content -Encoding UTF8 -LiteralPath {_ps_string(receipt)}
}}
function Stop-ScheduledTask {{ param($TaskName, $ErrorAction) $global:RuntimeTaskObject.State = 3 }}
function Start-Sleep {{ param($Seconds, $Milliseconds) }}
function Invoke-RestMethod {{
    param($Uri, $Method, $TimeoutSec)
    $global:HealthCalls++
    [pscustomobject]@{{
        ok=$true
        instance_id=$(if ('{health_mode}' -eq 'foreign') {{ 'OTHER' }} else {{ 'RESTART-EXACT' }})
        ready=($global:HealthCalls -ge 2 -and '{health_mode}' -ne 'local-ready' -and '{health_mode}' -ne 'failed-agent')
        status=$(if ($global:HealthCalls -ge 2 -and '{health_mode}' -eq 'foreign') {{ 'ready' }} else {{ 'degraded' }})
        startup=[pscustomobject]@{{
            services_ready=($global:HealthCalls -ge 2)
            failed_agents=$(if ('{health_mode}' -eq 'failed-agent') {{ 1 }} else {{ 0 }})
        }}
    }}
}}
& {_ps_string(ROOT / 'bin/hashi_restart_task_runner.ps1')} `
    -HashiRoot {_ps_string(root)} `
    -CodeRoot {_ps_string(root)} `
    -LogPath {_ps_string(log)} `
    -RuntimeTaskName 'HashiRuntime-restart-exact'
exit $LASTEXITCODE
""")

    if health_mode == "foreign":
        assert result.returncode != 0
        assert "identity mismatch" in log.read_text(encoding="utf-8-sig")
        return
    assert result.returncode == 0, result.stderr
    assert stopped.read_text(encoding="utf-8-sig").strip() == "stop"
    assert started.read_text(encoding="utf-8-sig").strip() == (
        "HashiRuntime-restart-exact"
    )
    assert "launch accepted" in log.read_text(encoding="utf-8-sig")


def test_bridge_controller_does_not_swallow_process_termination_errors():
    text = (ROOT / "bin/bridge_ctl.ps1").read_text(encoding="utf-8")
    stop_block = text.split("function Stop-BridgeProcesses", 1)[1].split(
        "function Remove-StaleFiles", 1
    )[0]

    assert "Stop-Process -Id $procId -Force -ErrorAction Stop" in stop_block
    assert "$killFailures +=" in stop_block
    assert "Process termination reported errors" in stop_block
    assert "Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue" not in stop_block


def test_bridge_launch_acceptance_allows_only_selected_launcher_or_direct_base_child():
    result = _powershell(rf"""
$Tokens = $null
$ParseErrors = $null
$Ast = [System.Management.Automation.Language.Parser]::ParseFile(
    {_ps_string(ROOT / 'bin/bridge_ctl.ps1')}, [ref]$Tokens, [ref]$ParseErrors)
if ($ParseErrors.Count) {{ throw ($ParseErrors | Out-String) }}
$Functions = $Ast.FindAll({{
    param($Node) $Node -is [System.Management.Automation.Language.FunctionDefinitionAst]
}}, $false)
. ([scriptblock]::Create(($Functions | ForEach-Object {{ $_.Extent.Text }}) -join [Environment]::NewLine))
$global:ProcessTable = @{{
    700 = [pscustomobject]@{{ProcessId=700;ParentProcessId=600;ExecutablePath='C:\runtime\core\Scripts\python.exe'}}
    701 = [pscustomobject]@{{ProcessId=701;ParentProcessId=700;ExecutablePath='C:\runtime\base\python.exe'}}
    702 = [pscustomobject]@{{ProcessId=702;ParentProcessId=999;ExecutablePath='C:\runtime\base\python.exe'}}
    703 = [pscustomobject]@{{ProcessId=703;ParentProcessId=700;ExecutablePath='C:\foreign\python.exe'}}
}}
function Get-CimInstance {{
    param($ClassName, $Filter, $ErrorAction)
    if ($ClassName -ne 'Win32_Process' -or $Filter -notmatch '(\d+)$') {{ return $null }}
    return $global:ProcessTable[[int]$Matches[1]]
}}
$Approved = @('C:\runtime\core\Scripts\python.exe', 'C:\runtime\base\python.exe')
[ordered]@{{
    selected_launcher = Test-CoreLaunchProcess -CandidatePid 700 -LauncherPid 700 -ApprovedImages $Approved
    venv_base_child = Test-CoreLaunchProcess -CandidatePid 701 -LauncherPid 700 -ApprovedImages $Approved
    unrelated_parent = Test-CoreLaunchProcess -CandidatePid 702 -LauncherPid 700 -ApprovedImages $Approved
    foreign_image = Test-CoreLaunchProcess -CandidatePid 703 -LauncherPid 700 -ApprovedImages $Approved
}} | ConvertTo-Json -Compress
""")
    assert result.returncode == 0, result.stderr
    observed = json.loads(result.stdout)
    assert observed == {
        "selected_launcher": True,
        "venv_base_child": True,
        "unrelated_parent": False,
        "foreign_image": False,
    }


def test_bridge_selected_venv_expands_only_to_its_declared_base_image(tmp_path):
    environment = tmp_path / "core"
    selected = environment / "Scripts" / "python.exe"
    base = tmp_path / "base"
    selected.parent.mkdir(parents=True)
    base.mkdir()
    selected.write_bytes(b"launcher")
    (base / "python.exe").write_bytes(b"base")
    (environment / "pyvenv.cfg").write_text(
        f"home = {base}\ninclude-system-site-packages = false\n", encoding="utf-8"
    )
    result = _powershell(f"""
$Tokens = $null
$ParseErrors = $null
$Ast = [System.Management.Automation.Language.Parser]::ParseFile(
    {_ps_string(ROOT / 'bin/bridge_ctl.ps1')}, [ref]$Tokens, [ref]$ParseErrors)
if ($ParseErrors.Count) {{ throw ($ParseErrors | Out-String) }}
$Function = $Ast.Find({{
    param($Node) $Node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
        $Node.Name -eq 'Get-ApprovedCoreProcessImages'
}}, $false)
. ([scriptblock]::Create($Function.Extent.Text))
@(Get-ApprovedCoreProcessImages -SelectedExecutable {_ps_string(selected)}) |
    ConvertTo-Json -Compress
""")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [str(selected), str(base / "python.exe")]


def test_bridge_checks_core_receipt_before_treating_launcher_exit_as_rejection():
    text = (ROOT / "bin/bridge_ctl.ps1").read_text(encoding="utf-8")
    start = text.split("function Start-Bridge", 1)[1].split(
        "function Show-Status", 1
    )[0]
    assert start.index("$BridgePid = Get-BridgePid") < start.index(
        "if ($Launcher.HasExited)"
    )


@pytest.mark.parametrize("expired", [False, True])
def test_bridge_consumes_active_request_and_discards_expired_stale_request(
    tmp_path, expired
):
    code = tmp_path / "code"
    home = tmp_path / "home"
    request = home / "state" / "instance" / "cold-restart" / "request.json"
    receipt = request.with_name("receipt.json")
    request.parent.mkdir(parents=True)
    code.mkdir()
    request.write_text(
        json.dumps(
            {
                "schema": 1,
                "operation_id": "a" * 32,
                "instance_id": "HASHI4",
                "code_root": str(code),
                "hashi_root": str(home),
                "receipt_path": str(receipt),
                "deadline_epoch_ms": int(time.time() * 1000)
                + (-1000 if expired else 30_000),
            }
        ),
        encoding="utf-8",
    )
    result = _powershell(f"""
$Tokens = $null
$ParseErrors = $null
$Ast = [System.Management.Automation.Language.Parser]::ParseFile(
    {_ps_string(ROOT / 'bin/bridge_ctl.ps1')}, [ref]$Tokens, [ref]$ParseErrors)
if ($ParseErrors.Count) {{ throw ($ParseErrors | Out-String) }}
$Function = $Ast.Find({{
    param($Node) $Node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
        $Node.Name -eq 'Get-StartupRequest'
}}, $false)
. ([scriptblock]::Create($Function.Extent.Text))
$StartupRequestPath = {_ps_string(request)}
$BridgeHome = {_ps_string(home)}
$ProjectDir = {_ps_string(code)}
$InstanceId = 'HASHI4'
$Value = Get-StartupRequest
[ordered]@{{
    is_null = $null -eq $Value
    request_exists = Test-Path -LiteralPath $StartupRequestPath
    operation_id = if ($null -eq $Value) {{ $null }} else {{ [string]$Value.operation_id }}
}} | ConvertTo-Json -Compress
""")
    assert result.returncode == 0, result.stderr
    observed = json.loads(result.stdout)
    assert observed["request_exists"] is False
    assert observed["is_null"] is expired
    assert observed["operation_id"] == (None if expired else "a" * 32)


@pytest.mark.parametrize("explicit_gateway,old_gateway", [(False, False), (True, False), (False, True)])
def test_bridge_controller_launches_qualified_core_with_gateway_and_scoped_logs(tmp_path, explicit_gateway, old_gateway):
    root = tmp_path / "hashi start with spaces"
    scripts = root / "scripts"
    scripts.mkdir(parents=True)
    qualified_core = root / "qualified Core" / "python.exe"
    (scripts / "resolve_launch_runtime.py").write_text(
        f"print({str(qualified_core)!r})\n", encoding="utf-8"
    )
    captured = tmp_path / "launch.json"
    result = _powershell(f"""
$ErrorActionPreference = 'Stop'
$Tokens = $null
$ParseErrors = $null
$Ast = [System.Management.Automation.Language.Parser]::ParseFile(
    {_ps_string(ROOT / 'bin/bridge_ctl.ps1')}, [ref]$Tokens, [ref]$ParseErrors)
if ($ParseErrors.Count) {{ throw ($ParseErrors | Out-String) }}
$Functions = $Ast.FindAll({{
    param($Node) $Node -is [System.Management.Automation.Language.FunctionDefinitionAst]
}}, $false)
. ([scriptblock]::Create(($Functions | ForEach-Object {{ $_.Extent.Text }}) -join [Environment]::NewLine))
$ProjectDir = {_ps_string(root)}
$BridgeHome = $ProjectDir
$MainScript = Join-Path $ProjectDir 'main.py'
$PythonExe = {_ps_string(sys.executable)}
$global:Launched = $false
$global:Stopped = $false
function Get-BridgeProcesses {{
    if ($global:Launched) {{ @{{123=@{{Name='python.exe'}}}} }}
    elseif (-not $global:Stopped -and [bool]::Parse('{old_gateway}')) {{
        @{{122=@{{Cmd='python main.py --api-gateway'}}}}
    }} else {{ @{{}} }}
}}
function Test-LockFileHeld {{ $false }}
function Stop-BridgeProcesses {{ $global:Stopped = $true; $true }}
function Remove-StaleFiles {{ }}
function Get-ResumeAgentNames {{ @('alpha', 'beta space') }}
function Start-Sleep {{ }}
function Start-Process {{
    param($FilePath, $ArgumentList, $WorkingDirectory, $WindowStyle,
          $RedirectStandardOutput, $RedirectStandardError, [switch]$PassThru)
    @{{FilePath=$FilePath; Arguments=$ArgumentList; Directory=$WorkingDirectory;
      WindowStyle=$WindowStyle; Stdout=$RedirectStandardOutput; Stderr=$RedirectStandardError}} |
        ConvertTo-Json | Set-Content -Encoding UTF8 -LiteralPath {_ps_string(captured)}
    $global:Launched = $true
    [pscustomobject]@{{HasExited=$false}}
}}
# Exercise the real restart dispatch with no old Core command-line evidence.
$Action = 'restart'
$Resume = $true
$Force = $false
$ApiGateway = [bool]::Parse('{explicit_gateway}')
$Dispatch = $Ast.EndBlock.Statements |
    Where-Object {{ $_ -is [System.Management.Automation.Language.SwitchStatementAst] }}
& ([scriptblock]::Create($Dispatch.Extent.Text))
""")
    assert result.returncode == 0, result.stderr
    launch = json.loads(captured.read_text(encoding="utf-8-sig"))
    assert launch["FilePath"] == str(qualified_core)
    assert launch["Directory"] == str(root)
    assert launch["WindowStyle"] == "Hidden"

    # Exercise native Windows argv parsing, including spaces in both roots and Agents.
    import ctypes
    argc = ctypes.c_int()
    parser = ctypes.windll.shell32.CommandLineToArgvW
    parser.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_int)]
    parser.restype = ctypes.POINTER(ctypes.c_wchar_p)
    argv = parser("python.exe " + launch["Arguments"], ctypes.byref(argc))
    try:
        arguments = [argv[index] for index in range(argc.value)][1:]
    finally:
        ctypes.windll.kernel32.LocalFree(ctypes.cast(argv, ctypes.c_void_p))
    assert arguments == [
        "-I", "-B", str(root / "main.py"), "--bridge-home", str(root),
        "--agents", "alpha", "beta space",
    ] + (["--api-gateway"] if explicit_gateway or old_gateway else [])
    assert Path(launch["Stdout"]).parent == root / "logs"
    assert Path(launch["Stderr"]).parent == root / "logs"
    assert launch["Stdout"] != launch["Stderr"]


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
    (remote / "identity_cache_probe.py").write_text("VALUE = 1\n", encoding="utf-8")
    (remote / "supervisor_identity.py").write_text(
        "import identity_cache_probe, runpy\n"
        f"runpy.run_path({str(ROOT / 'remote/supervisor_identity.py')!r}, run_name='__main__')\n",
        encoding="utf-8",
    )
    (remote / "__init__.py").write_text("", encoding="utf-8")
    (remote / "__main__.py").write_text(
        "import ctypes, json, os, sys\nfrom pathlib import Path\n"
        "kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)\n"
        "kernel32.GetCurrentProcess.restype = ctypes.c_void_p\n"
        "kernel32.GetPriorityClass.argtypes = [ctypes.c_void_p]\n"
        "Path('received.json').write_text(json.dumps(sys.argv[1:]), encoding='utf-8')\n"
        "print('REMOTE_STDOUT_OK', flush=True)\n"
        "print(f'REMOTE_BYTECODE_DISABLED={sys.dont_write_bytecode}', flush=True)\n"
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
    assert not (remote / "__pycache__").exists()  # Identity preflight must preserve a sealed base too.
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
    assert not log.startswith(b"\xff\xfe")
    text = log.decode("utf-8-sig")
    for marker in (
        "REMOTE_STDOUT_OK",
        "REMOTE_STDERR_OK",
        "REMOTE_AFTER_STDERR_OK",
        "REMOTE_UTF8_🌸_测试",
        "REMOTE_PRIORITY=32",
        "REMOTE_BYTECODE_DISABLED=True",
    ):
        assert marker in text
    assert not (remote / "__pycache__").exists()
    settings = json.loads(settings_path.read_text(encoding="utf-8-sig"))
    assert settings["Priority"] == 4


def test_legacy_task_runner_keeps_python_module_switch(tmp_path):
    (tmp_path / "legacy_probe.py").write_text(
        "import sys, json\n"
        "print(json.dumps({'args': sys.argv[1:], 'bytecode_disabled': sys.dont_write_bytecode}))\n",
        encoding="utf-8",
    )
    log = tmp_path / "legacy.log"
    result = _powershell(f"""
& {_ps_string(ROOT / 'bin/hashi_remote_task_runner.ps1')} -Python {_ps_string(sys.executable)} -HashiRoot {_ps_string(tmp_path)} -LogPath {_ps_string(log)} -PythonArgs '-m legacy_probe --help'
exit $LASTEXITCODE
""")
    assert result.returncode == 0, result.stderr
    data = log.read_bytes()
    assert not data.startswith(b"\xff\xfe")
    text = data.decode("utf-8-sig")
    assert json.loads(text.strip()) == {"args": ["--help"], "bytecode_disabled": True}
    assert not (tmp_path / "__pycache__").exists()


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


@pytest.mark.parametrize("admin_only_access", [False, True])
def test_restart_keeps_existing_readable_secrets_acl_for_current_principal(tmp_path, admin_only_access):
    import ctypes
    if admin_only_access and not ctypes.windll.shell32.IsUserAnAdmin():
        pytest.skip("Elevated setup versus Limited runtime ACL boundary")
    root = tmp_path / "hashi protected secrets"
    remote = root / "remote"
    remote.mkdir(parents=True)
    _stub_fixed_lifecycle_setup(root)
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
    if admin_only_access:
        shutil.copyfile(ROOT / "tools/private_files.py", staged / "tools/private_files.py")
    else:
        (staged / "tools/private_files.py").write_text(
            "raise SystemExit('ACL_HELPER_CALLED')\n", encoding="utf-8",
        )
    observed = tmp_path / "acl-observed.json"
    result = _powershell(f"""
$ErrorActionPreference = 'Stop'
$Secrets = {_ps_string(root / 'secrets.json')}
if ('{admin_only_access}' -eq 'True') {{
    $Acl = New-Object System.Security.AccessControl.FileSecurity
    $Acl.SetAccessRuleProtection($true, $false)
    foreach ($SidText in @('S-1-5-18', 'S-1-5-32-544')) {{
        $Sid = New-Object System.Security.Principal.SecurityIdentifier($SidText)
        $Acl.AddAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule($Sid, 'FullControl', 'Allow')))
    }}
    Set-Acl -LiteralPath $Secrets -AclObject $Acl
}} else {{
    $Acl = Get-Acl -LiteralPath $Secrets
    $Sid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User
    $Acl.SetAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule($Sid, 'FullControl', 'Allow')))
    Set-Acl -LiteralPath $Secrets -AclObject $Acl
}}
$Before = (Get-Acl -LiteralPath $Secrets).Sddl
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
$After = Get-Acl -LiteralPath $Secrets
$CurrentSid = [string][System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$Allowed = @($After.Access | Where-Object {{
    [string]$_.IdentityReference.Translate([System.Security.Principal.SecurityIdentifier]).Value -eq $CurrentSid -and
    $_.AccessControlType -eq 'Allow' -and
    ($_.FileSystemRights -band [System.Security.AccessControl.FileSystemRights]::FullControl) -eq [System.Security.AccessControl.FileSystemRights]::FullControl
}}).Count -gt 0
[pscustomobject]@{{before=$Before;after=$After.Sddl;allowed=$Allowed}} | ConvertTo-Json | Set-Content -Encoding UTF8 -LiteralPath {_ps_string(observed)}
""")

    assert result.returncode == 0, result.stderr
    assert "ACL_HELPER_CALLED" not in result.stderr
    acl = json.loads(observed.read_text(encoding="utf-8-sig"))
    assert acl["allowed"] is True
    assert (acl["before"] != acl["after"]) is admin_only_access


def test_restart_retires_only_exact_instance_remote_processes(tmp_path):
    root = tmp_path / "hashi space 测试"
    other = tmp_path / "other instance"
    remote = root / "remote"
    remote.mkdir(parents=True)
    _stub_fixed_lifecycle_setup(root)
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
    _stub_fixed_lifecycle_setup(root)
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
    _stub_fixed_lifecycle_setup(root)
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
    _stub_fixed_lifecycle_setup(root)
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
# HASHI-Fingerprint: hfp_fe0457bf7c984c12b12d7aa97547a06a

def test_missing_fixed_restart_task_fails_before_stopping_or_launching(tmp_path):
    root = tmp_path / "hashi manual fallback"
    remote = root / "remote"
    remote.mkdir(parents=True)
    (root / "agents.json").write_text(
        json.dumps({"global": {"instance_id": "RESTART-EXACT"}}), encoding="utf-8"
    )
    shutil.copyfile(ROOT / "remote/supervisor_identity.py", remote / "supervisor_identity.py")
    captured = tmp_path / "fallback.json"
    result = _powershell(f"""
$ErrorActionPreference = 'Stop'
function Get-ScheduledTask {{ param($TaskName, $ErrorAction) $null }}
function powershell.exe {{
    param([switch]$NoProfile, [switch]$NonInteractive, $ExecutionPolicy,
          $File, $Action, [switch]$Resume, [switch]$ApiGateway)
    @{{File=$File; Action=$Action; Resume=[bool]$Resume; ApiGateway=[bool]$ApiGateway}} |
        ConvertTo-Json | Set-Content -Encoding UTF8 -LiteralPath {_ps_string(captured)}
    $global:LASTEXITCODE = 0
}}
& {_ps_string(ROOT / 'bin/hashi_restart_ctl.ps1')} trigger -HashiRoot {_ps_string(root)} -Python {_ps_string(sys.executable)}
""")
    assert result.returncode != 0
    assert not captured.exists()
    assert "Missing fixed restart task" in result.stderr
