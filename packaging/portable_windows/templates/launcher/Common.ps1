Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

try {
    $utf8 = New-Object System.Text.UTF8Encoding($false)
    [Console]::InputEncoding = $utf8
    [Console]::OutputEncoding = $utf8
    $global:OutputEncoding = $utf8
    $Host.UI.RawUI.WindowTitle = 'HASHI Portable'
} catch {}

$script:PortableRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$script:UsbAppRoot = Join-Path $script:PortableRoot 'app'
$script:UsbPythonRoot = Join-Path $script:PortableRoot 'runtime\python'
$script:UsbNodeRoot = Join-Path $script:PortableRoot 'runtime\node'
$script:UsbBinRoot = Join-Path $script:PortableRoot 'runtime\bin'
$script:AppRoot = $script:UsbAppRoot
$script:HashiRoot = Join-Path $script:AppRoot 'hashi'
$script:WorkbenchRoot = Join-Path $script:AppRoot 'workbench'
$script:DataRoot = Join-Path $script:PortableRoot 'data'
$script:PortableIdentityPath = Join-Path $script:DataRoot 'portable-instance.json'
$script:PythonRoot = $script:UsbPythonRoot
$script:NodeRoot = $script:UsbNodeRoot
$script:BinRoot = $script:UsbBinRoot
$script:ExecutionMode = 'usb'
$script:LocalCacheManifestPath = Join-Path $script:PortableRoot 'install\local-cache-manifest.json'
$script:LocalProductRoot = Join-Path ([Environment]::GetFolderPath('CommonApplicationData')) 'HASHI Portable'
$script:PortableInstanceId = ''
$script:LocalInstanceRoot = ''
$script:LocalCacheRoot = ''
$script:LocalCacheInstallAttempted = $false
$script:LauncherStateRoot = Join-Path $script:DataRoot 'state\launcher'
$script:HashiPidPath = Join-Path $script:LauncherStateRoot 'hashi.pid'
$script:WorkbenchPidPath = Join-Path $script:LauncherStateRoot 'workbench.pid'
$script:HashiStartupTimeoutSeconds = 1800
$script:WorkbenchStartupTimeoutSeconds = 300

function Initialize-PortableInstanceIdentity {
    if (-not (Test-Path -LiteralPath $script:PortableIdentityPath -PathType Leaf)) {
        throw "Portable instance identity is missing: $script:PortableIdentityPath"
    }
    try {
        $identity = Get-Content -LiteralPath $script:PortableIdentityPath -Raw -Encoding UTF8 | ConvertFrom-Json
    } catch {
        throw "Portable instance identity is unreadable: $($_.Exception.Message)"
    }
    $instanceId = [string]$identity.portable_instance_id
    if (
        [int]$identity.schema_version -ne 1 -or
        [string]$identity.product -ne 'HASHI Portable Windows x64' -or
        $instanceId -notmatch '^[0-9a-f]{32}$'
    ) {
        throw 'Portable instance identity is invalid.'
    }
    $script:PortableInstanceId = $instanceId
    $script:LocalInstanceRoot = Join-Path $script:LocalProductRoot ("Instances\$instanceId")
    $script:LocalCacheRoot = Join-Path $script:LocalInstanceRoot 'Cache'
}

Initialize-PortableInstanceIdentity

function Write-BilingualMessage {
    param(
        [string]$English,
        [string]$Chinese,
        [ConsoleColor]$ForegroundColor = [ConsoleColor]::Gray
    )
    Write-Host $English -ForegroundColor $ForegroundColor
    Write-Host $Chinese -ForegroundColor $ForegroundColor
}

function Test-IsAdministrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator
    )
}

function Read-SetupRetryChoice {
    while ($true) {
        try {
            $choice = (Read-Host '[R] Retry / 重试    [X] Exit / 退出').Trim()
        } catch {
            return $false
        }
        if ($choice -match '^(?i:r|retry)$' -or $choice -eq '重试') { return $true }
        if ($choice -match '^(?i:x|exit)$' -or $choice -eq '退出') { return $false }
        Write-BilingualMessage `
            -English 'Please enter R to retry or X to exit.' `
            -Chinese '请输入 R 重试，或输入 X 退出。' `
            -ForegroundColor Yellow
    }
}

function Write-LauncherFailureHelp {
    param([string]$EnglishAction = 'HASHI could not start.')
    Write-BilingualMessage `
        -English $EnglishAction `
        -Chinese 'HASHI 无法启动。' `
        -ForegroundColor Red
    Write-BilingualMessage `
        -English "Logs: $script:DataRoot\logs" `
        -Chinese "日志位置：$script:DataRoot\logs" `
        -ForegroundColor Yellow
    Write-BilingualMessage `
        -English 'Run Diagnose_HASHI.bat for a guided system check.' `
        -Chinese '请运行 Diagnose_HASHI.bat 进行引导式系统检查。' `
        -ForegroundColor Yellow
}

function Get-PortableConfig {
    return Get-Content -LiteralPath (Join-Path $script:DataRoot 'agents.json') -Raw -Encoding UTF8 | ConvertFrom-Json
}

function Get-PortableSecrets {
    return Get-Content -LiteralPath (Join-Path $script:DataRoot 'secrets.json') -Raw -Encoding UTF8 | ConvertFrom-Json
}

function Set-PortableExecutionRoots {
    param(
        [string]$Root,
        [string]$Mode
    )
    $script:AppRoot = Join-Path $Root 'app'
    $script:HashiRoot = Join-Path $script:AppRoot 'hashi'
    $script:WorkbenchRoot = Join-Path $script:AppRoot 'workbench'
    $script:PythonRoot = Join-Path $Root 'runtime\python'
    $script:NodeRoot = Join-Path $Root 'runtime\node'
    $script:BinRoot = Join-Path $Root 'runtime\bin'
    $script:ExecutionMode = $Mode
}

function Use-UsbExecutionRoots {
    Set-PortableExecutionRoots -Root $script:PortableRoot -Mode 'usb'
}

function Get-LocalCacheManifest {
    if (-not (Test-Path -LiteralPath $script:LocalCacheManifestPath -PathType Leaf)) {
        return $null
    }
    try {
        $manifest = Get-Content -LiteralPath $script:LocalCacheManifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
        $bundleId = [string]$manifest.bundle_id
        $cacheKey = [string]$manifest.cache_key
        if ($bundleId -notmatch '^[0-9a-f]{64}$' -or $cacheKey -ne $bundleId.Substring(0, 20)) {
            return $null
        }
        return $manifest
    } catch {
        return $null
    }
}

function Get-LocalCacheCandidateRoot {
    param([object]$Manifest)
    if ($null -eq $Manifest) { return $null }
    return Join-Path $script:LocalCacheRoot ([string]$Manifest.cache_key)
}

function Test-LocalCacheReady {
    param(
        [string]$Root,
        [object]$Manifest
    )
    if (-not $Root -or $null -eq $Manifest) { return $false }
    $markerPath = Join-Path $Root '.hashi-local-cache.json'
    if (-not (Test-Path -LiteralPath $markerPath -PathType Leaf)) { return $false }
    try {
        $marker = Get-Content -LiteralPath $markerPath -Raw -Encoding UTF8 | ConvertFrom-Json
        if ([string]$marker.portable_instance_id -ne $script:PortableInstanceId) { return $false }
        if ([string]$marker.bundle_id -ne [string]$Manifest.bundle_id) { return $false }
        foreach ($relativeValue in @($Manifest.required_files)) {
            $relative = ([string]$relativeValue).Replace('/', '\')
            if ([IO.Path]::IsPathRooted($relative) -or $relative.Contains('..')) { return $false }
            if (-not (Test-Path -LiteralPath (Join-Path $Root $relative) -PathType Leaf)) {
                return $false
            }
        }
        return $true
    } catch {
        return $false
    }
}

function Use-ExistingLocalCache {
    $manifest = Get-LocalCacheManifest
    if ($null -eq $manifest) {
        Use-UsbExecutionRoots
        return $false
    }
    $candidate = Get-LocalCacheCandidateRoot -Manifest $manifest
    if (-not (Test-LocalCacheReady -Root $candidate -Manifest $manifest)) {
        Use-UsbExecutionRoots
        return $false
    }
    Set-PortableExecutionRoots -Root $candidate -Mode 'local-cache'
    return $true
}

function Ensure-LocalAccelerationCache {
    if (Use-ExistingLocalCache) { return $true }
    if ([string]$env:HASHI_PORTABLE_SKIP_LOCAL_CACHE -eq '1') {
        Write-BilingualMessage `
            -English 'Local runtime installation was explicitly skipped; HASHI will run from the USB drive.' `
            -Chinese '已明确跳过本机运行组件安装；HASHI 将直接从 USB 运行。' `
            -ForegroundColor Yellow
        return $false
    }
    if ($script:LocalCacheInstallAttempted) { return $false }
    $script:LocalCacheInstallAttempted = $true
    if ($null -eq (Get-LocalCacheManifest)) {
        Write-BilingualMessage `
            -English 'This USB has no local runtime installer; HASHI will run from the USB drive.' `
            -Chinese '此 USB 不包含本机运行组件安装包；HASHI 将直接从 USB 运行。' `
            -ForegroundColor Yellow
        return $false
    }

    $installer = Join-Path $PSScriptRoot 'Install-LocalCache.ps1'
    if (-not (Test-Path -LiteralPath $installer -PathType Leaf)) {
        Write-BilingualMessage `
            -English 'The local runtime installer is missing; HASHI will run from the USB drive.' `
            -Chinese '本机运行组件安装程序缺失；HASHI 将直接从 USB 运行。' `
            -ForegroundColor Yellow
        return $false
    }

    $isUpdate = $false
    if (Test-Path -LiteralPath $script:LocalCacheRoot -PathType Container) {
        $isUpdate = $null -ne (Get-ChildItem -LiteralPath $script:LocalCacheRoot -Directory -Force -ErrorAction SilentlyContinue | Select-Object -First 1)
    }
    if ($isUpdate) {
        Write-BilingualMessage `
            -English 'Updating HASHI runtime' `
            -Chinese '正在更新 HASHI 运行组件' `
            -ForegroundColor Cyan
        Write-BilingualMessage `
            -English 'HASHI needs to update its local runtime files on this PC.' `
            -Chinese 'HASHI 需要更新这台电脑上的本机运行组件。' `
            -ForegroundColor Gray
    } else {
        Write-BilingualMessage `
            -English 'Preparing HASHI for first use' `
            -Chinese '正在为首次使用准备 HASHI' `
            -ForegroundColor Cyan
        Write-BilingualMessage `
            -English 'HASHI needs to install local runtime files on this PC.' `
            -Chinese 'HASHI 需要在这台电脑上安装本机运行组件。' `
            -ForegroundColor Gray
    }
    Write-BilingualMessage `
        -English 'Your conversations, settings, and other personal data will remain on the USB drive.' `
        -Chinese '您的对话、设置和其他个人数据仍会保留在 USB 中。' `
        -ForegroundColor Green
    Write-BilingualMessage `
        -English 'Administrator permission is active. Keep the USB drive connected until setup is complete.' `
        -Chinese '管理员权限已生效。安装完成前请勿拔出 USB。' `
        -ForegroundColor Yellow

    $powerShell = Join-Path $PSHOME 'powershell.exe'
    $arguments = @(
        '-NoLogo',
        '-NoProfile',
        '-ExecutionPolicy',
        'Bypass',
        '-File',
        $installer
    )
    while ($true) {
        $failureDetail = $null
        try {
            if (-not (Test-IsAdministrator)) {
                throw 'Administrator privileges are required before setup can start.'
            }
            & $powerShell @arguments
            $installerExitCode = $LASTEXITCODE
            if ($installerExitCode -eq 0 -and (Use-ExistingLocalCache)) {
                Write-BilingualMessage `
                    -English 'Setup complete.' `
                    -Chinese '安装完成。' `
                    -ForegroundColor Green
                return $true
            }
            if ($installerExitCode -eq 0) {
                $failureDetail = 'The installed runtime did not pass its readiness check.'
            } else {
                $failureDetail = "Setup exited with code $installerExitCode."
            }
        } catch {
            $failureDetail = $_.Exception.Message
        }

        Use-UsbExecutionRoots
        Write-BilingualMessage `
            -English "Setup did not complete: $failureDetail" `
            -Chinese "安装未完成：$failureDetail" `
            -ForegroundColor Red
        Write-BilingualMessage `
            -English "Setup log: $script:DataRoot\logs\hashi-setup.log" `
            -Chinese "安装日志：$script:DataRoot\logs\hashi-setup.log" `
            -ForegroundColor Yellow
        Write-BilingualMessage `
            -English 'Correct the reported problem, then retry. HASHI will not start from an incomplete installation.' `
            -Chinese '请修正上述问题后重试。安装未完成时，HASHI 不会启动。' `
            -ForegroundColor Yellow
        if (-not (Read-SetupRetryChoice)) {
            throw 'Setup was not completed. HASHI was not started.'
        }
    }
}

function Initialize-PortableEnvironment {
    New-Item -ItemType Directory -Force -Path $script:LauncherStateRoot | Out-Null
    New-Item -ItemType Directory -Force -Path (Join-Path $script:DataRoot 'logs') | Out-Null
    New-Item -ItemType Directory -Force -Path (Join-Path $script:DataRoot 'tmp') | Out-Null

    $env:BRIDGE_HOME = $script:DataRoot
    $env:HASHI_REMOTE_ROOT = $script:DataRoot
    $env:HASHI_REMOTE_CONTROL_ROOT = $script:HashiRoot
    $env:HASHI_REMOTE_STATE_DIR = Join-Path $script:DataRoot 'state\remote'
    $remoteLiveEndpointsPath = Join-Path $script:DataRoot 'state\remote_live_endpoints.json'
    if ($script:ExecutionMode -eq 'local-cache') {
        try {
            $remoteDerivedStateRoot = Join-Path $script:LocalInstanceRoot 'State'
            New-Item -ItemType Directory -Force -Path $remoteDerivedStateRoot -ErrorAction Stop | Out-Null
            $remoteLiveEndpointsPath = Join-Path $remoteDerivedStateRoot 'remote_live_endpoints.json'
        } catch {
            $remoteLiveEndpointsPath = Join-Path $script:DataRoot 'state\remote_live_endpoints.json'
        }
    }
    $env:HASHI_REMOTE_LIVE_ENDPOINTS_PATH = $remoteLiveEndpointsPath
    $env:HASHI_PORTABLE_USB_ROOT = $script:PortableRoot
    $env:HASHI_PORTABLE_EXECUTION_MODE = $script:ExecutionMode
    $env:HASHI_PORTABLE_STORAGE_PROFILE = 'removable'
    $env:HASHI_TUI_ENABLE_API_GATEWAY = '0'
    $env:HASHI_TUI_ATTACH_ONLY = '1'
    $env:PYTHONUTF8 = '1'
    $env:PYTHONIOENCODING = 'utf-8'
    $env:PYTHONNOUSERSITE = '1'
    $env:PYTHONDONTWRITEBYTECODE = '1'
    $env:HASHI_TESSERACT_BINARY = Join-Path $script:HashiRoot 'hashi_assets\ocr\bin\windows-x86_64\tesseract.exe'
    $env:HASHI_OCR_MODEL_ROOT = Join-Path $script:HashiRoot 'hashi_assets\ocr\tessdata_fast-87416418657359cb625c412a48b6e1d6d41c29bd'
    $env:TESSDATA_PREFIX = $env:HASHI_OCR_MODEL_ROOT
    $env:TEMP = Join-Path $script:DataRoot 'tmp'
    $env:TMP = $env:TEMP
    $env:PATH = "$script:PythonRoot;$script:BinRoot;$env:SystemRoot\System32;$env:SystemRoot"
    $hostRoots = @(
        Get-PSDrive -PSProvider FileSystem |
            ForEach-Object { $_.Root } |
            Where-Object { $_ -and (Test-Path -LiteralPath $_ -PathType Container) } |
            Sort-Object -Unique
    )
    $env:HASHI_ADDITIONAL_ACCESS_ROOTS = [string]::Join(
        [System.IO.Path]::PathSeparator,
        $hostRoots
    )
}

function Read-LogTextSince {
    param(
        [string]$Path,
        [long]$Offset
    )
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return '' }
    $stream = $null
    $reader = $null
    try {
        $stream = [System.IO.File]::Open(
            $Path,
            [System.IO.FileMode]::Open,
            [System.IO.FileAccess]::Read,
            [System.IO.FileShare]::ReadWrite
        )
        if ($Offset -lt 0 -or $Offset -gt $stream.Length) { $Offset = 0 }
        [void]$stream.Seek($Offset, [System.IO.SeekOrigin]::Begin)
        $reader = New-Object System.IO.StreamReader($stream, [Text.Encoding]::UTF8, $true)
        return $reader.ReadToEnd()
    } catch {
        return ''
    } finally {
        if ($null -ne $reader) { $reader.Dispose() }
        elseif ($null -ne $stream) { $stream.Dispose() }
    }
}

function Get-HASHIStartupStage {
    param(
        [string]$LogPath,
        [long]$LogOffset
    )
    $text = Read-LogTextSince -Path $LogPath -Offset $LogOffset
    if (-not $text) { return $null }
    $stages = @(
        [PSCustomObject]@{ Percent = 12; Pattern = '=== Bridge starting ==='; English = 'Loading HASHI core'; Chinese = '正在加载 HASHI 核心' },
        [PSCustomObject]@{ Percent = 25; Pattern = 'Agents to start:'; English = 'Loading Portable agent configuration'; Chinese = '正在加载 Portable Agent 配置' },
        [PSCustomObject]@{ Percent = 40; Pattern = 'Hashi Remote lifecycle:'; English = 'Starting local and Remote services'; Chinese = '正在启动本机与 Remote 服务' },
        [PSCustomObject]@{ Percent = 58; Pattern = 'starting backend initialization'; English = 'Initializing HER v2'; Chinese = '正在初始化 HER v2' },
        [PSCustomObject]@{ Percent = 76; Pattern = 'backend ready'; English = 'HER v2 is ready'; Chinese = 'HER v2 已就绪' },
        [PSCustomObject]@{ Percent = 88; Pattern = 'starting local surfaces directly'; English = 'Starting local interfaces'; Chinese = '正在启动本机界面' },
        [PSCustomObject]@{ Percent = 96; Pattern = 'Backend API listening on'; English = 'Verifying the local API'; Chinese = '正在验证本机 API' }
    )
    $matched = $null
    foreach ($stage in $stages) {
        if ($text -match [Regex]::Escape([string]$stage.Pattern)) {
            $matched = $stage
        }
    }
    return $matched
}

function Write-StartupStage {
    param(
        [string]$Component,
        [int]$Percent,
        [string]$English,
        [string]$Chinese
    )
    Write-BilingualMessage `
        -English "$Component [$Percent%] $English" `
        -Chinese "$Component [$Percent%] $Chinese" `
        -ForegroundColor Cyan
}

function Get-Health {
    param([int]$Port)
    try {
        return Invoke-RestMethod -Method Get -Uri "http://127.0.0.1:$Port/api/health" -TimeoutSec 2
    } catch {
        return $null
    }
}

function Get-LiveProcessFromPidFile {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { return $null }
    try {
        $processId = [int](Get-Content -LiteralPath $Path -Raw).Trim()
        return Get-Process -Id $processId -ErrorAction Stop
    } catch {
        return $null
    }
}

function Quote-ProcessArgument {
    param([string]$Value)
    return '"' + ($Value -replace '(\\*)"', '$1$1\"' -replace '(\\+)$', '$1$1') + '"'
}

function Start-HASHIBackend {
    if (-not (Test-IsAdministrator)) {
        throw 'HASHI Portable must be started with administrator privileges.'
    }
    [void](Ensure-LocalAccelerationCache)
    Initialize-PortableEnvironment
    $config = Get-PortableConfig
    $port = [int]$config.global.workbench_port
    $env:HASHI_WORKBENCH_URL = "http://127.0.0.1:$port"
    $health = Get-Health -Port $port
    if ($null -ne $health) {
        if ([string]$health.instance_id -ne [string]$config.global.instance_id) {
            throw "Port $port belongs to another HASHI instance ($($health.instance_id))."
        }
        return $health
    }

    $python = Join-Path $script:PythonRoot 'python.exe'
    $main = Join-Path $script:HashiRoot 'main.py'
    if (-not (Test-Path -LiteralPath $python)) { throw "Portable Python is missing: $python" }
    if (-not (Test-Path -LiteralPath $main)) { throw "HASHI entry point is missing: $main" }

    $stdout = Join-Path $script:DataRoot 'logs\hashi-console.log'
    $stderr = Join-Path $script:DataRoot 'logs\hashi-console-error.log'
    $arguments = @(
        (Quote-ProcessArgument $main),
        '--bridge-home',
        (Quote-ProcessArgument $script:DataRoot),
        '--agents',
        'portable'
    )
    Write-BilingualMessage `
        -English 'Starting HASHI...' `
        -Chinese '正在启动 HASHI……' `
        -ForegroundColor Cyan
    Write-BilingualMessage `
        -English 'This may take a few minutes. Keep this window open and leave the USB connected.' `
        -Chinese '这可能需要几分钟。请保持此窗口开启，并勿拔出 USB。' `
        -ForegroundColor Yellow
    $bridgeLog = Join-Path $script:DataRoot 'logs\bridge.log'
    $bridgeLogOffset = if (Test-Path -LiteralPath $bridgeLog -PathType Leaf) {
        [long](Get-Item -LiteralPath $bridgeLog).Length
    } else {
        [long]0
    }
    Write-StartupStage `
        -Component 'HASHI' `
        -Percent 5 `
        -English 'Launching the Portable process' `
        -Chinese '正在启动 Portable 进程'
    $process = Start-Process -FilePath $python -ArgumentList $arguments -WorkingDirectory $script:DataRoot -WindowStyle Hidden -RedirectStandardOutput $stdout -RedirectStandardError $stderr -PassThru
    Set-Content -LiteralPath $script:HashiPidPath -Value $process.Id -Encoding ASCII

    $startedAt = Get-Date
    $deadline = $startedAt.AddSeconds($script:HashiStartupTimeoutSeconds)
    $lastStagePercent = 5
    do {
        Start-Sleep -Milliseconds 500
        $process.Refresh()
        if ($process.HasExited) {
            $tail = if (Test-Path -LiteralPath $stderr) { (Get-Content -LiteralPath $stderr -Tail 25) -join [Environment]::NewLine } else { '' }
            throw "HASHI exited during startup (code $($process.ExitCode)).`n$tail"
        }
        $health = Get-Health -Port $port
        if ($null -ne $health -and [string]$health.instance_id -eq [string]$config.global.instance_id) {
            Write-StartupStage `
                -Component 'HASHI' `
                -Percent 100 `
                -English 'Local API is ready' `
                -Chinese '本机 API 已就绪'
            return $health
        }
        $stage = Get-HASHIStartupStage -LogPath $bridgeLog -LogOffset $bridgeLogOffset
        if ($null -ne $stage -and [int]$stage.Percent -gt $lastStagePercent) {
            Write-StartupStage `
                -Component 'HASHI' `
                -Percent ([int]$stage.Percent) `
                -English ([string]$stage.English) `
                -Chinese ([string]$stage.Chinese)
            $lastStagePercent = [int]$stage.Percent
        }
    } while ((Get-Date) -lt $deadline)

    $stdoutTail = if (Test-Path -LiteralPath $stdout) { (Get-Content -LiteralPath $stdout -Tail 25) -join [Environment]::NewLine } else { '' }
    $stderrTail = if (Test-Path -LiteralPath $stderr) { (Get-Content -LiteralPath $stderr -Tail 25) -join [Environment]::NewLine } else { '' }
    throw "HASHI did not become healthy on port $port within $script:HashiStartupTimeoutSeconds seconds.`nRecent output:`n$stdoutTail`n$stderrTail"
}

function Start-WorkbenchServer {
    [void](Ensure-LocalAccelerationCache)
    Initialize-PortableEnvironment
    $config = Get-PortableConfig
    $secrets = Get-PortableSecrets
    $port = 18888
    try {
        $response = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:$port/" -TimeoutSec 2
        if ($response.StatusCode -eq 200) { return }
    } catch {}

    $node = Join-Path $script:NodeRoot 'node.exe'
    $server = Join-Path $script:WorkbenchRoot 'server.mjs'
    if (-not (Test-Path -LiteralPath $node)) { throw "Portable Node is missing: $node" }
    if (-not (Test-Path -LiteralPath $server)) { throw "Workbench server is missing: $server" }

    $env:PORT = [string]$port
    $env:BRIDGE_U_API = "http://127.0.0.1:$([int]$config.global.workbench_port)"
    $env:BRIDGE_U_ADMIN_TOKEN = [string]$secrets.workbench_admin_token
    $env:HASHI_WORKBENCH_UI_DIR = Join-Path $script:WorkbenchRoot 'ui'
    $env:HASHI_WORKBENCH_KASUMI_APP_DIR = Join-Path $script:WorkbenchRoot 'kasumi-app'
    $env:HASHI_WORKBENCH_STATE_DIR = Join-Path $script:DataRoot 'state\workbench'
    $env:HASHI_WORKBENCH_DATA_DIR = Join-Path $script:DataRoot 'workbench'
    $env:HASHI_WORKBENCH_CONTENT_ROOT = $script:DataRoot
    $env:HASHI_WORKBENCH_REPOSITORY_ROOT = $script:HashiRoot
    $env:HASHI_WORKBENCH_AGENTS_JSON = Join-Path $script:DataRoot 'agents.json'
    $env:HASHI_WORKBENCH_PYTHON = Join-Path $script:PythonRoot 'python.exe'
    if ($script:ExecutionMode -eq 'local-cache') {
        $observabilityRoot = Join-Path $script:LocalInstanceRoot 'Logs\workbench'
    } else {
        $observabilityRoot = Join-Path $script:DataRoot 'logs\workbench-observability'
    }
    New-Item -ItemType Directory -Force -Path $observabilityRoot | Out-Null
    $env:HASHI_WORKBENCH_OBSERVABILITY_DIR = $observabilityRoot

    $stdout = Join-Path $script:DataRoot 'logs\workbench-console.log'
    $stderr = Join-Path $script:DataRoot 'logs\workbench-console-error.log'
    Write-BilingualMessage `
        -English 'Starting Workbench...' `
        -Chinese '正在启动 Workbench……' `
        -ForegroundColor Cyan
    Write-StartupStage `
        -Component 'Workbench' `
        -Percent 25 `
        -English 'Launching the local interface server' `
        -Chinese '正在启动本机界面服务'
    $process = Start-Process -FilePath $node -ArgumentList (Quote-ProcessArgument $server) -WorkingDirectory $script:WorkbenchRoot -WindowStyle Hidden -RedirectStandardOutput $stdout -RedirectStandardError $stderr -PassThru
    Set-Content -LiteralPath $script:WorkbenchPidPath -Value $process.Id -Encoding ASCII

    $startedAt = Get-Date
    $deadline = $startedAt.AddSeconds($script:WorkbenchStartupTimeoutSeconds)
    do {
        Start-Sleep -Milliseconds 400
        if ($process.HasExited) {
            $tail = if (Test-Path -LiteralPath $stderr) { (Get-Content -LiteralPath $stderr -Tail 25) -join [Environment]::NewLine } else { '' }
            throw "Workbench exited during startup (code $($process.ExitCode)).`n$tail"
        }
        try {
            $response = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:$port/" -TimeoutSec 2
            if ($response.StatusCode -eq 200) {
                Write-StartupStage `
                    -Component 'Workbench' `
                    -Percent 100 `
                    -English 'Local interface is ready' `
                    -Chinese '本机界面已就绪'
                return
            }
        } catch {}
    } while ((Get-Date) -lt $deadline)
    throw "Workbench did not become healthy on port $port within $script:WorkbenchStartupTimeoutSeconds seconds."
}

function Find-SystemBrowser {
    $candidates = @(
        (Join-Path ${env:ProgramFiles(x86)} 'Microsoft\Edge\Application\msedge.exe'),
        (Join-Path $env:ProgramFiles 'Microsoft\Edge\Application\msedge.exe'),
        (Join-Path $env:LOCALAPPDATA 'Microsoft\Edge\Application\msedge.exe'),
        (Join-Path $env:ProgramFiles 'Google\Chrome\Application\chrome.exe'),
        (Join-Path ${env:ProgramFiles(x86)} 'Google\Chrome\Application\chrome.exe'),
        (Join-Path $env:LOCALAPPDATA 'Google\Chrome\Application\chrome.exe')
    )
    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path -LiteralPath $candidate)) { return $candidate }
    }
    return $null
}
