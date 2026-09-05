. (Join-Path $PSScriptRoot 'Common.ps1')

try {
    $health = Start-HASHIBackend
    Start-WorkbenchServer
    $browser = Find-SystemBrowser
    if (-not $browser) {
        throw 'Microsoft Edge or Google Chrome was not found on this PC.'
    }
    $profile = Join-Path $script:DataRoot 'browser-profile'
    New-Item -ItemType Directory -Force -Path $profile | Out-Null
    Write-BilingualMessage `
        -English 'HASHI is ready. Opening Workbench...' `
        -Chinese 'HASHI 已就绪。正在打开 Workbench……' `
        -ForegroundColor Green
    Start-Process -FilePath $browser -ArgumentList @(
        '--app=http://127.0.0.1:18888',
        ("--user-data-dir=" + (Quote-ProcessArgument $profile)),
        '--no-first-run',
        '--no-default-browser-check'
    ) | Out-Null
    Write-BilingualMessage `
        -English 'Workbench is ready.' `
        -Chinese 'Workbench 已就绪。' `
        -ForegroundColor Green
    exit 0
} catch {
    Write-LauncherFailureHelp -EnglishAction "Workbench could not start: $($_.Exception.Message)"
    exit 1
}
