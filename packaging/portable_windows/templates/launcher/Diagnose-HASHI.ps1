. (Join-Path $PSScriptRoot 'Common.ps1')

$failed = $false
Write-BilingualMessage `
    -English 'HASHI Portable system check' `
    -Chinese 'HASHI Portable 系统检查' `
    -ForegroundColor Cyan
[void](Use-ExistingLocalCache)
if ($script:ExecutionMode -eq 'local-cache') {
    Write-BilingualMessage `
        -English "[OK] Verified local runtime: $script:AppRoot" `
        -Chinese "[正常] 已验证本机运行组件：$script:AppRoot" `
        -ForegroundColor Green
} elseif ($null -ne (Get-LocalCacheManifest)) {
    Write-BilingualMessage `
        -English '[ACTION] The local runtime is not installed for this USB build. Start TUI or Workbench to run setup.' `
        -Chinese '[建议操作] 此 USB 版本尚未安装本机运行组件。请启动 TUI 或 Workbench 进行安装。' `
        -ForegroundColor Yellow
} else {
    Write-BilingualMessage `
        -English '[INFO] This image has no local runtime installer and will run directly from the USB.' `
        -Chinese '[信息] 此版本不包含本机运行组件安装包，将直接从 USB 运行。' `
        -ForegroundColor Yellow
}

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
    (Join-Path $script:PortableRoot 'install\local-cache-manifest.json'),
    (Join-Path $script:PortableRoot 'install\local-cache-small-files.zip')
)
foreach ($path in $required) {
    if (Test-Path -LiteralPath $path) {
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
        -English '[OK] USB data directory is writable.' `
        -Chinese '[正常] USB 数据目录可以写入。' `
        -ForegroundColor Green
} catch {
    Write-BilingualMessage `
        -English '[FAILED] USB data directory is not writable.' `
        -Chinese '[失败] USB 数据目录无法写入。' `
        -ForegroundColor Red
    $failed = $true
}

try {
    Initialize-PortableEnvironment
    & (Join-Path $script:PythonRoot 'python.exe') -c "import aiohttp, cryptography, fastapi, PIL, playwright, psutil, pymupdf, telegram, textual, yaml, zeroconf" 2>$null
    if ($LASTEXITCODE -ne 0) {
        throw "Python exited with code $LASTEXITCODE."
    }
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

$config = Get-PortableConfig
$health = Get-Health -Port ([int]$config.global.workbench_port)
if ($null -ne $health) {
    Write-BilingualMessage `
        -English '[OK] HASHI is running and responding.' `
        -Chinese '[正常] HASHI 正在运行并能正常响应。' `
        -ForegroundColor Green
} else {
    Write-BilingualMessage `
        -English '[INFO] HASHI is not currently running.' `
        -Chinese '[信息] HASHI 当前未运行。' `
        -ForegroundColor Yellow
}

try {
    $driveRoot = [IO.Path]::GetPathRoot($script:PortableRoot)
    if ($driveRoot -match '^[A-Za-z]:\\$') {
        $drive = [IO.DriveInfo]::new($driveRoot)
        $freeMiB = [Math]::Round(([double]$drive.AvailableFreeSpace / 1MB), 1)
        Write-BilingualMessage `
            -English "USB free space: $freeMiB MB" `
            -Chinese "USB 可用空间：$freeMiB MB" `
            -ForegroundColor Gray
    } else {
        Write-BilingualMessage `
            -English '[INFO] Free-space reporting is unavailable for this test path.' `
            -Chinese '[信息] 当前测试路径无法报告可用空间。' `
            -ForegroundColor Yellow
    }
} catch {
    Write-BilingualMessage `
        -English '[INFO] USB free-space reporting was unavailable.' `
        -Chinese '[信息] 无法读取 USB 可用空间。' `
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
