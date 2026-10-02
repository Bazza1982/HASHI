Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

. (Join-Path $PSScriptRoot 'Common.ps1')

try {
    if (-not (Test-IsAdministrator)) {
        throw 'Run this local installation script as administrator.'
    }
    $python = Join-Path $script:PythonRoot 'python.exe'
    $setup = Join-Path $script:HashiRoot 'scripts\provision_privacy_runtime.py'
    $runtime = Join-Path $script:DataRoot 'state\runtimes\privacy'
    if (-not (Test-Path -LiteralPath $setup -PathType Leaf)) {
        throw 'The Level 2 privacy installer is missing from this HASHI bundle.'
    }
    & $python $setup --runtime-dir $runtime
    if ($LASTEXITCODE -ne 0) { throw 'The local detector installation failed.' }
    & $python $setup --runtime-dir $runtime --check
    if ($LASTEXITCODE -ne 0) { throw 'The local detector readiness check failed.' }
    Write-BilingualMessage `
        -English 'The Level 2 local privacy detector is ready. Restart HASHI normally before enabling /privacy 2.' `
        -Chinese 'Level 2 本地隐私检测器已就绪。正常重启 HASHI 后可启用 /privacy 2。' `
        -ForegroundColor Green
    exit 0
} catch {
    Write-BilingualMessage `
        -English "Level 2 remains unavailable: $($_.Exception.Message)" `
        -Chinese 'Level 2 尚不可用；敏感内容不会因此自动放行。' `
        -ForegroundColor Red
    exit 1
}
