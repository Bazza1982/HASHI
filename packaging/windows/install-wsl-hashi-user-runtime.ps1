#Requires -Version 5.1
#Requires -RunAsAdministrator

[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'Medium')]
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$')]
    [string]$InstanceId,

    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[A-Za-z0-9][A-Za-z0-9._ -]{0,127}$')]
    [string]$Distro,

    [Parameter(Mandatory = $true)]
    [ValidatePattern('^/')]
    [string]$LinuxRoot,

    [ValidatePattern('^$|^/')]
    [string]$LinuxPython = '',

    [string]$ExpectedIdentity = '',

    [ValidatePattern('^$|^[A-Za-z0-9][A-Za-z0-9 ._-]{0,127}$')]
    [string]$TaskName = '',

    [ValidateRange(0, 86400)]
    [int]$LogonDelaySeconds = 20,

    [string]$RuntimeBase = '',

    [string]$WslExecutable = '',

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

function Invoke-WslProbe {
    param(
        [Parameter(Mandatory = $true)][string]$Executable,
        [Parameter(Mandatory = $true)][string[]]$Arguments
    )

    $nativeExitCode = 1
    $savedErrorActionPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        & $Executable @Arguments *> $null
        if ($null -ne $LASTEXITCODE) {
            $nativeExitCode = [int]$LASTEXITCODE
        }
    }
    finally {
        $ErrorActionPreference = $savedErrorActionPreference
    }
    return $nativeExitCode
}

if ($env:OS -ne 'Windows_NT') {
    throw 'The WSL user-runtime task must be installed from Windows PowerShell.'
}

$currentIdentity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
if ([string]::IsNullOrWhiteSpace($ExpectedIdentity)) {
    $ExpectedIdentity = $currentIdentity
}
if ([string]::IsNullOrWhiteSpace($TaskName)) {
    $TaskName = "$InstanceId-User-Runtime"
}
if ([string]::IsNullOrWhiteSpace($LinuxPython)) {
    $LinuxPython = if ($LinuxRoot -eq '/') {
        '/.venv/bin/python3'
    } else {
        "$($LinuxRoot.TrimEnd('/'))/.venv/bin/python3"
    }
}
if ($LinuxRoot.Length -gt 1) {
    $LinuxRoot = $LinuxRoot.TrimEnd('/')
}
if ([string]::IsNullOrWhiteSpace($RuntimeBase)) {
    $commonData = [Environment]::GetFolderPath(
        [Environment+SpecialFolder]::CommonApplicationData
    )
    $RuntimeBase = Join-Path $commonData 'HASHI'
}
if ([string]::IsNullOrWhiteSpace($WslExecutable)) {
    $WslExecutable = Join-Path $env:WINDIR 'System32\wsl.exe'
}

Assert-SingleLineValue -Name 'ExpectedIdentity' -Value $ExpectedIdentity
Assert-SingleLineValue -Name 'TaskName' -Value $TaskName
Assert-SingleLineValue -Name 'LinuxRoot' -Value $LinuxRoot
Assert-SingleLineValue -Name 'LinuxPython' -Value $LinuxPython
Assert-SingleLineValue -Name 'RuntimeBase' -Value $RuntimeBase
Assert-SingleLineValue -Name 'WslExecutable' -Value $WslExecutable

if (-not $currentIdentity.Equals(
    $ExpectedIdentity,
    [StringComparison]::OrdinalIgnoreCase
)) {
    throw "Run this installer as $ExpectedIdentity; actual identity is $currentIdentity."
}
if (-not (Test-Path -LiteralPath $WslExecutable -PathType Leaf)) {
    throw "Cannot find wsl.exe at $WslExecutable."
}

$sourceLauncher = Join-Path $PSScriptRoot 'start-wsl-hashi-user-runtime.ps1'
if (-not (Test-Path -LiteralPath $sourceLauncher -PathType Leaf)) {
    throw "The WSL user-runtime launcher is missing: $sourceLauncher"
}

$pythonProbeArguments = @(
    '--distribution', $Distro,
    '--cd', $LinuxRoot,
    '--', '/usr/bin/test', '-x', $LinuxPython
)
if ((Invoke-WslProbe -Executable $WslExecutable -Arguments $pythonProbeArguments) -ne 0) {
    throw "Runtime Python is unavailable in ${Distro}: $LinuxPython"
}

$sourceProbeArguments = @(
    '--distribution', $Distro,
    '--cd', $LinuxRoot,
    '--', '/usr/bin/test', '-f', 'main.py'
)
if ((Invoke-WslProbe -Executable $WslExecutable -Arguments $sourceProbeArguments) -ne 0) {
    throw "HASHI main.py is unavailable in ${Distro}: $LinuxRoot"
}

$existingTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($null -ne $existingTask -and [string]$existingTask.State -eq 'Running') {
    throw "Scheduled task $TaskName is running. Stop that exact instance before updating its deployment."
}

$sharedDirectory = Join-Path $RuntimeBase 'shared'
$installedLauncher = Join-Path $sharedDirectory 'start-wsl-hashi-user-runtime.ps1'
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
    '-Distro', $Distro,
    '-LinuxRoot', $LinuxRoot,
    '-LinuxPython', $LinuxPython,
    '-RuntimeBase', $RuntimeBase,
    '-WslExecutable', $WslExecutable
)
$actionArguments = ($launcherArguments | ForEach-Object {
    ConvertTo-NativeArgument -Value ([string]$_)
}) -join ' '

$action = New-ScheduledTaskAction `
    -Execute $powershellExecutable `
    -Argument $actionArguments `
    -WorkingDirectory $sharedDirectory
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
    -MultipleInstances IgnoreNew
$task = New-ScheduledTask `
    -Action $action `
    -Trigger $trigger `
    -Principal $principal `
    -Settings $settings `
    -Description "Start $InstanceId in WSL for $ExpectedIdentity at interactive logon."

if (-not $PSCmdlet.ShouldProcess(
    $TaskName,
    "Install the shared WSL launcher and register the $InstanceId logon task"
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
    distro = $Distro
    linux_root = $LinuxRoot
    linux_python = $LinuxPython
    launcher = $installedLauncher
    started = [bool]$StartNow
}
