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
    Start-Process -FilePath $browser -ArgumentList @(
        '--app=http://127.0.0.1:18888',
        ("--user-data-dir=" + (Quote-ProcessArgument $profile)),
        '--no-first-run',
        '--no-default-browser-check'
    ) | Out-Null
    Write-Host "Workbench is ready for $($health.instance_id)." -ForegroundColor Green
    exit 0
} catch {
    Write-Host $_.Exception.Message -ForegroundColor Red
    exit 1
}
