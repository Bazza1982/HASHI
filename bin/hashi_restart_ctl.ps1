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
$IdentityArgs = @("-B", $IdentityScript, "--hashi-root", $HashiRoot, "--format", "json")
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
$RuntimeTaskName = [string]$SupervisorIdentity.windows_runtime_task_name
if (-not $RuntimeTaskName -or $RuntimeTaskName -match '[\\/]') {
    throw "Invalid fixed runtime task name: $RuntimeTaskName"
}

$TaskRunner = Join-Path $PSScriptRoot "hashi_restart_task_runner.ps1"
if (-not (Test-Path -LiteralPath $TaskRunner -PathType Leaf)) {
    throw "Missing fixed restart task runner: $TaskRunner"
}
$RuntimeController = Join-Path $PSScriptRoot "bridge_ctl.ps1"
if (-not (Test-Path -LiteralPath $RuntimeController -PathType Leaf)) {
    throw "Missing fixed HASHI runtime controller: $RuntimeController"
}
$LogPath = Join-Path $HashiRoot "logs\hashi-restart-task.log"
$RestartTaskLimit = New-TimeSpan -Minutes 10
$RunnerArgs = @(
    "-NoProfile",
    "-NonInteractive",
    "-ExecutionPolicy", "Bypass",
    "-WindowStyle", "Hidden",
    "-File", "`"$TaskRunner`"",
    "-HashiRoot", "`"$HashiRoot`"",
    "-LogPath", "`"$LogPath`"",
    "-RuntimeTaskName", "`"$RuntimeTaskName`""
)
$RunnerArgumentString = $RunnerArgs -join " "
$RuntimeArgs = @(
    "-NoProfile",
    "-NonInteractive",
    "-ExecutionPolicy", "Bypass",
    "-WindowStyle", "Hidden",
    "-File", "`"$RuntimeController`"",
    "-Action", "start",
    "-Resume",
    "-ApiGateway"
)
$RuntimeArgumentString = $RuntimeArgs -join " "

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

function Test-FixedRuntimeTask {
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
            $RuntimeArgumentString,
            [System.StringComparison]::OrdinalIgnoreCase
        )
    )
}

function Grant-FixedTaskReadAccess {
    param([string]$Name, [string]$UserId)

    if (-not $UserId) { throw "Missing principal for fixed task '$Name'" }
    $UserSid = if ($UserId -match '^S-1-') {
        [Security.Principal.SecurityIdentifier]::new($UserId)
    } else {
        [Security.Principal.NTAccount]::new($UserId).Translate(
            [Security.Principal.SecurityIdentifier]
        )
    }
    $Scheduler = New-Object -ComObject 'Schedule.Service'
    $Scheduler.Connect()
    $Task = $Scheduler.GetFolder('\').GetTask($Name)
    $Security = [Security.AccessControl.RawSecurityDescriptor]::new(
        $Task.GetSecurityDescriptor(7)
    )
    $ReadMask = 0x120089
    foreach ($Ace in $Security.DiscretionaryAcl) {
        if ($Ace -is [Security.AccessControl.CommonAce] -and
            $Ace.AceQualifier -eq [Security.AccessControl.AceQualifier]::AccessAllowed -and
            $Ace.SecurityIdentifier -eq $UserSid -and
            ($Ace.AccessMask -band $ReadMask) -eq $ReadMask) {
            return
        }
    }
    if ($null -eq $Security.DiscretionaryAcl) {
        throw "Missing access control list for fixed task '$Name'"
    }
    # The Limited Remote must see the fixed tasks registered by an elevated
    # installer. Delegate read access only, retaining every existing ACE.
    $ReadAce = [Security.AccessControl.CommonAce]::new(
        [Security.AccessControl.AceFlags]::None,
        [Security.AccessControl.AceQualifier]::AccessAllowed,
        $ReadMask, $UserSid, $false, $null
    )
    $Index = 0
    while ($Index -lt $Security.DiscretionaryAcl.Count -and
           -not $Security.DiscretionaryAcl[$Index].IsInherited) { $Index++ }
    $Security.DiscretionaryAcl.InsertAce($Index, $ReadAce)
    # TASK_DONT_ADD_PRINCIPAL_ACE prevents an implicit broader principal grant.
    $Task.SetSecurityDescriptor(
        $Security.GetSddlForm([Security.AccessControl.AccessControlSections]::Access),
        16
    )
    $Verified = [Security.AccessControl.RawSecurityDescriptor]::new(
        $Task.GetSecurityDescriptor(7)
    )
    foreach ($Ace in $Verified.DiscretionaryAcl) {
        if ($Ace -is [Security.AccessControl.CommonAce] -and
            $Ace.AceQualifier -eq [Security.AccessControl.AceQualifier]::AccessAllowed -and
            $Ace.SecurityIdentifier -eq $UserSid -and
            ($Ace.AccessMask -band $ReadMask) -eq $ReadMask) { return }
    }
    throw "Read delegation verification failed for fixed task '$Name'"
}

function Register-FixedRuntimeTask {
    $TaskAction = New-ScheduledTaskAction `
        -Execute "powershell.exe" `
        -Argument $RuntimeArgumentString `
        -WorkingDirectory $HashiRoot
    $Settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -ExecutionTimeLimit ([TimeSpan]::Zero) `
        -MultipleInstances IgnoreNew `
        -Priority 4
    $Principal = New-ScheduledTaskPrincipal `
        -UserId (Resolve-RestartTaskUser) `
        -LogonType Interactive `
        -RunLevel Highest
    $Task = New-ScheduledTask -Action $TaskAction -Settings $Settings -Principal $Principal
    Register-ScheduledTask `
        -TaskName $RuntimeTaskName `
        -InputObject $Task `
        -Force `
        -ErrorAction Stop | Out-Null
    Grant-FixedTaskReadAccess -Name $RuntimeTaskName -UserId $Principal.UserId
    Write-Host "Registered fixed HASHI runtime task '$RuntimeTaskName'"
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
        -ExecutionTimeLimit $RestartTaskLimit `
        -MultipleInstances IgnoreNew `
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
    Grant-FixedTaskReadAccess -Name $TaskName -UserId $Principal.UserId
    Write-Host "Registered fixed HASHI restart task '$TaskName'"
}

switch ($Action) {
    "register" {
        Register-FixedRuntimeTask
        Register-FixedRestartTask
    }
    "ensure" {
        $RuntimeTask = Get-ScheduledTask -TaskName $RuntimeTaskName -ErrorAction SilentlyContinue
        if ($null -eq $RuntimeTask) {
            Register-FixedRuntimeTask
        } elseif (-not (Test-FixedRuntimeTask -Task $RuntimeTask)) {
            Register-FixedRuntimeTask
        } else {
            Grant-FixedTaskReadAccess -Name $RuntimeTaskName -UserId $RuntimeTask.Principal.UserId
        }
        $Task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        if ($null -eq $Task) {
            Register-FixedRestartTask
        } elseif (-not (Test-FixedRestartTask -Task $Task)) {
            Register-FixedRestartTask
        } else {
            $ExpectedLimit = [Xml.XmlConvert]::ToString($RestartTaskLimit)
            if ($Task.Settings.ExecutionTimeLimit -ne $ExpectedLimit) {
                $Task.Settings.ExecutionTimeLimit = $ExpectedLimit
                Set-ScheduledTask -TaskName $TaskName -Settings $Task.Settings `
                    -ErrorAction Stop | Out-Null
            }
            Grant-FixedTaskReadAccess -Name $TaskName -UserId $Task.Principal.UserId
        }
    }
    "trigger" {
        $Task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        if ($null -eq $Task) {
            # A non-elevated/manual Core needs no privilege bridge. Keep the
            # exact same fixed controller and root boundary in that configuration.
            Write-Warning "Fixed restart task '$TaskName' is not registered; using same-privilege fixed restart."
            & powershell.exe `
                -NoProfile `
                -NonInteractive `
                -ExecutionPolicy Bypass `
                -File $RuntimeController `
                -Action restart `
                -Resume `
                -ApiGateway
            exit $LASTEXITCODE
        }
        if (-not (Test-FixedRestartTask -Task $Task)) {
            throw "Refusing mismatched fixed restart task '$TaskName'"
        }
        $RuntimeTask = Get-ScheduledTask -TaskName $RuntimeTaskName -ErrorAction SilentlyContinue
        if (-not (Test-FixedRuntimeTask -Task $RuntimeTask)) {
            throw "Refusing missing or mismatched fixed runtime task '$RuntimeTaskName'"
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
        $RuntimeTask = Get-ScheduledTask -TaskName $RuntimeTaskName -ErrorAction SilentlyContinue
        if ($null -ne $RuntimeTask -and [string]$RuntimeTask.State -ne "Running") {
            Unregister-ScheduledTask `
                -TaskName $RuntimeTaskName `
                -Confirm:$false `
                -ErrorAction SilentlyContinue
        }
    }
    "status" {
        $Task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        $RuntimeTask = Get-ScheduledTask -TaskName $RuntimeTaskName -ErrorAction SilentlyContinue
        [PSCustomObject]@{
            InstanceId = $SupervisorIdentity.instance_id
            TaskName = $TaskName
            Registered = $null -ne $Task
            DefinitionMatches = Test-FixedRestartTask -Task $Task
            RuntimeTaskName = $RuntimeTaskName
            RuntimeRegistered = $null -ne $RuntimeTask
            RuntimeDefinitionMatches = Test-FixedRuntimeTask -Task $RuntimeTask
            Runner = $TaskRunner
            HashiRoot = $HashiRoot
        } | Format-List
    }
}
