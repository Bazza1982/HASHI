Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

try {
    $utf8 = New-Object System.Text.UTF8Encoding($false)
    [Console]::InputEncoding = $utf8
    [Console]::OutputEncoding = $utf8
    $global:OutputEncoding = $utf8
    $Host.UI.RawUI.WindowTitle = 'HASHI Portable'
} catch {}

$script:ExpectedInstallRoot = [System.IO.Path]::GetFullPath('C:\HASHI-Portable')
$script:PortableRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$script:AppRoot = Join-Path $script:PortableRoot 'app'
$script:HashiRoot = Join-Path $script:AppRoot 'hashi'
$script:WorkbenchRoot = Join-Path $script:AppRoot 'workbench'
$script:DataRoot = Join-Path $script:PortableRoot 'data'
$script:PythonRoot = Join-Path $script:PortableRoot 'runtime\python'
$script:NodeRoot = Join-Path $script:PortableRoot 'runtime\node'
$script:BinRoot = Join-Path $script:PortableRoot 'runtime\bin'
$script:PortableIdentityPath = Join-Path $script:DataRoot 'portable-instance.json'
$script:InstallMarkerPath = Join-Path $script:PortableRoot '.hashi-local-install.json'
$script:LauncherStateRoot = Join-Path $script:DataRoot 'state\launcher'
$script:EndpointPath = Join-Path $script:DataRoot 'state\local-endpoint.json'
$script:HashiPidPath = Join-Path $script:LauncherStateRoot 'hashi.pid'
$script:WorkbenchPidPath = Join-Path $script:LauncherStateRoot 'workbench.pid'
$script:HashiStartupTimeoutSeconds = 1800
$script:WorkbenchStartupTimeoutSeconds = 300
$script:PortableInstanceId = ''

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

function Test-PathInsideRoot {
    param(
        [string]$Candidate,
        [string]$Root
    )
    if (-not $Candidate -or -not $Root) { return $false }
    try {
        $candidatePath = [System.IO.Path]::GetFullPath($Candidate)
        $rootPath = [System.IO.Path]::GetFullPath($Root).TrimEnd('\') + '\'
        return $candidatePath.StartsWith(
            $rootPath,
            [System.StringComparison]::OrdinalIgnoreCase
        )
    } catch {
        return $false
    }
}

function Get-Sha256 {
    param([string]$Path)
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Read-JsonObject {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $null }
    try {
        return Get-Content -LiteralPath $Path -Raw -Encoding UTF8 | ConvertFrom-Json
    } catch {
        return $null
    }
}

function Write-Utf8JsonAtomic {
    param(
        [string]$Path,
        [object]$Value
    )
    $directory = [System.IO.Path]::GetDirectoryName($Path)
    New-Item -ItemType Directory -Force -Path $directory | Out-Null
    $temporary = Join-Path $directory ('.' + [System.IO.Path]::GetFileName($Path) + '.' + [Guid]::NewGuid().ToString('N') + '.tmp')
    $backup = Join-Path $directory ('.' + [System.IO.Path]::GetFileName($Path) + '.' + [Guid]::NewGuid().ToString('N') + '.bak')
    $json = ($Value | ConvertTo-Json -Depth 100) + [Environment]::NewLine
    $encoding = New-Object System.Text.UTF8Encoding($false)
    try {
        [System.IO.File]::WriteAllText($temporary, $json, $encoding)
        if (Test-Path -LiteralPath $Path -PathType Leaf) {
            [System.IO.File]::Replace($temporary, $Path, $backup, $true)
            Remove-Item -LiteralPath $backup -Force -ErrorAction SilentlyContinue
        } else {
            [System.IO.File]::Move($temporary, $Path)
        }
    } finally {
        Remove-Item -LiteralPath $temporary -Force -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath $backup -Force -ErrorAction SilentlyContinue
    }
}

function Initialize-LocalInstallation {
    if (-not [string]::Equals(
        $script:PortableRoot.TrimEnd('\'),
        $script:ExpectedInstallRoot.TrimEnd('\'),
        [System.StringComparison]::OrdinalIgnoreCase
    )) {
        throw 'HASHI must be installed before it can run. Use 安装_HASHI_到本机.bat on the USB drive.'
    }
    if (-not (Test-Path -LiteralPath $script:PortableRoot -PathType Container)) {
        throw 'The HASHI installation folder is missing.'
    }
    $rootItem = Get-Item -LiteralPath $script:PortableRoot -Force
    if (($rootItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw 'The HASHI installation folder cannot be a link or reparse point.'
    }

    $marker = Read-JsonObject -Path $script:InstallMarkerPath
    $identity = Read-JsonObject -Path $script:PortableIdentityPath
    if ($null -eq $marker -or $null -eq $identity) {
        throw 'The HASHI local installation marker is missing or unreadable.'
    }
    $instanceId = [string]$identity.portable_instance_id
    if (
        [int]$identity.schema_version -ne 1 -or
        [string]$identity.product -ne 'HASHI Portable Windows x64' -or
        $instanceId -notmatch '^[0-9a-f]{32}$'
    ) {
        throw 'The HASHI portable identity is invalid.'
    }
    $markedRoot = [System.IO.Path]::GetFullPath([string]$marker.install_root)
    if (
        [int]$marker.schema_version -ne 1 -or
        [string]$marker.product -ne 'HASHI Portable Local Installation' -or
        [string]$marker.portable_instance_id -ne $instanceId -or
        -not [string]::Equals(
            $markedRoot.TrimEnd('\'),
            $script:ExpectedInstallRoot.TrimEnd('\'),
            [System.StringComparison]::OrdinalIgnoreCase
        )
    ) {
        throw 'The HASHI local installation marker is invalid.'
    }
    $buildInfo = Join-Path $script:PortableRoot 'BUILD_INFO.json'
    if (
        -not (Test-Path -LiteralPath $buildInfo -PathType Leaf) -or
        [string]$marker.bundle_id -ne (Get-Sha256 -Path $buildInfo)
    ) {
        throw 'The HASHI local installation build identity does not match its marker.'
    }
    foreach ($required in @(
        (Join-Path $script:PythonRoot 'python.exe'),
        (Join-Path $script:NodeRoot 'node.exe'),
        (Join-Path $script:HashiRoot 'main.py'),
        (Join-Path $script:HashiRoot 'tui.py'),
        (Join-Path $script:WorkbenchRoot 'server.mjs'),
        (Join-Path $script:DataRoot 'agents.json'),
        (Join-Path $script:DataRoot 'secrets.json')
    )) {
        if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
            throw "The HASHI local installation is incomplete: $required"
        }
    }
    $script:PortableInstanceId = $instanceId
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
        -English 'Run 诊断_HASHI.bat for a guided system check.' `
        -Chinese '请运行 诊断_HASHI.bat 进行引导式系统检查。' `
        -ForegroundColor Yellow
}

function Get-PortableConfig {
    $config = Read-JsonObject -Path (Join-Path $script:DataRoot 'agents.json')
    if ($null -eq $config) { throw 'The HASHI configuration is unreadable.' }
    return $config
}

function Get-PortableSecrets {
    $secrets = Read-JsonObject -Path (Join-Path $script:DataRoot 'secrets.json')
    if ($null -eq $secrets) { throw 'The HASHI secrets file is unreadable.' }
    return $secrets
}

function Initialize-PortableEnvironment {
    New-Item -ItemType Directory -Force -Path $script:LauncherStateRoot | Out-Null
    New-Item -ItemType Directory -Force -Path (Join-Path $script:DataRoot 'logs') | Out-Null
    New-Item -ItemType Directory -Force -Path (Join-Path $script:DataRoot 'tmp') | Out-Null

    $env:BRIDGE_HOME = $script:DataRoot
    $env:HASHI_REMOTE_ROOT = $script:DataRoot
    $env:HASHI_REMOTE_CONTROL_ROOT = $script:HashiRoot
    $env:HASHI_REMOTE_STATE_DIR = Join-Path $script:DataRoot 'state\remote'
    $env:HASHI_REMOTE_LIVE_ENDPOINTS_PATH = Join-Path $script:DataRoot 'state\remote_live_endpoints.json'
    $env:HASHI_PORTABLE_ROOT = $script:PortableRoot
    $env:HASHI_PORTABLE_EXECUTION_MODE = 'local-install'
    $env:HASHI_WINDOWS_NATIVE_ONLY = '1'
    Remove-Item Env:HASHI_PORTABLE_STORAGE_PROFILE -ErrorAction SilentlyContinue
    $env:HASHI_LOCAL_ENDPOINT_FILE = $script:EndpointPath
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
    $powerShellRoot = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0'
    $env:PATH = "$script:PythonRoot;$script:BinRoot;$powerShellRoot;$env:SystemRoot\System32;$env:SystemRoot"
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
        if ($text -match [Regex]::Escape([string]$stage.Pattern)) { $matched = $stage }
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

function Get-OwnedProcess {
    param([int]$ProcessId)
    if ($ProcessId -le 0) { return $null }
    try {
        $process = Get-Process -Id $ProcessId -ErrorAction Stop
        $candidate = Get-CimInstance Win32_Process -Filter "ProcessId = $ProcessId" -ErrorAction Stop
        if (-not (Test-PathInsideRoot -Candidate ([string]$candidate.ExecutablePath) -Root $script:PortableRoot)) {
            return $null
        }
        return $process
    } catch {
        return $null
    }
}

function Get-UnverifiedOwnedBackendProcesses {
    $mainPath = [System.IO.Path]::GetFullPath((Join-Path $script:HashiRoot 'main.py'))
    $dataPath = [System.IO.Path]::GetFullPath($script:DataRoot)
    $matches = @()
    foreach ($candidate in @(Get-CimInstance Win32_Process -ErrorAction Stop)) {
        $executable = [string]$candidate.ExecutablePath
        $commandLine = [string]$candidate.CommandLine
        if (
            (Test-PathInsideRoot -Candidate $executable -Root $script:PortableRoot) -and
            $commandLine.IndexOf($mainPath, [StringComparison]::OrdinalIgnoreCase) -ge 0 -and
            $commandLine.IndexOf($dataPath, [StringComparison]::OrdinalIgnoreCase) -ge 0
        ) {
            $matches += $candidate
        }
    }
    return @($matches)
}

function Read-LocalEndpoint {
    $endpoint = Read-JsonObject -Path $script:EndpointPath
    if ($null -eq $endpoint) { return $null }
    try {
        $config = Get-PortableConfig
        $marker = Read-JsonObject -Path $script:InstallMarkerPath
        $markedRoot = [System.IO.Path]::GetFullPath([string]$endpoint.install_root)
        $apiPort = [int]$endpoint.api_port
        if (
            $null -eq $marker -or
            [int]$endpoint.schema_version -ne 1 -or
            [string]$endpoint.product -ne 'HASHI Portable Local Endpoint' -or
            [string]$endpoint.portable_instance_id -ne $script:PortableInstanceId -or
            [string]$endpoint.instance_id -ne [string]$config.global.instance_id -or
            [string]$endpoint.build_id -ne [string]$marker.bundle_id -or
            [string]$endpoint.api_host -ne '127.0.0.1' -or
            $apiPort -lt 1 -or $apiPort -gt 65535 -or
            -not [string]::Equals(
                $markedRoot.TrimEnd('\'),
                $script:PortableRoot.TrimEnd('\'),
                [System.StringComparison]::OrdinalIgnoreCase
            ) -or
            [string]$endpoint.launch_nonce -notmatch '^[0-9a-f]{32}$'
        ) {
            return $null
        }
        return $endpoint
    } catch {
        return $null
    }
}

function Get-VerifiedLocalEndpoint {
    $endpoint = Read-LocalEndpoint
    if ($null -eq $endpoint) { return $null }
    $process = Get-OwnedProcess -ProcessId ([int]$endpoint.backend_pid)
    if ($null -eq $process) { return $null }
    try {
        $process.Refresh()
        if ([long]$endpoint.backend_start_ticks -ne [long]$process.StartTime.ToUniversalTime().Ticks) {
            return $null
        }
    } catch {
        return $null
    }
    $health = Get-Health -Port ([int]$endpoint.api_port)
    if (
        $null -eq $health -or
        [string]$health.instance_id -ne [string]$endpoint.instance_id -or
        [int]$health.workbench_port -ne [int]$endpoint.api_port
    ) {
        return $null
    }
    return $endpoint
}

function Test-LoopbackPortAvailable {
    param([int]$Port)
    if ($Port -lt 1024 -or $Port -gt 65535) { return $false }
    $listener = $null
    try {
        $listener = [System.Net.Sockets.TcpListener]::new(
            [System.Net.IPAddress]::Loopback,
            $Port
        )
        $listener.Start()
        return $true
    } catch {
        return $false
    } finally {
        if ($null -ne $listener) { try { $listener.Stop() } catch {} }
    }
}

function Get-FreeLoopbackPort {
    param(
        [int]$PreferredPort,
        [int[]]$ReservedPorts = @()
    )
    if (
        $PreferredPort -notin $ReservedPorts -and
        (Test-LoopbackPortAvailable -Port $PreferredPort)
    ) {
        return $PreferredPort
    }
    for ($attempt = 0; $attempt -lt 20; $attempt++) {
        $listener = [System.Net.Sockets.TcpListener]::new(
            [System.Net.IPAddress]::Loopback,
            0
        )
        try {
            $listener.Start()
            $candidate = [int]$listener.LocalEndpoint.Port
        } finally {
            $listener.Stop()
        }
        if ($candidate -notin $ReservedPorts -and $candidate -ge 1024) {
            return $candidate
        }
    }
    throw 'Windows could not allocate a free local TCP port for HASHI.'
}

function Set-LocalApiPort {
    param([int]$Port)
    $configPath = Join-Path $script:DataRoot 'agents.json'
    $config = Get-PortableConfig
    $config.global.api_host = '127.0.0.1'
    $config.global.workbench_port = $Port
    Write-Utf8JsonAtomic -Path $configPath -Value $config
}

function Write-BackendEndpoint {
    param(
        [System.Diagnostics.Process]$Process,
        [int]$ApiPort,
        [string]$LaunchNonce
    )
    $config = Get-PortableConfig
    $marker = Read-JsonObject -Path $script:InstallMarkerPath
    $Process.Refresh()
    $record = [ordered]@{
        schema_version = 1
        product = 'HASHI Portable Local Endpoint'
        portable_instance_id = $script:PortableInstanceId
        instance_id = [string]$config.global.instance_id
        install_root = $script:PortableRoot
        build_id = [string]$marker.bundle_id
        api_host = '127.0.0.1'
        api_port = $ApiPort
        backend_pid = $Process.Id
        backend_start_ticks = $Process.StartTime.ToUniversalTime().Ticks
        launch_nonce = $LaunchNonce
        workbench_ui_port = 0
        workbench_pid = 0
        workbench_start_ticks = 0
        written_at_utc = [DateTime]::UtcNow.ToString('o')
    }
    Write-Utf8JsonAtomic -Path $script:EndpointPath -Value $record
    return [PSCustomObject]$record
}

function Update-WorkbenchEndpoint {
    param(
        [int]$Port,
        [System.Diagnostics.Process]$Process
    )
    $endpoint = Get-VerifiedLocalEndpoint
    if ($null -eq $endpoint) { throw 'The verified local HASHI endpoint disappeared.' }
    $values = [ordered]@{}
    foreach ($property in $endpoint.PSObject.Properties) {
        $values[$property.Name] = $property.Value
    }
    $Process.Refresh()
    $values['workbench_ui_port'] = $Port
    $values['workbench_pid'] = $Process.Id
    $values['workbench_start_ticks'] = $Process.StartTime.ToUniversalTime().Ticks
    $values['written_at_utc'] = [DateTime]::UtcNow.ToString('o')
    Write-Utf8JsonAtomic -Path $script:EndpointPath -Value $values
}

function Quote-ProcessArgument {
    param([string]$Value)
    return '"' + ($Value -replace '(\\*)"', '$1$1\"' -replace '(\\+)$', '$1$1') + '"'
}

function Start-HASHIBackend {
    if (-not (Test-IsAdministrator)) {
        throw 'HASHI Portable must be started with administrator privileges.'
    }
    Initialize-PortableEnvironment
    $existing = Get-VerifiedLocalEndpoint
    if ($null -ne $existing) {
        $env:HASHI_WORKBENCH_URL = "http://127.0.0.1:$([int]$existing.api_port)"
        return $existing
    }
    $unverifiedBackends = @(Get-UnverifiedOwnedBackendProcesses)
    if ($unverifiedBackends.Count -gt 0) {
        throw 'A local HASHI backend is running without a valid endpoint record. Run 停止 HASHI, then start again.'
    }
    Remove-Item -LiteralPath $script:EndpointPath, $script:HashiPidPath -Force -ErrorAction SilentlyContinue

    $config = Get-PortableConfig
    $preferredPort = [int]$config.global.workbench_port
    $port = Get-FreeLoopbackPort -PreferredPort $preferredPort
    Set-LocalApiPort -Port $port
    $env:HASHI_WORKBENCH_URL = "http://127.0.0.1:$port"

    $python = Join-Path $script:PythonRoot 'python.exe'
    $main = Join-Path $script:HashiRoot 'main.py'
    if (-not (Test-Path -LiteralPath $python -PathType Leaf)) { throw "Portable Python is missing: $python" }
    if (-not (Test-Path -LiteralPath $main -PathType Leaf)) { throw "HASHI entry point is missing: $main" }

    $stdout = Join-Path $script:DataRoot 'logs\hashi-console.log'
    $stderr = Join-Path $script:DataRoot 'logs\hashi-console-error.log'
    $arguments = @(
        (Quote-ProcessArgument $main),
        '--bridge-home',
        (Quote-ProcessArgument $script:DataRoot),
        '--agents',
        'agent'
    )
    Write-BilingualMessage `
        -English 'Starting HASHI...' `
        -Chinese '正在启动 HASHI……' `
        -ForegroundColor Cyan
    Write-BilingualMessage `
        -English 'This may take a few minutes. Keep this window open.' `
        -Chinese '这可能需要几分钟，请保持此窗口开启。' `
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
        -English 'Launching the local process' `
        -Chinese '正在启动本机进程'
    $process = Start-Process -FilePath $python -ArgumentList $arguments -WorkingDirectory $script:DataRoot -WindowStyle Hidden -RedirectStandardOutput $stdout -RedirectStandardError $stderr -PassThru
    Set-Content -LiteralPath $script:HashiPidPath -Value $process.Id -Encoding ASCII

    $deadline = (Get-Date).AddSeconds($script:HashiStartupTimeoutSeconds)
    $lastStagePercent = 5
    do {
        Start-Sleep -Milliseconds 500
        $process.Refresh()
        if ($process.HasExited) {
            Remove-Item -LiteralPath $script:HashiPidPath -Force -ErrorAction SilentlyContinue
            $tail = if (Test-Path -LiteralPath $stderr) { (Get-Content -LiteralPath $stderr -Tail 25) -join [Environment]::NewLine } else { '' }
            throw "HASHI exited during startup (code $($process.ExitCode)).`n$tail"
        }
        $health = Get-Health -Port $port
        if ($null -ne $health) {
            if ([string]$health.instance_id -ne [string]$config.global.instance_id) {
                Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
                Remove-Item -LiteralPath $script:HashiPidPath -Force -ErrorAction SilentlyContinue
                throw 'The selected local port was taken by another process during startup.'
            }
            Write-StartupStage `
                -Component 'HASHI' `
                -Percent 100 `
                -English 'Local API is ready' `
                -Chinese '本机 API 已就绪'
            return Write-BackendEndpoint `
                -Process $process `
                -ApiPort $port `
                -LaunchNonce ([Guid]::NewGuid().ToString('N'))
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

    Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $script:HashiPidPath -Force -ErrorAction SilentlyContinue
    $stdoutTail = if (Test-Path -LiteralPath $stdout) { (Get-Content -LiteralPath $stdout -Tail 25) -join [Environment]::NewLine } else { '' }
    $stderrTail = if (Test-Path -LiteralPath $stderr) { (Get-Content -LiteralPath $stderr -Tail 25) -join [Environment]::NewLine } else { '' }
    throw "HASHI did not become healthy on the selected local port within $script:HashiStartupTimeoutSeconds seconds.`nRecent output:`n$stdoutTail`n$stderrTail"
}

function Start-WorkbenchServer {
    Initialize-PortableEnvironment
    $endpoint = Get-VerifiedLocalEndpoint
    if ($null -eq $endpoint) { throw 'HASHI local API is not available.' }

    $existingPort = [int]$endpoint.workbench_ui_port
    $existingProcess = Get-OwnedProcess -ProcessId ([int]$endpoint.workbench_pid)
    if ($existingPort -gt 0 -and $null -ne $existingProcess) {
        try {
            $existingProcess.Refresh()
            if (
                [long]$endpoint.workbench_start_ticks -eq [long]$existingProcess.StartTime.ToUniversalTime().Ticks -and
                (Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:$existingPort/" -TimeoutSec 2).StatusCode -eq 200
            ) {
                return $existingPort
            }
        } catch {}
    }
    if ($null -ne $existingProcess) {
        Stop-Process -Id $existingProcess.Id -Force -ErrorAction SilentlyContinue
    }

    $port = Get-FreeLoopbackPort -PreferredPort 18888 -ReservedPorts @([int]$endpoint.api_port)
    $secrets = Get-PortableSecrets
    $node = Join-Path $script:NodeRoot 'node.exe'
    $server = Join-Path $script:WorkbenchRoot 'server.mjs'
    if (-not (Test-Path -LiteralPath $node -PathType Leaf)) { throw "Portable Node is missing: $node" }
    if (-not (Test-Path -LiteralPath $server -PathType Leaf)) { throw "Workbench server is missing: $server" }

    $env:PORT = [string]$port
    $env:BRIDGE_U_API = "http://127.0.0.1:$([int]$endpoint.api_port)"
    $env:BRIDGE_U_ADMIN_TOKEN = [string]$secrets.workbench_admin_token
    $env:HASHI_WORKBENCH_UI_DIR = Join-Path $script:WorkbenchRoot 'ui'
    $env:HASHI_WORKBENCH_KASUMI_APP_DIR = Join-Path $script:WorkbenchRoot 'kasumi-app'
    $env:HASHI_WORKBENCH_STATE_DIR = Join-Path $script:DataRoot 'state\workbench'
    $env:HASHI_WORKBENCH_DATA_DIR = Join-Path $script:DataRoot 'workbench'
    $env:HASHI_WORKBENCH_CONTENT_ROOT = $script:DataRoot
    $env:HASHI_WORKBENCH_REPOSITORY_ROOT = $script:HashiRoot
    $env:HASHI_WORKBENCH_AGENTS_JSON = Join-Path $script:DataRoot 'agents.json'
    $env:HASHI_WORKBENCH_PYTHON = Join-Path $script:PythonRoot 'python.exe'
    $observabilityRoot = Join-Path $script:DataRoot 'logs\workbench-observability'
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

    $deadline = (Get-Date).AddSeconds($script:WorkbenchStartupTimeoutSeconds)
    do {
        Start-Sleep -Milliseconds 400
        $process.Refresh()
        if ($process.HasExited) {
            Remove-Item -LiteralPath $script:WorkbenchPidPath -Force -ErrorAction SilentlyContinue
            $tail = if (Test-Path -LiteralPath $stderr) { (Get-Content -LiteralPath $stderr -Tail 25) -join [Environment]::NewLine } else { '' }
            throw "Workbench exited during startup (code $($process.ExitCode)).`n$tail"
        }
        try {
            $response = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:$port/" -TimeoutSec 2
            if ($response.StatusCode -eq 200) {
                Update-WorkbenchEndpoint -Port $port -Process $process
                Write-StartupStage `
                    -Component 'Workbench' `
                    -Percent 100 `
                    -English 'Local interface is ready' `
                    -Chinese '本机界面已就绪'
                return $port
            }
        } catch {}
    } while ((Get-Date) -lt $deadline)
    Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $script:WorkbenchPidPath -Force -ErrorAction SilentlyContinue
    throw "Workbench did not become healthy on the selected local port within $script:WorkbenchStartupTimeoutSeconds seconds."
}

function Find-SystemBrowser {
    $candidates = @()
    if (${env:ProgramFiles(x86)}) {
        $candidates += Join-Path ${env:ProgramFiles(x86)} 'Microsoft\Edge\Application\msedge.exe'
    }
    if ($env:ProgramFiles) {
        $candidates += Join-Path $env:ProgramFiles 'Microsoft\Edge\Application\msedge.exe'
    }
    if ($env:LOCALAPPDATA) {
        $candidates += Join-Path $env:LOCALAPPDATA 'Microsoft\Edge\Application\msedge.exe'
    }
    if ($env:ProgramFiles) {
        $candidates += Join-Path $env:ProgramFiles 'Google\Chrome\Application\chrome.exe'
    }
    if (${env:ProgramFiles(x86)}) {
        $candidates += Join-Path ${env:ProgramFiles(x86)} 'Google\Chrome\Application\chrome.exe'
    }
    if ($env:LOCALAPPDATA) {
        $candidates += Join-Path $env:LOCALAPPDATA 'Google\Chrome\Application\chrome.exe'
    }
    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate -PathType Leaf) { return $candidate }
    }
    return $null
}

Initialize-LocalInstallation
