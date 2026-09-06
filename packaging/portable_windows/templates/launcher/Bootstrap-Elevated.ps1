[CmdletBinding()]
param(
    [ValidateSet('TUI', 'Workbench')]
    [string]$Surface = 'TUI',
    [switch]$ForceSetup
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
    $arguments = @(
        '-NoLogo',
        '-NoProfile',
        '-ExecutionPolicy',
        'Bypass',
        '-File',
        (Quote-BootstrapArgument $entry),
        '-Surface',
        $Surface
    )
    if ($ForceSetup) { $arguments += '-ForceSetup' }
    Start-Process `
        -FilePath (Join-Path $PSHOME 'powershell.exe') `
        -Verb RunAs `
        -ArgumentList $arguments | Out-Null
} catch {
    try {
        $shell = New-Object -ComObject WScript.Shell
        [void]$shell.Popup(
            "HASHI startup failed.`nHASHI 启动失败。",
            0,
            'HASHI Portable',
            16
        )
    } catch {}
    exit 1
}
