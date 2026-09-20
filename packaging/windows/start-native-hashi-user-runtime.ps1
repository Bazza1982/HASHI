#Requires -Version 5.1

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$')]
    [string]$InstanceId,

    [Parameter(Mandatory = $true)]
    [string]$ExpectedIdentity,

    [Parameter(Mandatory = $true)]
    [string]$HashiRoot,

    [string]$PythonExecutable = '',

    [string]$RuntimeBase = '',

    [ValidatePattern('^$|^[A-Za-z0-9_.-]{1,256}$')]
    [string]$LegacyServiceName = '',

    [switch]$ApiGateway
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
Assert-SingleLineValue -Name 'HashiRoot' -Value $HashiRoot

$HashiRoot = [IO.Path]::GetFullPath($HashiRoot).TrimEnd('\')
if ([string]::IsNullOrWhiteSpace($PythonExecutable)) {
    $PythonExecutable = Join-Path $HashiRoot '.venv\Scripts\python.exe'
}
else {
    Assert-SingleLineValue -Name 'PythonExecutable' -Value $PythonExecutable
    $PythonExecutable = [IO.Path]::GetFullPath($PythonExecutable)
}
if ([string]::IsNullOrWhiteSpace($RuntimeBase)) {
    $commonData = [Environment]::GetFolderPath(
        [Environment+SpecialFolder]::CommonApplicationData
    )
    $RuntimeBase = Join-Path $commonData 'HASHI'
}
else {
    Assert-SingleLineValue -Name 'RuntimeBase' -Value $RuntimeBase
    $RuntimeBase = [IO.Path]::GetFullPath($RuntimeBase)
}
if ([string]::IsNullOrWhiteSpace($LegacyServiceName)) {
    $LegacyServiceName = $InstanceId
}

$mainPath = Join-Path $HashiRoot 'main.py'
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

function Invoke-NativeRuntime {
    param([Parameter(Mandatory = $true)][string[]]$Arguments)

    # Windows PowerShell 5.1 turns native stderr redirected through its pipeline
    # into NativeCommandError. Redirect the OS handles directly and use only the
    # child process exit code as lifecycle authority.
    $argumentLine = ($Arguments | ForEach-Object {
        ConvertTo-NativeArgument -Value ([string]$_)
    }) -join ' '
    $process = Start-Process `
        -FilePath $PythonExecutable `
        -ArgumentList $argumentLine `
        -WorkingDirectory $HashiRoot `
        -WindowStyle Hidden `
        -RedirectStandardOutput $stdoutLogPath `
        -RedirectStandardError $stderrLogPath `
        -PassThru `
        -Wait
    $process.Refresh()
    return [PSCustomObject]@{
        ProcessId = [int]$process.Id
        ExitCode = [int]$process.ExitCode
    }
}

$launcherExitCode = 1
try {
    Rotate-LauncherLogIfNeeded
    Archive-PreviousStreamLog -Path $stdoutLogPath
    Archive-PreviousStreamLog -Path $stderrLogPath

    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    $isAdministrator = $principal.IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator
    )
    if (-not $identity.Name.Equals(
        $ExpectedIdentity,
        [StringComparison]::OrdinalIgnoreCase
    )) {
        throw "$InstanceId user runtime must run as $ExpectedIdentity; actual identity is $($identity.Name)."
    }
    if (-not $isAdministrator) {
        throw "$InstanceId user runtime requires the elevated administrator token."
    }
    if (-not (Test-Path -LiteralPath $HashiRoot -PathType Container)) {
        throw "$InstanceId checkout root is unavailable: $HashiRoot"
    }
    if (-not (Test-Path -LiteralPath $PythonExecutable -PathType Leaf)) {
        throw "$InstanceId runtime Python is unavailable: $PythonExecutable"
    }
    if (-not (Test-Path -LiteralPath $mainPath -PathType Leaf)) {
        throw "$InstanceId main entrypoint is unavailable: $mainPath"
    }

    $legacyService = Get-Service -Name $LegacyServiceName -ErrorAction SilentlyContinue
    if ($null -ne $legacyService -and $legacyService.Status -ne 'Stopped') {
        Write-LauncherLog (
            "Legacy $LegacyServiceName service is still $($legacyService.Status); " +
            'skipping duplicate startup.'
        )
        exit 0
    }

    $env:BRIDGE_CODE_ROOT = $HashiRoot
    $env:BRIDGE_HOME = $HashiRoot
    $env:PYTHONIOENCODING = 'utf-8'
    $env:PYTHONPATH = $HashiRoot
    $env:PYTHONUTF8 = '1'

    $mainArguments = @(
        $mainPath,
        '--bridge-home', $HashiRoot
    )
    if ($ApiGateway) {
        $mainArguments += '--api-gateway'
    }

    Write-LauncherLog "Starting $InstanceId as $($identity.Name) from native Windows checkout $HashiRoot."
    $result = Invoke-NativeRuntime -Arguments $mainArguments
    $launcherExitCode = [int]$result.ExitCode
    Write-LauncherLog (
        "$InstanceId launcher process $($result.ProcessId) exited with code $launcherExitCode."
    )
}
catch {
    Write-LauncherLog "$InstanceId launcher failed: $($_.Exception.Message)"
    $launcherExitCode = 1
}

exit $launcherExitCode
