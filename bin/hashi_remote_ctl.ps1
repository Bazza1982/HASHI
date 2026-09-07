param(
    [Parameter(Position = 0)]
    [ValidateSet("register", "enable", "disable", "unregister", "install", "uninstall", "start", "stop", "restart", "status", "logs", "command", "doctor")]
    [string]$Action = "status",

    [string]$HashiRoot,
    [string]$Python,
    [string]$TaskName,
    [string]$InstanceId = $env:HASHI_INSTANCE_ID,
    [string]$MaxTerminalLevel = $env:HASHI_REMOTE_MAX_TERMINAL_LEVEL,
    [string]$Discovery = $env:HASHI_REMOTE_DISCOVERY,
    [string]$Port = $env:HASHI_REMOTE_PORT,
    [switch]$NoTls
)

$ErrorActionPreference = "Stop"

if (-not $HashiRoot) {
    $HashiRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
}

if (-not $Python) {
    $VenvPython = Join-Path $HashiRoot ".venv\Scripts\python.exe"
    if (Test-Path $VenvPython) {
        $Python = $VenvPython
    } else {
        $Python = "python"
    }
}

$IdentityScript = Join-Path $HashiRoot "remote\supervisor_identity.py"
if (-not (Test-Path $IdentityScript)) {
    throw "Missing supervisor identity helper: $IdentityScript"
}
$IdentityArgs = @($IdentityScript, "--hashi-root", $HashiRoot, "--format", "json")
if ($InstanceId) {
    $IdentityArgs += @("--instance-id", $InstanceId)
}
$IdentityJson = & $Python @IdentityArgs
if ($LASTEXITCODE -ne 0) {
    throw "Could not resolve the Hashi Remote supervisor identity."
}
$SupervisorIdentity = $IdentityJson | ConvertFrom-Json
if (-not $TaskName) {
    $TaskName = $SupervisorIdentity.windows_task_name
}
if ($TaskName -match '[\\/]') {
    throw "Invalid scheduled task name: $TaskName"
}

$LogDir = Join-Path $HashiRoot "logs"
$LogPath = Join-Path $LogDir "hashi-remote-supervisor.log"
$ArgsList = @("-m", "remote", "--hashi-root", [string]$HashiRoot, "--supervised")
$TaskRunner = Join-Path $PSScriptRoot "hashi_remote_task_runner.ps1"

if ($NoTls -or $env:HASHI_REMOTE_NO_TLS -eq "1") {
    $ArgsList += "--no-tls"
}
if ($MaxTerminalLevel) {
    $ArgsList += @("--max-terminal-level", $MaxTerminalLevel)
}
if ($Discovery) {
    $ArgsList += @("--discovery", $Discovery)
}
if ($Port) {
    $ArgsList += @("--port", $Port)
}

$ArgumentString = ($ArgsList -join " ")
$CommandPreview = "$Python $ArgumentString"

function Ensure-LogDir {
    New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
}

function Register-HashiRemoteSupervisor {
    Ensure-LogDir
    $RunnerArgs = @(
        "-NoProfile",
        "-ExecutionPolicy", "Bypass",
        "-WindowStyle", "Hidden",
        "-File", "`"$TaskRunner`"",
        "-Python", "`"$Python`"",
        "-HashiRoot", "`"$HashiRoot`"",
        "-LogPath", "`"$LogPath`""
    )
    if ($ArgsList.Count -gt 0) {
        # Pass argv as data; nested command-line quotes lose paths and flags.
        $ArgsJson = ConvertTo-Json -InputObject @($ArgsList) -Compress
        $ArgsBase64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($ArgsJson))
        $RunnerArgs += @("-PythonArgsBase64", $ArgsBase64)
    }
    $Action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument ($RunnerArgs -join " ") -WorkingDirectory $HashiRoot
    $Trigger = New-ScheduledTaskTrigger -AtLogOn
    $Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)
    $Principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
    $Task = New-ScheduledTask -Action $Action -Trigger $Trigger -Settings $Settings -Principal $Principal
    Register-ScheduledTask -TaskName $TaskName -InputObject $Task -Force | Out-Null
    Write-Host "Registered and enabled Remote supervisor task '$TaskName'"
    Write-Host $CommandPreview
}

function Get-RemotePort {
    if ($Port) {
        return [int]$Port
    }
    $ConfigPath = Join-Path $HashiRoot "remote\config.yaml"
    if (Test-Path $ConfigPath) {
        $Match = Select-String -Path $ConfigPath -Pattern '^\s*port:\s*(\d+)' | Select-Object -First 1
        if ($Match -and $Match.Matches.Count -gt 0) {
            return [int]$Match.Matches[0].Groups[1].Value
        }
    }
    return 8766
}

function Show-RemoteDoctor {
    $EffectivePort = Get-RemotePort
    $FirewallRules = Get-NetFirewallRule -Direction Inbound -Enabled True -ErrorAction SilentlyContinue |
        Where-Object { $_.DisplayName -match "Hashi|Remote|Python" }
    $Listening = Get-NetTCPConnection -LocalPort $EffectivePort -State Listen -ErrorAction SilentlyContinue
    $WslStatus = $null
    if (Get-Command wsl.exe -ErrorAction SilentlyContinue) {
        $WslStatus = (& wsl.exe --status 2>$null) -join "`n"
    }
    [PSCustomObject]@{
        HashiRoot = $HashiRoot
        RemotePort = $EffectivePort
        Listening = [bool]$Listening
        FirewallRuleCount = @($FirewallRules).Count
        FirewallRules = @($FirewallRules | Select-Object -ExpandProperty DisplayName)
        WslAvailable = [bool](Get-Command wsl.exe -ErrorAction SilentlyContinue)
        WslStatus = $WslStatus
        Command = $CommandPreview
    } | Format-List
}

switch ($Action) {
    { $_ -in "register", "install" } {
        Register-HashiRemoteSupervisor
    }
    "enable" {
        Register-HashiRemoteSupervisor
        Enable-ScheduledTask -TaskName $TaskName | Out-Null
        Start-ScheduledTask -TaskName $TaskName
        Write-Host "Activated Remote supervisor task '$TaskName'"
    }
    "disable" {
        Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        Disable-ScheduledTask -TaskName $TaskName | Out-Null
        Write-Host "Disabled Remote supervisor task '$TaskName'"
    }
    { $_ -in "unregister", "uninstall" } {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
        Write-Host "Unregistered Remote supervisor task '$TaskName'"
    }
    "start" {
        Start-ScheduledTask -TaskName $TaskName
    }
    "stop" {
        Stop-ScheduledTask -TaskName $TaskName
    }
    "restart" {
        Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        Start-ScheduledTask -TaskName $TaskName
    }
    "status" {
        $Task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        if ($null -eq $Task) {
            Write-Host "Remote supervisor task '$TaskName' is not registered"
            exit 2
        }
        $Info = Get-ScheduledTaskInfo -TaskName $TaskName
        [PSCustomObject]@{
            InstanceId = $SupervisorIdentity.instance_id
            TaskName = $Task.TaskName
            State = $Task.State
            LastRunTime = $Info.LastRunTime
            LastTaskResult = $Info.LastTaskResult
            NextRunTime = $Info.NextRunTime
            Command = $CommandPreview
        } | Format-List
    }
    "logs" {
        if (Test-Path $LogPath) {
            Get-Content -Path $LogPath -Tail 120
        } else {
            Write-Host "No supervisor log found at $LogPath"
        }
    }
    "command" {
        [PSCustomObject]@{
            InstanceId = $SupervisorIdentity.instance_id
            TaskName = $TaskName
            Command = $CommandPreview
        } | Format-List
    }
    "doctor" {
        Show-RemoteDoctor
    }
}
