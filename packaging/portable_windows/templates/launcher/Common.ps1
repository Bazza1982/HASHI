Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$script:PortableRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$script:AppRoot = Join-Path $script:PortableRoot 'app'
$script:HashiRoot = Join-Path $script:AppRoot 'hashi'
$script:WorkbenchRoot = Join-Path $script:AppRoot 'workbench'
$script:DataRoot = Join-Path $script:PortableRoot 'data'
$script:PythonRoot = Join-Path $script:PortableRoot 'runtime\python'
$script:NodeRoot = Join-Path $script:PortableRoot 'runtime\node'
$script:BinRoot = Join-Path $script:PortableRoot 'runtime\bin'
$script:LauncherStateRoot = Join-Path $script:DataRoot 'state\launcher'
$script:HashiPidPath = Join-Path $script:LauncherStateRoot 'hashi.pid'
$script:WorkbenchPidPath = Join-Path $script:LauncherStateRoot 'workbench.pid'

function Get-PortableConfig {
    return Get-Content -LiteralPath (Join-Path $script:DataRoot 'agents.json') -Raw -Encoding UTF8 | ConvertFrom-Json
}

function Get-PortableSecrets {
    return Get-Content -LiteralPath (Join-Path $script:DataRoot 'secrets.json') -Raw -Encoding UTF8 | ConvertFrom-Json
}

function Initialize-PortableEnvironment {
    New-Item -ItemType Directory -Force -Path $script:LauncherStateRoot | Out-Null
    New-Item -ItemType Directory -Force -Path (Join-Path $script:DataRoot 'logs') | Out-Null
    New-Item -ItemType Directory -Force -Path (Join-Path $script:DataRoot 'tmp') | Out-Null

    $env:BRIDGE_HOME = $script:DataRoot
    $env:HASHI_REMOTE_ROOT = $script:DataRoot
    $env:HASHI_REMOTE_CONTROL_ROOT = $script:HashiRoot
    $env:HASHI_REMOTE_STATE_DIR = Join-Path $script:DataRoot 'state\remote'
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
    $process = Start-Process -FilePath $python -ArgumentList $arguments -WorkingDirectory $script:DataRoot -WindowStyle Hidden -RedirectStandardOutput $stdout -RedirectStandardError $stderr -PassThru
    Set-Content -LiteralPath $script:HashiPidPath -Value $process.Id -Encoding ASCII

    $deadline = (Get-Date).AddSeconds(75)
    do {
        Start-Sleep -Milliseconds 500
        if ($process.HasExited) {
            $tail = if (Test-Path -LiteralPath $stderr) { (Get-Content -LiteralPath $stderr -Tail 25) -join [Environment]::NewLine } else { '' }
            throw "HASHI exited during startup (code $($process.ExitCode)).`n$tail"
        }
        $health = Get-Health -Port $port
        if ($null -ne $health -and [string]$health.instance_id -eq [string]$config.global.instance_id) {
            return $health
        }
    } while ((Get-Date) -lt $deadline)

    throw "HASHI did not become healthy on port $port within 75 seconds."
}

function Start-WorkbenchServer {
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
