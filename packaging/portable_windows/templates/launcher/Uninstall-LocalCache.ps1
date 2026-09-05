[CmdletBinding()]
param(
    [switch]$Quiet
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$script:ProductRoot = [IO.Path]::GetFullPath(
    (Join-Path ([Environment]::GetFolderPath('CommonApplicationData')) 'HASHI Portable')
)
$script:CacheRoot = Join-Path $script:ProductRoot 'Cache'
$script:StageRoot = Join-Path $script:ProductRoot 'Stage'
$script:UninstallRegistryPath = 'HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall\HASHIPortableLocalAcceleration'

function Test-IsAdministrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Quote-ProcessArgument {
    param([string]$Value)
    return '"' + ($Value -replace '(\\*)"', '$1$1\"' -replace '(\\+)$', '$1$1') + '"'
}

function Start-ElevatedUninstaller {
    $powerShell = Join-Path $PSHOME 'powershell.exe'
    $arguments = @(
        '-NoLogo',
        '-NoProfile',
        '-ExecutionPolicy',
        'Bypass',
        '-File',
        (Quote-ProcessArgument $PSCommandPath)
    )
    if ($Quiet) { $arguments += '-Quiet' }
    try {
        $process = Start-Process -FilePath $powerShell -Verb RunAs -ArgumentList $arguments -Wait -PassThru
        exit $process.ExitCode
    } catch {
        if (-not $Quiet) {
            Write-Host "Administrator approval was cancelled or failed: $($_.Exception.Message)" -ForegroundColor Yellow
        }
        exit 1
    }
}

if (-not (Test-IsAdministrator)) {
    Start-ElevatedUninstaller
}

try {
    $expectedRoot = [IO.Path]::GetFullPath(
        (Join-Path ([Environment]::GetFolderPath('CommonApplicationData')) 'HASHI Portable')
    )
    if (-not $script:ProductRoot.Equals($expectedRoot, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Refusing to uninstall an unexpected directory.'
    }

    if (-not $Quiet) {
        Write-Host 'Removing the HASHI program/runtime cache from this PC...' -ForegroundColor Cyan
        Write-Host 'USB data, API keys, conversations, configuration, and workspaces will not be touched.' -ForegroundColor Yellow
    }

    $cachePrefix = $script:CacheRoot.TrimEnd('\') + '\'
    Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | ForEach-Object {
        $executable = [string]$_.ExecutablePath
        if ($executable -and $executable.StartsWith($cachePrefix, [StringComparison]::OrdinalIgnoreCase)) {
            Stop-Process -Id ([int]$_.ProcessId) -Force -ErrorAction SilentlyContinue
        }
    }

    if (Test-Path -LiteralPath $script:CacheRoot) {
        Remove-Item -LiteralPath $script:CacheRoot -Recurse -Force
    }
    if (Test-Path -LiteralPath $script:StageRoot) {
        Remove-Item -LiteralPath $script:StageRoot -Recurse -Force
    }
    Remove-Item -LiteralPath $script:UninstallRegistryPath -Recurse -Force -ErrorAction SilentlyContinue

    if (-not $Quiet) {
        Write-Host 'The local acceleration cache has been removed.' -ForegroundColor Green
        Write-Host 'Your USB data is unchanged.' -ForegroundColor Green
    }

    # The installed uninstaller may be running from ProductRoot.  A short-lived
    # second PowerShell removes that final script and directory after this
    # process exits.
    if (Test-Path -LiteralPath $script:ProductRoot) {
        $escapedRoot = $script:ProductRoot.Replace("'", "''")
        $cleanup = @"
try { Wait-Process -Id $PID -Timeout 30 -ErrorAction SilentlyContinue } catch {}
Start-Sleep -Milliseconds 300
Remove-Item -LiteralPath '$escapedRoot' -Recurse -Force -ErrorAction SilentlyContinue
"@
        $encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($cleanup))
        Start-Process -FilePath (Join-Path $PSHOME 'powershell.exe') -WindowStyle Hidden -ArgumentList @(
            '-NoLogo', '-NoProfile', '-EncodedCommand', $encoded
        ) | Out-Null
    }
    exit 0
} catch {
    if (-not $Quiet) {
        Write-Host "HASHI local acceleration uninstall failed: $($_.Exception.Message)" -ForegroundColor Red
        Write-Host 'USB data was not targeted.' -ForegroundColor Yellow
    }
    exit 1
}
