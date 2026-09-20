#Requires -Version 5.1

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$')]
    [string]$InstanceId,

    [Parameter(Mandatory = $true)]
    [string]$ExpectedIdentity,

    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[A-Za-z0-9][A-Za-z0-9._ -]{0,127}$')]
    [string]$Distro,

    [Parameter(Mandatory = $true)]
    [ValidatePattern('^/')]
    [string]$LinuxRoot,

    [Parameter(Mandatory = $true)]
    [ValidatePattern('^/')]
    [string]$LinuxPython,

    [string]$RuntimeBase = '',

    [string]$WslExecutable = ''
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

Assert-SingleLineValue -Name 'ExpectedIdentity' -Value $ExpectedIdentity
Assert-SingleLineValue -Name 'LinuxRoot' -Value $LinuxRoot
Assert-SingleLineValue -Name 'LinuxPython' -Value $LinuxPython

if ([string]::IsNullOrWhiteSpace($RuntimeBase)) {
    $commonData = [Environment]::GetFolderPath(
        [Environment+SpecialFolder]::CommonApplicationData
    )
    $RuntimeBase = Join-Path $commonData 'HASHI'
}
if ([string]::IsNullOrWhiteSpace($WslExecutable)) {
    $WslExecutable = Join-Path $env:WINDIR 'System32\wsl.exe'
}

$runtimeDirectory = Join-Path $RuntimeBase $InstanceId
$logDirectory = Join-Path $runtimeDirectory 'logs'
$launcherLogPath = Join-Path $logDirectory 'user-runtime.log'
$stdoutLogPath = Join-Path $logDirectory 'user-runtime.stdout.log'
$stderrLogPath = Join-Path $logDirectory 'user-runtime.stderr.log'
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)

New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null

function Get-RotatedLogPath {
    param([Parameter(Mandatory = $true)][string]$Path)

    $parent = Split-Path -Parent $Path
    $stem = [IO.Path]::GetFileNameWithoutExtension($Path)
    $extension = [IO.Path]::GetExtension($Path)
    $stamp = [DateTimeOffset]::Now.ToString('yyyyMMdd-HHmmss')
    $suffix = [Guid]::NewGuid().ToString('N').Substring(0, 8)
    return Join-Path $parent ("{0}-{1}-{2}{3}" -f $stem, $stamp, $suffix, $extension)
}

function Test-LogContainsNullByte {
    param([Parameter(Mandatory = $true)][string]$Path)

    $stream = [IO.File]::Open(
        $Path,
        [IO.FileMode]::Open,
        [IO.FileAccess]::Read,
        [IO.FileShare]::ReadWrite
    )
    try {
        $buffer = New-Object byte[] 4096
        while (($count = $stream.Read($buffer, 0, $buffer.Length)) -gt 0) {
            for ($index = 0; $index -lt $count; $index++) {
                if ($buffer[$index] -eq 0) {
                    return $true
                }
            }
        }
    }
    finally {
        $stream.Dispose()
    }
    return $false
}

function Rotate-LauncherLogIfNeeded {
    if (-not (Test-Path -LiteralPath $launcherLogPath -PathType Leaf)) {
        return
    }

    $logItem = Get-Item -LiteralPath $launcherLogPath
    $containsLegacyEncoding = (
        $logItem.Length -gt 0 -and
        (Test-LogContainsNullByte -Path $launcherLogPath)
    )
    if ($logItem.Length -ge 10MB -or $containsLegacyEncoding) {
        Move-Item -LiteralPath $launcherLogPath -Destination (
            Get-RotatedLogPath -Path $launcherLogPath
        )
    }
}

function Archive-PreviousStreamLog {
    param([Parameter(Mandatory = $true)][string]$Path)

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return
    }

    $logItem = Get-Item -LiteralPath $Path
    if ($logItem.Length -eq 0) {
        Remove-Item -LiteralPath $Path -Force
        return
    }
    Move-Item -LiteralPath $Path -Destination (Get-RotatedLogPath -Path $Path)
}

function Write-LauncherLog {
    param([Parameter(Mandatory = $true)][string]$Message)

    $timestamp = [DateTimeOffset]::Now.ToString('o')
    $singleLine = $Message -replace '[\r\n]+', ' '
    [IO.File]::AppendAllText(
        $launcherLogPath,
        "$timestamp $singleLine$([Environment]::NewLine)",
        $utf8NoBom
    )
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

function Invoke-WslNative {
    param([Parameter(Mandatory = $true)][string[]]$Arguments)

    # Direct stream redirection keeps Windows PowerShell 5.1 from promoting
    # legitimate native stderr into NativeCommandError records. Start-Process
    # receives one correctly quoted Windows command line; wsl.exe's exit code
    # remains the only success authority.
    $argumentLine = ($Arguments | ForEach-Object {
        ConvertTo-NativeArgument -Value ([string]$_)
    }) -join ' '
    $process = Start-Process `
        -FilePath $WslExecutable `
        -ArgumentList $argumentLine `
        -WindowStyle Hidden `
        -RedirectStandardOutput $stdoutLogPath `
        -RedirectStandardError $stderrLogPath `
        -PassThru `
        -Wait
    $process.Refresh()
    return [int]$process.ExitCode
}

$launcherExitCode = 1
try {
    Rotate-LauncherLogIfNeeded
    Archive-PreviousStreamLog -Path $stdoutLogPath
    Archive-PreviousStreamLog -Path $stderrLogPath

    $identity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
    if (-not $identity.Equals(
        $ExpectedIdentity,
        [StringComparison]::OrdinalIgnoreCase
    )) {
        throw "$InstanceId user runtime must run as $ExpectedIdentity; actual identity is $identity."
    }
    if (-not (Test-Path -LiteralPath $WslExecutable -PathType Leaf)) {
        throw "$InstanceId user runtime cannot find wsl.exe at $WslExecutable."
    }

    $runtimeCheckArguments = @(
        '--distribution', $Distro,
        '--cd', $LinuxRoot,
        '--', '/usr/bin/test', '-x', $LinuxPython
    )
    $runtimeCheckExitCode = Invoke-WslNative -Arguments $runtimeCheckArguments
    if ($runtimeCheckExitCode -ne 0) {
        throw "$InstanceId runtime Python is unavailable in ${Distro}: $LinuxPython (exit $runtimeCheckExitCode)."
    }

    $mainArguments = @(
        '--distribution', $Distro,
        '--cd', $LinuxRoot,
        '--', $LinuxPython,
        'main.py',
        '--bridge-home', $LinuxRoot
    )

    Write-LauncherLog "Starting $InstanceId from WSL distribution $Distro as $identity."
    $launcherExitCode = Invoke-WslNative -Arguments $mainArguments
    Write-LauncherLog "$InstanceId launcher exited with code $launcherExitCode."
}
catch {
    Write-LauncherLog "$InstanceId launcher failed: $($_.Exception.Message)"
    $launcherExitCode = 1
}

exit $launcherExitCode
