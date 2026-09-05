. (Join-Path $PSScriptRoot 'Common.ps1')

try {
    $health = Start-HASHIBackend
    Write-BilingualMessage `
        -English 'HASHI is ready. Opening the terminal interface...' `
        -Chinese 'HASHI 已就绪。正在打开终端界面……' `
        -ForegroundColor Green
    & (Join-Path $script:PythonRoot 'python.exe') (Join-Path $script:HashiRoot 'tui.py')
    exit $LASTEXITCODE
} catch {
    Write-LauncherFailureHelp -EnglishAction "HASHI could not start: $($_.Exception.Message)"
    exit 1
}
