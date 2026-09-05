. (Join-Path $PSScriptRoot 'Common.ps1')

$failed = $false
Write-Host 'HASHI Portable diagnostic' -ForegroundColor Cyan
[void](Use-ExistingLocalCache)
if ($script:ExecutionMode -eq 'local-cache') {
    Write-Host "[OK] Verified local acceleration cache selected: $script:AppRoot" -ForegroundColor Green
} elseif ($null -ne (Get-LocalCacheManifest)) {
    Write-Host '[INFO] Local acceleration is not installed for this USB build; expanded USB fallback selected.' -ForegroundColor Yellow
} else {
    Write-Host '[INFO] This image has no local acceleration payload; expanded USB mode selected.' -ForegroundColor Yellow
}

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
    (Join-Path $script:HashiRoot 'hashi_assets\ocr\tessdata_fast-87416418657359cb625c412a48b6e1d6d41c29bd\eng.traineddata'),
    (Join-Path $script:PortableRoot 'install\local-cache-manifest.json'),
    (Join-Path $script:PortableRoot 'install\local-cache-small-files.zip')
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

try {
    $driveRoot = [IO.Path]::GetPathRoot($script:PortableRoot)
    if ($driveRoot -match '^[A-Za-z]:\\$') {
        $drive = [IO.DriveInfo]::new($driveRoot)
        Write-Host ("USB free space: {0:N0} bytes" -f $drive.AvailableFreeSpace)
    } else {
        Write-Host '[INFO] Free-space reporting is unavailable for this non-drive test path.' -ForegroundColor Yellow
    }
} catch {
    Write-Host '[INFO] USB free-space reporting was unavailable.' -ForegroundColor Yellow
}
if ($failed) { exit 1 }
exit 0
