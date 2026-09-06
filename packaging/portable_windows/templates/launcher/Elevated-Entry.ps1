[CmdletBinding()]
param(
    [ValidateSet('TUI', 'Workbench')]
    [string]$Surface = 'TUI',
    [switch]$ForceSetup
)

. (Join-Path $PSScriptRoot 'Common.ps1')

function Wait-ForLaunchKey {
    Write-Host ''
    Write-BilingualMessage `
        -English 'Press any key to launch HASHI.' `
        -Chinese '按任意键启动 HASHI。' `
        -ForegroundColor Green
    try {
        [void]$Host.UI.RawUI.ReadKey('NoEcho,IncludeKeyDown')
    } catch {
        [void](Read-Host)
    }
    Write-Host ''
}

function Wait-ForDismissKey {
    try {
        [void]$Host.UI.RawUI.ReadKey('NoEcho,IncludeKeyDown')
    } catch {
        [void](Read-Host)
    }
}

if (-not (Test-IsAdministrator)) {
    Write-BilingualMessage `
        -English 'HASHI startup failed.' `
        -Chinese 'HASHI 启动失败。' `
        -ForegroundColor Red
    exit 2
}

$cacheWasReady = Use-ExistingLocalCache
if ($ForceSetup -or -not $cacheWasReady) {
    $installer = Join-Path $PSScriptRoot 'Install-LocalCache.ps1'
    & (Join-Path $PSHOME 'powershell.exe') `
        -NoLogo `
        -NoProfile `
        -ExecutionPolicy Bypass `
        -File $installer `
        -PauseOnError
    if ($LASTEXITCODE -ne 0) {
        exit 1
    }
    if (-not (Use-ExistingLocalCache)) {
        Write-BilingualMessage `
            -English 'Setup failed.' `
            -Chinese '安装失败。' `
            -ForegroundColor Red
        exit 1
    }
    Wait-ForLaunchKey
}

$launcher = if ($Surface -eq 'Workbench') {
    Join-Path $PSScriptRoot 'Start-Workbench.ps1'
} else {
    Join-Path $PSScriptRoot 'Start-TUI.ps1'
}

& (Join-Path $PSHOME 'powershell.exe') `
    -NoLogo `
    -NoProfile `
    -ExecutionPolicy Bypass `
    -File $launcher `
    -FailureHandledByEntry
$launchExitCode = $LASTEXITCODE
if ($launchExitCode -ne 0) {
    Write-BilingualMessage `
        -English 'HASHI startup failed.' `
        -Chinese 'HASHI 启动失败。' `
        -ForegroundColor Red
    Wait-ForDismissKey
    exit 2
}
exit 0
