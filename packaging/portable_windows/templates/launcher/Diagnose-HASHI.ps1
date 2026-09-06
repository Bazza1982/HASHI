. (Join-Path $PSScriptRoot 'Common.ps1')

$failed = $false
Write-BilingualMessage `
    -English 'HASHI Portable system check' `
    -Chinese 'HASHI Portable 系统检查' `
    -ForegroundColor Cyan
Write-BilingualMessage `
    -English "Local installation: $script:PortableRoot" `
    -Chinese "本机安装位置：$script:PortableRoot" `
    -ForegroundColor Gray

$required = @(
    (Join-Path $script:PythonRoot 'python.exe'),
    (Join-Path $script:NodeRoot 'node.exe'),
    (Join-Path $script:HashiRoot 'main.py'),
    (Join-Path $script:WorkbenchRoot 'server.mjs'),
    (Join-Path $script:DataRoot 'agents.json'),
    (Join-Path $script:DataRoot 'secrets.json'),
    (Join-Path $script:DataRoot 'portable-instance.json'),
    (Join-Path $script:PythonRoot 'piper.exe'),
    (Join-Path $script:BinRoot 'ffmpeg.exe'),
    (Join-Path $script:HashiRoot 'voice_models\piper\zh_CN-huayan-medium.onnx'),
    (Join-Path $script:HashiRoot 'hashi_assets\ocr\bin\windows-x86_64\tesseract.exe'),
    (Join-Path $script:HashiRoot 'hashi_assets\ocr\tessdata_fast-87416418657359cb625c412a48b6e1d6d41c29bd\eng.traineddata'),
    $script:InstallMarkerPath
)
foreach ($path in $required) {
    if (Test-Path -LiteralPath $path -PathType Leaf) {
        Write-Host "[OK / 正常] $path" -ForegroundColor Green
    } else {
        Write-Host "[MISSING / 缺失] $path" -ForegroundColor Red
        $failed = $true
    }
}

try {
    New-Item -ItemType Directory -Force -Path (Join-Path $script:DataRoot 'tmp') | Out-Null
    $probe = Join-Path $script:DataRoot 'tmp\write-probe.tmp'
    Set-Content -LiteralPath $probe -Value 'ok' -Encoding ASCII
    Remove-Item -LiteralPath $probe -Force
    Write-BilingualMessage `
        -English '[OK] Local data directory is writable.' `
        -Chinese '[正常] 本机数据目录可以写入。' `
        -ForegroundColor Green
} catch {
    Write-BilingualMessage `
        -English '[FAILED] Local data directory is not writable.' `
        -Chinese '[失败] 本机数据目录无法写入。' `
        -ForegroundColor Red
    $failed = $true
}

try {
    Initialize-PortableEnvironment
    & (Join-Path $script:PythonRoot 'python.exe') -c "import aiohttp, cryptography, fastapi, PIL, playwright, psutil, pymupdf, telegram, textual, yaml, zeroconf" 2>$null
    if ($LASTEXITCODE -ne 0) { throw "Python exited with code $LASTEXITCODE." }
    Write-BilingualMessage `
        -English '[OK] Python runtime and required modules loaded successfully.' `
        -Chinese '[正常] Python 运行环境及必要模块已成功加载。' `
        -ForegroundColor Green
} catch {
    Write-BilingualMessage `
        -English "[FAILED] Python runtime check: $($_.Exception.Message)" `
        -Chinese "[失败] Python 运行环境检查：$($_.Exception.Message)" `
        -ForegroundColor Red
    $failed = $true
}

$endpoint = Get-VerifiedLocalEndpoint
if ($null -ne $endpoint) {
    Write-BilingualMessage `
        -English "[OK] HASHI is responding at 127.0.0.1:$([int]$endpoint.api_port)." `
        -Chinese "[正常] HASHI 正在 127.0.0.1:$([int]$endpoint.api_port) 正常响应。" `
        -ForegroundColor Green
} elseif (Test-Path -LiteralPath $script:EndpointPath -PathType Leaf) {
    Write-BilingualMessage `
        -English '[FAILED] The saved local endpoint is stale or does not belong to this installation.' `
        -Chinese '[失败] 已保存的本机端点已失效，或不属于此安装。' `
        -ForegroundColor Red
    $failed = $true
} else {
    Write-BilingualMessage `
        -English '[INFO] HASHI is not currently running.' `
        -Chinese '[信息] HASHI 当前未运行。' `
        -ForegroundColor Yellow
}

try {
    $drive = [System.IO.DriveInfo]::new('C:\')
    $freeMiB = [Math]::Round(([double]$drive.AvailableFreeSpace / 1MB), 1)
    Write-BilingualMessage `
        -English "Local free space: $freeMiB MB" `
        -Chinese "本机可用空间：$freeMiB MB" `
        -ForegroundColor Gray
} catch {
    Write-BilingualMessage `
        -English '[INFO] Local free-space reporting was unavailable.' `
        -Chinese '[信息] 无法读取本机可用空间。' `
        -ForegroundColor Yellow
}

if ($failed) {
    Write-BilingualMessage `
        -English 'One or more checks failed. Correct the items marked FAILED/MISSING, then retry the launcher.' `
        -Chinese '一项或多项检查失败。请修正标记为“失败/缺失”的项目，再重试启动。' `
        -ForegroundColor Red
    Write-BilingualMessage `
        -English "Logs: $script:DataRoot\logs" `
        -Chinese "日志位置：$script:DataRoot\logs" `
        -ForegroundColor Yellow
    exit 1
}
Write-BilingualMessage `
    -English 'All required checks passed. You can start HASHI normally.' `
    -Chinese '所有必要检查均已通过，您可以正常启动 HASHI。' `
    -ForegroundColor Green
exit 0
