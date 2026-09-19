param(
    [Parameter(Position = 0)]
    [ValidateSet("ensure", "register", "trigger", "unregister", "status")]
    [string]$Action = "status",

    [string]$HashiRoot,
    [string]$Python,
    [string]$TaskUserId,
    [string]$InstanceId
)

$ErrorActionPreference = "Stop"
if (-not $HashiRoot) {
    $HashiRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
}
$HashiRoot = ([System.IO.Path]::GetFullPath($HashiRoot)).TrimEnd('\')
if (-not $Python) {
    $VenvPython = Join-Path $HashiRoot ".venv\Scripts\python.exe"
    $Python = if (Test-Path -LiteralPath $VenvPython -PathType Leaf) {
        $VenvPython
    } else {
        "python"
    }
}

$IdentityScript = Join-Path $HashiRoot "remote\supervisor_identity.py"
if (-not (Test-Path -LiteralPath $IdentityScript -PathType Leaf)) {
    throw "Missing supervisor identity helper: $IdentityScript"
}
$IdentityArgs = @($IdentityScript, "--hashi-root", $HashiRoot, "--format", "json")
if ($InstanceId) {
    $IdentityArgs += @("--instance-id", $InstanceId)
}
$IdentityJson = & $Python @IdentityArgs
if ($LASTEXITCODE -ne 0) {
    throw "Could not resolve the HASHI restart task identity."
}
$SupervisorIdentity = $IdentityJson | ConvertFrom-Json
$TaskName = [string]$SupervisorIdentity.windows_restart_task_name
if (-not $TaskName -or $TaskName -match '[\\/]') {
    throw "Invalid fixed restart task name: $TaskName"
}

$TaskRunner = Join-Path $PSScriptRoot "hashi_restart_task_runner.ps1"
if (-not (Test-Path -LiteralPath $TaskRunner -PathType Leaf)) {
    throw "Missing fixed restart task runner: $TaskRunner"
}
$LogPath = Join-Path $HashiRoot "logs\hashi-restart-task.log"
$RunnerArgs = @(
    "-NoProfile",
    "-NonInteractive",
    "-ExecutionPolicy", "Bypass",
    "-WindowStyle", "Hidden",
    "-File", "`"$TaskRunner`"",
    "-HashiRoot", "`"$HashiRoot`"",
    "-LogPath", "`"$LogPath`""
)
$RunnerArgumentString = $RunnerArgs -join " "

function Resolve-RestartTaskUser {
    if ($TaskUserId) {
        return $TaskUserId
    }
    return [string][System.Security.Principal.WindowsIdentity]::GetCurrent().Name
}

function Test-FixedRestartTask {
    param($Task)

    if ($null -eq $Task) {
        return $false
    }
    $TaskActions = @($Task.Actions)
    if ($TaskActions.Count -ne 1) {
        return $false
    }
    $TaskAction = $TaskActions[0]
    $Executable = [System.IO.Path]::GetFileName([string]$TaskAction.Execute)
    $WorkingDirectory = ([string]$TaskAction.WorkingDirectory).TrimEnd('\')
    $RunLevel = [string]$Task.Principal.RunLevel
    return (
        $Executable.Equals("powershell.exe", [System.StringComparison]::OrdinalIgnoreCase) -and
        $WorkingDirectory.Equals($HashiRoot, [System.StringComparison]::OrdinalIgnoreCase) -and
        $RunLevel.Equals("Highest", [System.StringComparison]::OrdinalIgnoreCase) -and
        ([string]$TaskAction.Arguments).Equals(
            $RunnerArgumentString,
            [System.StringComparison]::OrdinalIgnoreCase
        )
    )
}

function Register-FixedRestartTask {
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $LogPath) | Out-Null
    $TaskAction = New-ScheduledTaskAction `
        -Execute "powershell.exe" `
        -Argument $RunnerArgumentString `
        -WorkingDirectory $HashiRoot
    $Settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -Priority 4
    $Principal = New-ScheduledTaskPrincipal `
        -UserId (Resolve-RestartTaskUser) `
        -LogonType Interactive `
        -RunLevel Highest
    $Task = New-ScheduledTask -Action $TaskAction -Settings $Settings -Principal $Principal
    Register-ScheduledTask `
        -TaskName $TaskName `
        -InputObject $Task `
        -Force `
        -ErrorAction Stop | Out-Null
    Write-Host "Registered fixed HASHI restart task '$TaskName'"
}

switch ($Action) {
    "register" {
        Register-FixedRestartTask
    }
    "ensure" {
        $Task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        if ($null -eq $Task) {
            Register-FixedRestartTask
        } elseif (-not (Test-FixedRestartTask -Task $Task)) {
            Register-FixedRestartTask
        }
    }
    "trigger" {
        $Task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        if ($null -eq $Task) {
            # A non-elevated/manual Core needs no privilege bridge. Keep the
            # exact same fixed runner and root boundary in that configuration.
            Write-Warning "Fixed restart task '$TaskName' is not registered; using same-privilege fixed restart."
            & $TaskRunner -HashiRoot $HashiRoot -LogPath $LogPath
            exit $LASTEXITCODE
        }
        if (-not (Test-FixedRestartTask -Task $Task)) {
            throw "Refusing mismatched fixed restart task '$TaskName'"
        }
        Start-ScheduledTask -TaskName $TaskName -ErrorAction Stop
        Write-Host "Triggered fixed HASHI restart task '$TaskName'"
    }
    "unregister" {
        Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        Unregister-ScheduledTask `
            -TaskName $TaskName `
            -Confirm:$false `
            -ErrorAction SilentlyContinue
    }
    "status" {
        $Task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        [PSCustomObject]@{
            InstanceId = $SupervisorIdentity.instance_id
            TaskName = $TaskName
            Registered = $null -ne $Task
            DefinitionMatches = Test-FixedRestartTask -Task $Task
            Runner = $TaskRunner
            HashiRoot = $HashiRoot
        } | Format-List
    }
}
