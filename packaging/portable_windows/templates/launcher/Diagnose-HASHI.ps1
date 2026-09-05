. (Join-Path $PSScriptRoot 'Common.ps1')

$failed = $false
Write-Host 'HASHI Portable diagnostic' -ForegroundColor Cyan

$required = @(
    (Join-Path $script:PythonRoot 'python.exe'),
    (Join-Path $script:NodeRoot 'node.exe'),
    (Join-Path $script:HashiRoot 'main.py'),
    (Join-Path $script:WorkbenchRoot 'server.mjs'),
    (Join-Path $script:DataRoot 'agents.json'),
    (Join-Path $script:DataRoot 'secrets.json'),
    (Join-Path $script:PythonRoot 'piper.exe'),
    (Join-Path $script:BinRoot 'ffmpeg.exe'),
    (Join-Path $script:HashiRoot 'voice_models\piper\zh_CN-huayan-medium.onnx'),
    (Join-Path $script:HashiRoot 'hashi_assets\ocr\bin\windows-x86_64\tesseract.exe'),
    (Join-Path $script:HashiRoot 'hashi_assets\ocr\tessdata_fast-87416418657359cb625c412a48b6e1d6d41c29bd\eng.traineddata')
)
foreach ($path in $required) {
    if (Test-Path -LiteralPath $path) {
        Write-Host "[OK] $path" -ForegroundColor Green
    } else {
        Write-Host "[MISSING] $path" -ForegroundColor Red
        $failed = $true
    }
}

try {
    New-Item -ItemType Directory -Force -Path (Join-Path $script:DataRoot 'tmp') | Out-Null
    $probe = Join-Path $script:DataRoot 'tmp\write-probe.tmp'
    Set-Content -LiteralPath $probe -Value 'ok' -Encoding ASCII
    Remove-Item -LiteralPath $probe -Force
    Write-Host '[OK] USB data directory is writable.' -ForegroundColor Green
} catch {
    Write-Host '[FAILED] USB data directory is not writable.' -ForegroundColor Red
    $failed = $true
}

try {
    Initialize-PortableEnvironment
    & (Join-Path $script:PythonRoot 'python.exe') -c "import aiohttp, cryptography, fastapi, PIL, playwright, psutil, pymupdf, telegram, textual, yaml, zeroconf; print('[OK] Python runtime imports')"
    if ($LASTEXITCODE -ne 0) { $failed = $true }
} catch {
    Write-Host '[FAILED] Python runtime imports.' -ForegroundColor Red
    $failed = $true
}

$config = Get-PortableConfig
$health = Get-Health -Port ([int]$config.global.workbench_port)
if ($null -ne $health) {
    Write-Host "[OK] HASHI API is healthy ($($health.instance_id))." -ForegroundColor Green
} else {
    Write-Host '[INFO] HASHI is not currently running.' -ForegroundColor Yellow
}

$drive = Get-Item -LiteralPath $script:PortableRoot
Write-Host ("Free space: {0:N0} bytes" -f $drive.PSDrive.Free)
if ($failed) { exit 1 }
exit 0
