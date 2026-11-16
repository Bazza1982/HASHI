param(
    [Parameter(Position = 0)]
    [ValidateSet("ensure", "register", "precheck", "start", "trigger", "unregister", "status")]
    [string]$Action = "status",

    [string]$HashiRoot,
    [string]$CodeRoot,
    [string]$Python,
    [string]$TaskUserId,
    [string]$InstanceId,
    [string]$RuntimeDataRoot = $env:HASHI_DATA_ROOT,
    [string]$OperationId,
    [string]$ReceiptPath,
    [ValidateRange(1, 300)]
    [int]$TimeoutSeconds = 30
)

$ErrorActionPreference = "Stop"
if (-not $HashiRoot) {
    $HashiRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
}
$HashiRoot = ([System.IO.Path]::GetFullPath($HashiRoot)).TrimEnd('\')
if (-not $CodeRoot) { $CodeRoot = Join-Path $PSScriptRoot ".." }
$CodeRoot = ([System.IO.Path]::GetFullPath($CodeRoot)).TrimEnd('\')
if (-not $Python) {
    $VenvPython = Join-Path $CodeRoot ".venv\Scripts\python.exe"
    $Python = if (Test-Path -LiteralPath $VenvPython -PathType Leaf) {
        $VenvPython
    } else {
        "python"
    }
}

$IdentityScript = Join-Path $CodeRoot "remote\supervisor_identity.py"
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

$TaskRunner = Join-Path $CodeRoot "bin\hashi_restart_task_runner.ps1"
if (-not (Test-Path -LiteralPath $TaskRunner -PathType Leaf)) {
    throw "Missing fixed restart task runner: $TaskRunner"
}
$RuntimeController = Join-Path $CodeRoot "bin\bridge_ctl.ps1"
if (-not (Test-Path -LiteralPath $RuntimeController -PathType Leaf)) {
    throw "Missing fixed HASHI runtime controller: $RuntimeController"
}
$LifecycleDirectory = Join-Path $HashiRoot "state\instance\cold-restart"
$RequestPath = Join-Path $LifecycleDirectory "request.json"
$LogPath = Join-Path $HashiRoot "logs\hashi-restart-task.log"
$RestartTaskLimit = New-TimeSpan -Minutes 5
$RunnerArgs = @(
    "-NoProfile",
    "-NonInteractive",
    "-ExecutionPolicy", "Bypass",
    "-WindowStyle", "Hidden",
    "-File", "`"$TaskRunner`"",
    "-HashiRoot", "`"$HashiRoot`"",
    "-CodeRoot", "`"$CodeRoot`"",
    "-LogPath", "`"$LogPath`"",
    "-RuntimeTaskName", "`"$RuntimeTaskName`"",
    "-RequestPath", "`"$RequestPath`""
)
$RunnerArgumentString = $RunnerArgs -join " "
$RuntimeArgs = @(
    "-NoProfile",
    "-NonInteractive",
    "-ExecutionPolicy", "Bypass",
    "-WindowStyle", "Hidden",
    "-File", "`"$RuntimeController`"",
    "-BridgeHome", "`"$HashiRoot`"",
    "-Action", "start",
    "-Resume",
    "-ApiGateway",
    "-StartupRequestPath", ('"' + $RequestPath + '"')
)
if ($RuntimeDataRoot) {
    if (-not [IO.Path]::IsPathRooted($RuntimeDataRoot)) {
        throw "RuntimeDataRoot must be an absolute installation catalogue path."
    }
    $RuntimeDataRoot = [IO.Path]::GetFullPath($RuntimeDataRoot)
    $env:HASHI_DATA_ROOT = $RuntimeDataRoot
    $RuntimeArgs += @("-RuntimeDataRoot", ('"' + $RuntimeDataRoot + '"'))
}
$RuntimeArgumentString = $RuntimeArgs -join " "

function Resolve-RestartTaskUser {
    param([string]$Name)
    $Existing = Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue
    if ($null -ne $Existing -and $Existing.Principal.UserId) {
        return [string]$Existing.Principal.UserId
    }
    if ($TaskUserId) { return $TaskUserId }
    return [string][System.Security.Principal.WindowsIdentity]::GetCurrent().Name
}

function Resolve-RestartTaskLogonType {
    param([string]$Name)
    $Existing = Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue
    if ($null -ne $Existing -and $Existing.Principal.LogonType) {
        return [string]$Existing.Principal.LogonType
    }
    return "Interactive"
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

function Write-AtomicJson {
    param([string]$Path, $Value)
    $Parent = Split-Path -Parent $Path
    New-Item -ItemType Directory -Force -Path $Parent | Out-Null
    $Temporary = $Path + "." + [Guid]::NewGuid().ToString("N") + ".tmp"
    try {
        [IO.File]::WriteAllText(
            $Temporary,
            ($Value | ConvertTo-Json -Depth 8 -Compress),
            [Text.UTF8Encoding]::new($false)
        )
        Move-Item -LiteralPath $Temporary -Destination $Path -Force
    } finally {
        Remove-Item -LiteralPath $Temporary -Force -ErrorAction SilentlyContinue
    }
}

function Assert-FixedRestartConfiguration {
    $Task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($null -eq $Task) {
        throw "Missing fixed restart task '$TaskName'"
    }
    if (-not (Test-FixedRestartTask -Task $Task)) {
        throw "Incompatible fixed restart task '$TaskName'"
    }
    $RuntimeTask = Get-ScheduledTask -TaskName $RuntimeTaskName -ErrorAction SilentlyContinue
    if ($null -eq $RuntimeTask) {
        throw "Missing fixed runtime task '$RuntimeTaskName'"
    }
    if (-not (Test-FixedRuntimeTask -Task $RuntimeTask)) {
        throw "Incompatible fixed runtime task '$RuntimeTaskName'"
    }
    return @($Task, $RuntimeTask)
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

function Assert-FixedTaskHome {
    param($Task, [string]$Name)
    if ($null -eq $Task) { return }
    $Actions = @($Task.Actions)
    if ($Actions.Count -ne 1 -or -not ([string]$Actions[0].WorkingDirectory).TrimEnd('\').Equals(
        $HashiRoot, [System.StringComparison]::OrdinalIgnoreCase
    )) {
        throw "Refusing fixed task '$Name' belonging to another instance home"
    }
}

function Get-FixedTaskDacl {
    param([string]$Name)
    $Existing = Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue
    if ($null -eq $Existing) { return $null }
    Assert-FixedTaskHome -Task $Existing -Name $Name
    $Scheduler = New-Object -ComObject 'Schedule.Service'
    $Scheduler.Connect()
    return $Scheduler.GetFolder('\').GetTask($Name).GetSecurityDescriptor(4)
}

function Restore-FixedTaskDacl {
    param([string]$Name, [string]$Dacl)
    if (-not $Dacl) { return }
    $Scheduler = New-Object -ComObject 'Schedule.Service'
    $Scheduler.Connect()
    $Task = $Scheduler.GetFolder('\').GetTask($Name)
    $Task.SetSecurityDescriptor($Dacl, 16)
    $Expected = [Security.AccessControl.RawSecurityDescriptor]::new($Dacl)
    $Actual = [Security.AccessControl.RawSecurityDescriptor]::new($Task.GetSecurityDescriptor(4))
    if ($Expected.GetSddlForm([Security.AccessControl.AccessControlSections]::Access) -ne
        $Actual.GetSddlForm([Security.AccessControl.AccessControlSections]::Access)) {
        throw "Fixed task '$Name' access preservation could not be verified"
    }
}

function Register-FixedRuntimeTask {
    $PreviousDacl = Get-FixedTaskDacl -Name $RuntimeTaskName
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
        -UserId (Resolve-RestartTaskUser -Name $RuntimeTaskName) `
        -LogonType (Resolve-RestartTaskLogonType -Name $RuntimeTaskName) `
        -RunLevel Highest
    $Task = New-ScheduledTask -Action $TaskAction -Settings $Settings -Principal $Principal
    Register-ScheduledTask `
        -TaskName $RuntimeTaskName `
        -InputObject $Task `
        -Force `
        -ErrorAction Stop | Out-Null
    Restore-FixedTaskDacl -Name $RuntimeTaskName -Dacl $PreviousDacl
    Grant-FixedTaskReadAccess -Name $RuntimeTaskName -UserId $Principal.UserId
    Write-Host "Registered fixed HASHI runtime task '$RuntimeTaskName'"
}

function Register-FixedRestartTask {
    $PreviousDacl = Get-FixedTaskDacl -Name $TaskName
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
        -UserId (Resolve-RestartTaskUser -Name $TaskName) `
        -LogonType (Resolve-RestartTaskLogonType -Name $TaskName) `
        -RunLevel Highest
    $Task = New-ScheduledTask -Action $TaskAction -Settings $Settings -Principal $Principal
    Register-ScheduledTask `
        -TaskName $TaskName `
        -InputObject $Task `
        -Force `
        -ErrorAction Stop | Out-Null
    Restore-FixedTaskDacl -Name $TaskName -Dacl $PreviousDacl
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
    "precheck" {
        $FixedTasks = Assert-FixedRestartConfiguration
        [PSCustomObject]@{
            ok = $true
            stage = "task_definition_precheck"
            instance_id = [string]$SupervisorIdentity.instance_id
            restart_task = $TaskName
            runtime_task = $RuntimeTaskName
        } | ConvertTo-Json -Compress
    }
    "start" {
        $RuntimeTask = Get-ScheduledTask -TaskName $RuntimeTaskName -ErrorAction SilentlyContinue
        if ($null -eq $RuntimeTask) {
            & powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass `
                -File $RuntimeController -BridgeHome $HashiRoot -Action start -Resume -ApiGateway
            exit $LASTEXITCODE
        }
        if (-not (Test-FixedRuntimeTask -Task $RuntimeTask)) {
            throw "Refusing mismatched fixed runtime task '$RuntimeTaskName'"
        }
        Start-ScheduledTask -TaskName $RuntimeTaskName -ErrorAction Stop
        Write-Host "Triggered fixed HASHI runtime task '$RuntimeTaskName'"
    }
    "trigger" {
        $FixedTasks = Assert-FixedRestartConfiguration
        if (-not $OperationId) { $OperationId = [Guid]::NewGuid().ToString("N") }
        if ($OperationId -notmatch '^[a-fA-F0-9]{32}$') {
            throw "Invalid cold-restart operation id"
        }
        if (-not $ReceiptPath) {
            $ReceiptPath = Join-Path $LifecycleDirectory ("receipt-" + $OperationId.ToLowerInvariant() + ".json")
        }
        $ReceiptPath = [IO.Path]::GetFullPath($ReceiptPath)
        $HomePrefix = $HashiRoot.TrimEnd('\') + '\'
        if (-not $ReceiptPath.StartsWith($HomePrefix, [StringComparison]::OrdinalIgnoreCase)) {
            throw "Cold-restart receipt must remain inside the HASHI instance home"
        }
        New-Item -ItemType Directory -Force -Path $LifecycleDirectory | Out-Null
        Remove-Item -LiteralPath $ReceiptPath -Force -ErrorAction SilentlyContinue
        $Request = [ordered]@{
            schema = 1
            operation_id = $OperationId.ToLowerInvariant()
            instance_id = [string]$SupervisorIdentity.instance_id
            code_root = $CodeRoot
            hashi_root = $HashiRoot
            receipt_path = $ReceiptPath
            requested_at = [DateTimeOffset]::UtcNow.ToString("o")
            deadline_epoch_ms = [DateTimeOffset]::UtcNow.AddSeconds($TimeoutSeconds).ToUnixTimeMilliseconds()
        }
        Write-AtomicJson -Path $RequestPath -Value $Request
        Start-ScheduledTask -TaskName $TaskName -ErrorAction Stop
        $AcceptanceClock = [Diagnostics.Stopwatch]::StartNew()
        while ($AcceptanceClock.Elapsed.TotalSeconds -lt $TimeoutSeconds) {
            if (Test-Path -LiteralPath $ReceiptPath -PathType Leaf) {
                try {
                    $Receipt = Get-Content -LiteralPath $ReceiptPath -Raw | ConvertFrom-Json
                    if ([string]$Receipt.operation_id -eq $Request.operation_id) {
                        if (-not ([string]$SupervisorIdentity.instance_id).Equals(
                            [string]$Receipt.instance_id,
                            [StringComparison]::OrdinalIgnoreCase
                        )) {
                            throw "Cold restart launch receipt instance identity mismatch"
                        }
                        if ([string]$Receipt.state -eq "launch_accepted") {
                            $Receipt | ConvertTo-Json -Depth 8 -Compress
                            exit 0
                        }
                        if ([string]$Receipt.state -eq "failed") {
                            $FailureStage = [string]$Receipt.failure_stage
                            $FailureReason = [string]$Receipt.error
                            throw "Cold restart failed at $FailureStage`: $FailureReason"
                        }
                    }
                } catch {
                    if ($_.Exception.Message -like "Cold restart*") { throw }
                }
            }
            Start-Sleep -Milliseconds 100
        }
        throw "Cold restart task did not produce a launch acceptance receipt within $TimeoutSeconds seconds"
    }
    "unregister" {
        $Task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        $RuntimeTask = Get-ScheduledTask -TaskName $RuntimeTaskName -ErrorAction SilentlyContinue
        Assert-FixedTaskHome -Task $Task -Name $TaskName
        Assert-FixedTaskHome -Task $RuntimeTask -Name $RuntimeTaskName
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
            CodeRoot = $CodeRoot
        } | Format-List
    }
}
