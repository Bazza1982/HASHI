[CmdletBinding()]
param(
    [ValidateSet('Install', 'Start', 'Stop', 'Diagnose', 'Uninstall')]
    [string]$Action = 'Start',
    [ValidateSet('TUI')]
    [string]$Surface = 'TUI'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Quote-BootstrapArgument {
    param([string]$Value)
    return '"' + ($Value -replace '(\\*)"', '$1$1\"' -replace '(\\+)$', '$1$1') + '"'
}

try {
    $entry = Join-Path $PSScriptRoot 'Elevated-Entry.ps1'
    if (-not (Test-Path -LiteralPath $entry -PathType Leaf)) {
        throw 'Elevated entry point is missing.'
    }
    $desktopPath = [Environment]::GetFolderPath('Desktop')
    if (-not $desktopPath) { throw 'The current user desktop path is unavailable.' }
    $arguments = @(
        '-NoLogo',
        '-NoProfile',
        '-ExecutionPolicy',
        'Bypass',
        '-File',
        (Quote-BootstrapArgument $entry),
        '-Action',
        $Action,
        '-Surface',
        $Surface,
        '-DesktopPath',
        (Quote-BootstrapArgument $desktopPath)
    )
    Start-Process `
        -FilePath (Join-Path $PSHOME 'powershell.exe') `
        -Verb RunAs `
        -ArgumentList $arguments | Out-Null
} catch {
    try {
        $shell = New-Object -ComObject WScript.Shell
        [void]$shell.Popup(
            "HASHI operation failed.`nHASHI 操作失败。",
            0,
            'HASHI Portable',
            16
        )
    } catch {}
    exit 1
}
