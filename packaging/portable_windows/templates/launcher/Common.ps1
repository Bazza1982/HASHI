Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$script:PortableRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$script:UsbAppRoot = Join-Path $script:PortableRoot 'app'
$script:UsbPythonRoot = Join-Path $script:PortableRoot 'runtime\python'
$script:UsbNodeRoot = Join-Path $script:PortableRoot 'runtime\node'
$script:UsbBinRoot = Join-Path $script:PortableRoot 'runtime\bin'
$script:AppRoot = $script:UsbAppRoot
$script:HashiRoot = Join-Path $script:AppRoot 'hashi'
$script:WorkbenchRoot = Join-Path $script:AppRoot 'workbench'
$script:DataRoot = Join-Path $script:PortableRoot 'data'
$script:PythonRoot = $script:UsbPythonRoot
$script:NodeRoot = $script:UsbNodeRoot
$script:BinRoot = $script:UsbBinRoot
$script:ExecutionMode = 'usb'
$script:LocalCacheManifestPath = Join-Path $script:PortableRoot 'install\local-cache-manifest.json'
$script:LocalCacheRoot = Join-Path ([Environment]::GetFolderPath('CommonApplicationData')) 'HASHI Portable\Cache'
$script:LocalCacheInstallAttempted = $false
$script:LauncherStateRoot = Join-Path $script:DataRoot 'state\launcher'
$script:HashiPidPath = Join-Path $script:LauncherStateRoot 'hashi.pid'
$script:WorkbenchPidPath = Join-Path $script:LauncherStateRoot 'workbench.pid'
$script:HashiStartupTimeoutSeconds = 1800
$script:HashiStartupProgressSeconds = 15

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
        Write-Host 'Local acceleration was skipped by HASHI_PORTABLE_SKIP_LOCAL_CACHE=1.' -ForegroundColor Yellow
        return $false
    }
    if ($script:LocalCacheInstallAttempted) { return $false }
    $script:LocalCacheInstallAttempted = $true
    if ($null -eq (Get-LocalCacheManifest)) {
        Write-Host 'This USB has no local acceleration payload; using the expanded USB copy.' -ForegroundColor Yellow
        return $false
    }

    $installer = Join-Path $PSScriptRoot 'Install-LocalCache.ps1'
    if (-not (Test-Path -LiteralPath $installer -PathType Leaf)) {
        Write-Host 'Local acceleration installer is missing; using the expanded USB copy.' -ForegroundColor Yellow
        return $false
    }
    Write-Host 'First use on this PC: preparing the local acceleration cache.' -ForegroundColor Cyan
    Write-Host 'Windows will request administrator approval. Installation has no fixed 10-minute cutoff.' -ForegroundColor Yellow
    try {
        $powerShell = Join-Path $PSHOME 'powershell.exe'
        $arguments = @(
            '-NoLogo',
            '-NoProfile',
            '-ExecutionPolicy',
            'Bypass',
            '-File',
            (Quote-ProcessArgument $installer),
            '-PauseOnError'
        )
        $process = Start-Process -FilePath $powerShell -Verb RunAs -ArgumentList $arguments -Wait -PassThru
        if ($process.ExitCode -ne 0) {
            Write-Host 'Local acceleration was not installed; continuing safely from the USB.' -ForegroundColor Yellow
            Use-UsbExecutionRoots
            return $false
        }
    } catch {
        Write-Host "Local acceleration could not be installed: $($_.Exception.Message)" -ForegroundColor Yellow
        Write-Host 'Continuing safely from the expanded USB copy.' -ForegroundColor Yellow
        Use-UsbExecutionRoots
        return $false
    }
    if (-not (Use-ExistingLocalCache)) {
        Write-Host 'The installed cache did not pass its readiness check; using the USB copy.' -ForegroundColor Yellow
        return $false
    }
    Write-Host 'Using the verified local program cache; authoritative data remains on the USB.' -ForegroundColor Green
    return $true
}

function Initialize-PortableEnvironment {
    New-Item -ItemType Directory -Force -Path $script:LauncherStateRoot | Out-Null
    New-Item -ItemType Directory -Force -Path (Join-Path $script:DataRoot 'logs') | Out-Null
    New-Item -ItemType Directory -Force -Path (Join-Path $script:DataRoot 'tmp') | Out-Null

    $env:BRIDGE_HOME = $script:DataRoot
    $env:HASHI_REMOTE_ROOT = $script:DataRoot
    $env:HASHI_REMOTE_CONTROL_ROOT = $script:HashiRoot
    $env:HASHI_REMOTE_STATE_DIR = Join-Path $script:DataRoot 'state\remote'
    $env:HASHI_PORTABLE_USB_ROOT = $script:PortableRoot
    $env:HASHI_PORTABLE_EXECUTION_MODE = $script:ExecutionMode
    $env:HASHI_TUI_ENABLE_API_GATEWAY = '0'
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
    [void](Ensure-LocalAccelerationCache)
    Initialize-PortableEnvironment
    $config = Get-PortableConfig
    $port = [int]$config.global.workbench_port
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
    Write-Host "Starting HASHI Portable in $script:ExecutionMode mode..." -ForegroundColor Cyan
    Write-Host 'A slow PC or USB fallback can take several minutes. Please keep this window open.' -ForegroundColor Yellow
    $process = Start-Process -FilePath $python -ArgumentList $arguments -WorkingDirectory $script:DataRoot -WindowStyle Hidden -RedirectStandardOutput $stdout -RedirectStandardError $stderr -PassThru
    Set-Content -LiteralPath $script:HashiPidPath -Value $process.Id -Encoding ASCII

    $startedAt = Get-Date
    $deadline = $startedAt.AddSeconds($script:HashiStartupTimeoutSeconds)
    $nextProgress = $startedAt.AddSeconds($script:HashiStartupProgressSeconds)
    do {
        Start-Sleep -Milliseconds 500
        $process.Refresh()
        if ($process.HasExited) {
            $tail = if (Test-Path -LiteralPath $stderr) { (Get-Content -LiteralPath $stderr -Tail 25) -join [Environment]::NewLine } else { '' }
            throw "HASHI exited during startup (code $($process.ExitCode)).`n$tail"
        }
        $health = Get-Health -Port $port
        if ($null -ne $health -and [string]$health.instance_id -eq [string]$config.global.instance_id) {
            return $health
        }
        $now = Get-Date
        if ($now -ge $nextProgress) {
            $elapsed = [int]($now - $startedAt).TotalSeconds
            Write-Host "Still starting HASHI... $elapsed seconds elapsed (backend process $($process.Id) is running)." -ForegroundColor Cyan
            $nextProgress = $now.AddSeconds($script:HashiStartupProgressSeconds)
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

    $stdout = Join-Path $script:DataRoot 'logs\workbench-console.log'
    $stderr = Join-Path $script:DataRoot 'logs\workbench-console-error.log'
    $process = Start-Process -FilePath $node -ArgumentList (Quote-ProcessArgument $server) -WorkingDirectory $script:WorkbenchRoot -WindowStyle Hidden -RedirectStandardOutput $stdout -RedirectStandardError $stderr -PassThru
    Set-Content -LiteralPath $script:WorkbenchPidPath -Value $process.Id -Encoding ASCII

    $deadline = (Get-Date).AddSeconds(45)
    do {
        Start-Sleep -Milliseconds 400
        if ($process.HasExited) {
            $tail = if (Test-Path -LiteralPath $stderr) { (Get-Content -LiteralPath $stderr -Tail 25) -join [Environment]::NewLine } else { '' }
            throw "Workbench exited during startup (code $($process.ExitCode)).`n$tail"
        }
        try {
            $response = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:$port/" -TimeoutSec 2
            if ($response.StatusCode -eq 200) { return }
        } catch {}
    } while ((Get-Date) -lt $deadline)
    throw "Workbench did not become healthy on port $port within 45 seconds."
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
