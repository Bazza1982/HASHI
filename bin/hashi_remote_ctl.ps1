param(
    [Parameter(Position = 0)]
    [ValidateSet("install", "uninstall", "start", "stop", "restart", "status", "logs", "command", "doctor")]
    [string]$Action = "status",

    [string]$HashiRoot,
    [string]$Python,
    [string]$TaskName = "HashiRemote",
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

$LogDir = Join-Path $HashiRoot "logs"
$LogPath = Join-Path $LogDir "hashi-remote-supervisor.log"
$ArgsList = @("-m", "remote", "--hashi-root", "`"$HashiRoot`"", "--supervised")

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

function Install-HashiRemoteTask {
    Ensure-LogDir
    $Action = New-ScheduledTaskAction -Execute $Python -Argument $ArgumentString -WorkingDirectory $HashiRoot
    $Trigger = New-ScheduledTaskTrigger -AtLogOn
    $Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)
    $Principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
    $Task = New-ScheduledTask -Action $Action -Trigger $Trigger -Settings $Settings -Principal $Principal
    Register-ScheduledTask -TaskName $TaskName -InputObject $Task -Force | Out-Null
    Write-Host "Installed scheduled task '$TaskName'"
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
    "install" {
        Install-HashiRemoteTask
    }
    "uninstall" {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
        Write-Host "Uninstalled scheduled task '$TaskName'"
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
            Write-Host "Task '$TaskName' is not installed"
            exit 2
        }
        $Info = Get-ScheduledTaskInfo -TaskName $TaskName
        [PSCustomObject]@{
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
        Write-Host $CommandPreview
    }
    "doctor" {
        Show-RemoteDoctor
    }
}
