#Requires -Version 5.1
#Requires -RunAsAdministrator

[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'Medium')]
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$')]
    [string]$InstanceId,

    [Parameter(Mandatory = $true)]
    [string]$HashiRoot,

    [string]$PythonExecutable = '',

    [string]$ExpectedIdentity = '',

    [ValidatePattern('^$|^[A-Za-z0-9][A-Za-z0-9 ._-]{0,127}$')]
    [string]$TaskName = '',

    [ValidatePattern('^$|^[A-Za-z0-9_.-]{1,256}$')]
    [string]$LegacyServiceName = '',

    [ValidateRange(0, 86400)]
    [int]$LogonDelaySeconds = 20,

    [string]$RuntimeBase = '',

    [switch]$ApiGateway,

    [switch]$StartNow
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Assert-SingleLineValue {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$Value
    )

    if ([string]::IsNullOrWhiteSpace($Value) -or $Value -match '[\x00\r\n]') {
        throw "$Name must be a non-empty, single-line value."
    }
}

function ConvertTo-NativeArgument {
    param([Parameter(Mandatory = $true)][AllowEmptyString()][string]$Value)

    if ($Value.Length -eq 0) {
        return '""'
    }
    if ($Value -notmatch '[\s"]') {
        return $Value
    }
    return '"' + ($Value -replace '(\\*)"', '$1$1\"' -replace '(\\+)$', '$1$1') + '"'
}

if ($env:OS -ne 'Windows_NT') {
    throw 'The native Windows user-runtime task must be installed from Windows PowerShell.'
}

$currentIdentity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
if ([string]::IsNullOrWhiteSpace($ExpectedIdentity)) {
    $ExpectedIdentity = $currentIdentity
}
if ([string]::IsNullOrWhiteSpace($TaskName)) {
    $TaskName = "$InstanceId-User-Runtime"
}
if ([string]::IsNullOrWhiteSpace($LegacyServiceName)) {
    $LegacyServiceName = $InstanceId
}
if ([string]::IsNullOrWhiteSpace($RuntimeBase)) {
    $commonData = [Environment]::GetFolderPath(
        [Environment+SpecialFolder]::CommonApplicationData
    )
    $RuntimeBase = Join-Path $commonData 'HASHI'
}

Assert-SingleLineValue -Name 'ExpectedIdentity' -Value $ExpectedIdentity
Assert-SingleLineValue -Name 'TaskName' -Value $TaskName
Assert-SingleLineValue -Name 'HashiRoot' -Value $HashiRoot
Assert-SingleLineValue -Name 'LegacyServiceName' -Value $LegacyServiceName
Assert-SingleLineValue -Name 'RuntimeBase' -Value $RuntimeBase

$HashiRoot = [IO.Path]::GetFullPath($HashiRoot).TrimEnd('\')
$RuntimeBase = [IO.Path]::GetFullPath($RuntimeBase).TrimEnd('\')
if ([string]::IsNullOrWhiteSpace($PythonExecutable)) {
    $PythonExecutable = Join-Path $HashiRoot '.venv\Scripts\python.exe'
}
else {
    Assert-SingleLineValue -Name 'PythonExecutable' -Value $PythonExecutable
    $PythonExecutable = [IO.Path]::GetFullPath($PythonExecutable)
}

if (-not $currentIdentity.Equals(
    $ExpectedIdentity,
    [StringComparison]::OrdinalIgnoreCase
)) {
    throw "Run this installer as $ExpectedIdentity; actual identity is $currentIdentity."
}
if (-not (Test-Path -LiteralPath $HashiRoot -PathType Container)) {
    throw "HASHI checkout root is unavailable: $HashiRoot"
}
if (-not (Test-Path -LiteralPath $PythonExecutable -PathType Leaf)) {
    throw "Runtime Python is unavailable: $PythonExecutable"
}

$mainPath = Join-Path $HashiRoot 'main.py'
$configPath = Join-Path $HashiRoot 'agents.json'
if (-not (Test-Path -LiteralPath $mainPath -PathType Leaf)) {
    throw "HASHI main.py is unavailable: $mainPath"
}
if (-not (Test-Path -LiteralPath $configPath -PathType Leaf)) {
    throw "HASHI instance configuration is unavailable: $configPath"
}
$instanceConfig = Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json
$configuredInstanceId = [string]$instanceConfig.global.instance_id
if (-not $configuredInstanceId.Equals(
    $InstanceId,
    [StringComparison]::OrdinalIgnoreCase
)) {
    throw "Configured instance_id is $configuredInstanceId, not $InstanceId."
}

$sourceLauncher = Join-Path $PSScriptRoot 'start-native-hashi-user-runtime.ps1'
if (-not (Test-Path -LiteralPath $sourceLauncher -PathType Leaf)) {
    throw "The native Windows user-runtime launcher is missing: $sourceLauncher"
}

$existingTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($null -ne $existingTask -and [string]$existingTask.State -eq 'Running') {
    throw "Scheduled task $TaskName is running. Stop that exact instance before updating its deployment."
}

$sharedDirectory = Join-Path $RuntimeBase 'shared'
$installedLauncher = Join-Path $sharedDirectory 'start-native-hashi-user-runtime.ps1'
$powershellExecutable = Join-Path (
    [Environment]::GetFolderPath([Environment+SpecialFolder]::Windows)
) 'System32\WindowsPowerShell\v1.0\powershell.exe'
if (-not (Test-Path -LiteralPath $powershellExecutable -PathType Leaf)) {
    throw "Cannot find Windows PowerShell 5.1 at $powershellExecutable."
}

$launcherArguments = @(
    '-NoLogo',
    '-NoProfile',
    '-NonInteractive',
    '-ExecutionPolicy', 'Bypass',
    '-WindowStyle', 'Hidden',
    '-File', $installedLauncher,
    '-InstanceId', $InstanceId,
    '-ExpectedIdentity', $ExpectedIdentity,
    '-HashiRoot', $HashiRoot,
    '-PythonExecutable', $PythonExecutable,
    '-RuntimeBase', $RuntimeBase,
    '-LegacyServiceName', $LegacyServiceName
)
if ($ApiGateway) {
    $launcherArguments += '-ApiGateway'
}
$actionArguments = ($launcherArguments | ForEach-Object {
    ConvertTo-NativeArgument -Value ([string]$_)
}) -join ' '

$action = New-ScheduledTaskAction `
    -Execute $powershellExecutable `
    -Argument $actionArguments `
    -WorkingDirectory $HashiRoot
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $ExpectedIdentity
$trigger.Delay = "PT${LogonDelaySeconds}S"
$principal = New-ScheduledTaskPrincipal `
    -UserId $ExpectedIdentity `
    -LogonType Interactive `
    -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -DontStopOnIdleEnd `
    -StartWhenAvailable `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 10 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew
$task = New-ScheduledTask `
    -Action $action `
    -Trigger $trigger `
    -Principal $principal `
    -Settings $settings `
    -Description "Start native Windows $InstanceId for $ExpectedIdentity at interactive logon."

if (-not $PSCmdlet.ShouldProcess(
    $TaskName,
    "Install the shared native Windows launcher and register the $InstanceId logon task"
)) {
    return
}

New-Item -ItemType Directory -Path $sharedDirectory -Force | Out-Null
$candidateLauncher = "$installedLauncher.candidate-$([Guid]::NewGuid().ToString('N'))"
$backupLauncher = "$installedLauncher.previous"
$hadInstalledLauncher = Test-Path -LiteralPath $installedLauncher -PathType Leaf
$launcherAdopted = $false

try {
    Copy-Item -LiteralPath $sourceLauncher -Destination $candidateLauncher
    if ($hadInstalledLauncher) {
        Remove-Item `
            -LiteralPath $backupLauncher `
            -Force `
            -ErrorAction SilentlyContinue
        [IO.File]::Replace(
            $candidateLauncher,
            $installedLauncher,
            $backupLauncher,
            $true
        )
    }
    else {
        [IO.File]::Move($candidateLauncher, $installedLauncher)
    }
    $launcherAdopted = $true

    Register-ScheduledTask `
        -TaskName $TaskName `
        -InputObject $task `
        -Force | Out-Null
}
catch {
    if ($launcherAdopted) {
        if ($hadInstalledLauncher -and (
            Test-Path -LiteralPath $backupLauncher -PathType Leaf
        )) {
            [IO.File]::Replace(
                $backupLauncher,
                $installedLauncher,
                $null,
                $true
            )
        }
        elseif (-not $hadInstalledLauncher) {
            Remove-Item -LiteralPath $installedLauncher -Force -ErrorAction SilentlyContinue
        }
    }
    throw
}
finally {
    Remove-Item -LiteralPath $candidateLauncher -Force -ErrorAction SilentlyContinue
}

if ($StartNow) {
    Start-ScheduledTask -TaskName $TaskName
}

[PSCustomObject]@{
    instance_id = $InstanceId
    task_name = $TaskName
    identity = $ExpectedIdentity
    hashi_root = $HashiRoot
    python = $PythonExecutable
    launcher = $installedLauncher
    api_gateway = [bool]$ApiGateway
    started = [bool]$StartNow
}
