. (Join-Path $PSScriptRoot 'Common.ps1')

try {
    $health = Start-HASHIBackend
    Write-Host "HASHI is ready: $($health.instance_id)" -ForegroundColor Green
    & (Join-Path $script:PythonRoot 'python.exe') (Join-Path $script:HashiRoot 'tui.py')
    exit $LASTEXITCODE
} catch {
    Write-Host $_.Exception.Message -ForegroundColor Red
    exit 1
}
