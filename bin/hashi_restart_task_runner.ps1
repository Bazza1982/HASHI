param(
    [Parameter(Mandatory = $true)]
    [string]$HashiRoot,

    [string]$CodeRoot,

    [Parameter(Mandatory = $true)]
    [string]$LogPath,

    [Parameter(Mandatory = $true)]
    [string]$RuntimeTaskName
)

$ErrorActionPreference = "Stop"
$LifecycleClock = [Diagnostics.Stopwatch]::StartNew()
$ResolvedRoot = ([System.IO.Path]::GetFullPath($HashiRoot)).TrimEnd('\')
$ResolvedCodeRoot = if ($CodeRoot) { [System.IO.Path]::GetFullPath($CodeRoot) } else { Join-Path $PSScriptRoot ".." }
$Controller = Join-Path $ResolvedCodeRoot "bin\bridge_ctl.ps1"
if (-not (Test-Path -LiteralPath $Controller -PathType Leaf)) {
    throw "Missing fixed HASHI restart controller: $Controller"
}
if (-not $RuntimeTaskName -or $RuntimeTaskName -match '[\\/]') {
    throw "Invalid fixed HASHI runtime task name: $RuntimeTaskName"
}
$AgentsPath = Join-Path $ResolvedRoot "agents.json"
if (-not (Test-Path -LiteralPath $AgentsPath -PathType Leaf)) {
    throw "Missing HASHI instance configuration: $AgentsPath"
}
$AgentsConfig = Get-Content -LiteralPath $AgentsPath -Raw | ConvertFrom-Json
$ExpectedInstance = [string]$AgentsConfig.global.instance_id
if (-not $ExpectedInstance) { throw "Missing HASHI instance identity" }
$WorkbenchPort = [int]$AgentsConfig.global.workbench_port
if ($WorkbenchPort -lt 1 -or $WorkbenchPort -gt 65535) {
    throw "Invalid HASHI Backend port: $WorkbenchPort"
}

$LogDirectory = Split-Path -Parent $LogPath
New-Item -ItemType Directory -Force -Path $LogDirectory | Out-Null
$LifecycleDirectory = Join-Path $ResolvedRoot "logs\lifecycle"
$LifecycleLog = Join-Path $LifecycleDirectory "platform-$PID.jsonl"
$LifecycleLogReady = $false
try {
    New-Item -ItemType Directory -Force -Path $LifecycleDirectory | Out-Null
    $LifecycleLogReady = $true
} catch {}
function Write-LifecycleTiming {
    param([string]$Stage, [string]$Event, [double]$DurationMs = 0, [int]$Attempt = 0)
    if (-not $LifecycleLogReady) { return }
    try {
        $Entry = [ordered]@{
            schema = 1; stage = $Stage; event = $Event; pid = $PID
            epoch_ns = ([DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() * 1000000)
            clock_resolution_ms = 1
            monotonic_ns = ([Diagnostics.Stopwatch]::GetTimestamp() * 1000000000.0 / [Diagnostics.Stopwatch]::Frequency)
            duration_ms = $DurationMs; attempt = $Attempt
            elapsed_ms = $LifecycleClock.Elapsed.TotalMilliseconds
        }
        $Line = ($Entry | ConvertTo-Json -Compress) + [Environment]::NewLine
        [IO.File]::AppendAllText($LifecycleLog, $Line, [Text.UTF8Encoding]::new($false))
    } catch {}
}
Write-LifecycleTiming "platform.initialize" "end" $LifecycleClock.Elapsed.TotalMilliseconds
Write-LifecycleTiming "platform.restart_task" "start"

$Timestamp = [DateTimeOffset]::Now.ToString("o")
Add-Content -LiteralPath $LogPath -Encoding UTF8 -Value "$Timestamp fixed restart task started for $ResolvedRoot"

$env:BRIDGE_HOME = $ResolvedRoot
$env:BRIDGE_CODE_ROOT = $ResolvedCodeRoot
$env:HASHI_ENABLE_LEGACY_FIXED_RUNTIME = "1"
$StopStarted = $LifecycleClock.Elapsed.TotalMilliseconds
$ControllerOutput = @(& powershell.exe `
    -NoProfile `
    -NonInteractive `
    -ExecutionPolicy Bypass `
    -File $Controller `
    -BridgeHome $ResolvedRoot `
    -Action stop *>&1)
$Result = $LASTEXITCODE
Write-LifecycleTiming "platform.stop_controller" "end" ($LifecycleClock.Elapsed.TotalMilliseconds - $StopStarted)
$ControllerOutput | Out-File -LiteralPath $LogPath -Append -Encoding UTF8
if ($Result -ne 0) {
    Add-Content -LiteralPath $LogPath -Encoding UTF8 -Value "Fixed restart stop failed with exit code $Result"
    exit $Result
}

$TaskSettleStarted = $LifecycleClock.Elapsed.TotalMilliseconds
$RuntimeTask = Get-ScheduledTask -TaskName $RuntimeTaskName -ErrorAction Stop
for ($Attempt = 0; $Attempt -lt 20 -and [string]$RuntimeTask.State -eq "Running"; $Attempt++) {
    Start-Sleep -Milliseconds 250
    $RuntimeTask = Get-ScheduledTask -TaskName $RuntimeTaskName -ErrorAction Stop
}
if ([string]$RuntimeTask.State -eq "Running") {
    Stop-ScheduledTask -TaskName $RuntimeTaskName -ErrorAction Stop
    for ($Attempt = 0; $Attempt -lt 20; $Attempt++) {
        Start-Sleep -Milliseconds 250
        $RuntimeTask = Get-ScheduledTask -TaskName $RuntimeTaskName -ErrorAction Stop
        if ([string]$RuntimeTask.State -ne "Running") {
            break
        }
    }
}
if ([string]$RuntimeTask.State -eq "Running") {
    Add-Content -LiteralPath $LogPath -Encoding UTF8 -Value "Fixed HASHI runtime task did not stop cleanly"
    exit 1
}

Write-LifecycleTiming "platform.task_stop_settle" "end" ($LifecycleClock.Elapsed.TotalMilliseconds - $TaskSettleStarted)
$TaskStartStarted = $LifecycleClock.Elapsed.TotalMilliseconds
Start-ScheduledTask -TaskName $RuntimeTaskName -ErrorAction Stop
Write-LifecycleTiming "platform.start_registered_task" "end" ($LifecycleClock.Elapsed.TotalMilliseconds - $TaskStartStarted)
Add-Content -LiteralPath $LogPath -Encoding UTF8 -Value "Triggered fixed HASHI runtime task $RuntimeTaskName"

$HealthUri = "http://127.0.0.1:$WorkbenchPort/api/health"
# Full independent-runtime preparation and multi-Agent startup can outlast
# the old window. The registered task still has a ten-minute execution limit.
for ($Attempt = 0; $Attempt -lt 300; $Attempt++) {
    Start-Sleep -Seconds 1
    $HealthStarted = $LifecycleClock.Elapsed.TotalMilliseconds
    try {
        $Health = Invoke-RestMethod -Uri $HealthUri -Method Get -TimeoutSec 1
        $LocalReady = (
            $Health.startup.services_ready -eq $true -and
            [int]$Health.startup.failed_agents -eq 0
        )
        if (
            $Health.ok -eq $true -and
            $ExpectedInstance.Equals([string]$Health.instance_id, [System.StringComparison]::OrdinalIgnoreCase) -and
            ($Health.ready -eq $true -or $LocalReady)
        ) {
            Write-LifecycleTiming "platform.restart_task" "end" $LifecycleClock.Elapsed.TotalMilliseconds
            Add-Content -LiteralPath $LogPath -Encoding UTF8 -Value "Fixed HASHI restart completed; Backend ready on port $WorkbenchPort"
            exit 0
        }
    } catch {
        # Startup is expected to refuse health requests until the Backend binds.
    } finally {
        Write-LifecycleTiming "platform.health_poll" "end" ($LifecycleClock.Elapsed.TotalMilliseconds - $HealthStarted) $Attempt
    }
    $TaskQueryStarted = $LifecycleClock.Elapsed.TotalMilliseconds
    $RuntimeTask = Get-ScheduledTask -TaskName $RuntimeTaskName -ErrorAction Stop
    Write-LifecycleTiming "platform.task_status_query" "end" ($LifecycleClock.Elapsed.TotalMilliseconds - $TaskQueryStarted) $Attempt
    if ([string]$RuntimeTask.State -ne "Running") {
        $RuntimeInfo = Get-ScheduledTaskInfo -TaskName $RuntimeTaskName -ErrorAction SilentlyContinue
        if ($null -eq $RuntimeInfo -or [int64]$RuntimeInfo.LastTaskResult -ne 0) {
            Add-Content -LiteralPath $LogPath -Encoding UTF8 -Value "Fixed HASHI runtime task failed before readiness; result=$($RuntimeInfo.LastTaskResult)"
            exit 1
        }
    }
}

Add-Content -LiteralPath $LogPath -Encoding UTF8 -Value "Fixed HASHI restart timed out waiting for Backend readiness"
exit 1
