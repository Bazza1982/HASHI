param(
    [Parameter(Mandatory = $true)]
    [string]$HashiRoot,

    [string]$CodeRoot,

    [Parameter(Mandatory = $true)]
    [string]$LogPath,

    [Parameter(Mandatory = $true)]
    [string]$RuntimeTaskName,

    [string]$RequestPath
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
if (-not $RequestPath) {
    $RequestPath = Join-Path $ResolvedRoot "state\instance\cold-restart\request.json"
}
$RequestPath = [IO.Path]::GetFullPath($RequestPath)
$RootPrefix = $ResolvedRoot.TrimEnd('\') + '\'
if (-not $RequestPath.StartsWith($RootPrefix, [StringComparison]::OrdinalIgnoreCase)) {
    throw "Cold-restart request must remain inside the HASHI instance home"
}
if (-not (Test-Path -LiteralPath $RequestPath -PathType Leaf)) {
    throw "Missing cold-restart request: $RequestPath"
}
$Request = Get-Content -LiteralPath $RequestPath -Raw | ConvertFrom-Json
$RequiredRequestFields = @(
    "schema", "operation_id", "instance_id", "code_root", "hashi_root",
    "receipt_path", "deadline_epoch_ms"
)
$RequestFields = @($Request.PSObject.Properties.Name)
$MissingRequestFields = @($RequiredRequestFields | Where-Object { $_ -notin $RequestFields })
if ($MissingRequestFields.Count -ne 0 -or [int]$Request.schema -ne 1) {
    throw "Invalid cold-restart request schema"
}
$RequiredRequestFields | Where-Object { $_ -ne "schema" -and $_ -ne "deadline_epoch_ms" } |
    ForEach-Object {
        if ([string]::IsNullOrWhiteSpace([string]$Request.$_)) {
            throw "Invalid cold-restart request field: $_"
        }
    }
$OperationId = [string]$Request.operation_id
if ($OperationId -notmatch '^[a-fA-F0-9]{32}$') {
    throw "Invalid cold-restart operation id"
}
$ReceiptPath = [IO.Path]::GetFullPath([string]$Request.receipt_path)
if (-not $ReceiptPath.StartsWith($RootPrefix, [StringComparison]::OrdinalIgnoreCase)) {
    throw "Cold-restart receipt must remain inside the HASHI instance home"
}
$DeadlineEpochMs = [int64]$Request.deadline_epoch_ms
$RemainingMs = $DeadlineEpochMs - [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
if ($RemainingMs -le 0) {
    throw "Cold-restart request expired before task acceptance"
}
$AgentsPath = Join-Path $ResolvedRoot "agents.json"
if (-not (Test-Path -LiteralPath $AgentsPath -PathType Leaf)) {
    throw "Missing HASHI instance configuration: $AgentsPath"
}
$AgentsConfig = Get-Content -LiteralPath $AgentsPath -Raw | ConvertFrom-Json
$ExpectedInstance = [string]$AgentsConfig.global.instance_id
if (-not $ExpectedInstance) { throw "Missing HASHI instance identity" }
if (-not $ExpectedInstance.Equals([string]$Request.instance_id, [StringComparison]::OrdinalIgnoreCase)) {
    throw "Cold-restart request instance identity mismatch"
}
if (-not $ResolvedCodeRoot.TrimEnd('\').Equals(
        ([IO.Path]::GetFullPath([string]$Request.code_root)).TrimEnd('\'),
        [StringComparison]::OrdinalIgnoreCase)) {
    throw "Cold-restart request code root mismatch"
}
if (-not $ResolvedRoot.Equals(
        ([IO.Path]::GetFullPath([string]$Request.hashi_root)).TrimEnd('\'),
        [StringComparison]::OrdinalIgnoreCase)) {
    throw "Cold-restart request instance root mismatch"
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
            operation_id = $OperationId.ToLowerInvariant()
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

function Write-AtomicJson {
    param([string]$Path, $Value)
    $Parent = Split-Path -Parent $Path
    New-Item -ItemType Directory -Force -Path $Parent | Out-Null
    $Temporary = $Path + "." + [Guid]::NewGuid().ToString("N") + ".tmp"
    try {
        [IO.File]::WriteAllText(
            $Temporary,
            ($Value | ConvertTo-Json -Depth 8 -Compress),
            [Text.UTF8Encoding]::new($false)
        )
        Move-Item -LiteralPath $Temporary -Destination $Path -Force
    } finally {
        Remove-Item -LiteralPath $Temporary -Force -ErrorAction SilentlyContinue
    }
}

function Write-RestartReceipt {
    param(
        [string]$State,
        [string]$FailureStage = $null,
        [string]$ErrorText = $null,
        [hashtable]$Evidence = @{}
    )
    Write-AtomicJson -Path $ReceiptPath -Value ([ordered]@{
        schema = 1
        operation_id = $OperationId.ToLowerInvariant()
        instance_id = $ExpectedInstance
        state = $State
        failure_stage = $FailureStage
        error = $ErrorText
        runner_pid = $PID
        elapsed_ms = [math]::Round($LifecycleClock.Elapsed.TotalMilliseconds, 3)
        updated_at = [DateTimeOffset]::UtcNow.ToString("o")
        evidence = $Evidence
    })
}

function Test-DeadlineExpired {
    return $LifecycleClock.Elapsed.TotalMilliseconds -ge $RemainingMs
}
Write-LifecycleTiming "platform.initialize" "end" $LifecycleClock.Elapsed.TotalMilliseconds
Write-LifecycleTiming "platform.restart_task" "start"

# Keep one exact registered-task handle. Its State/LastTaskResult properties
# are live Scheduler RPC reads; do not reopen the CIM provider on every poll.
# Resolve read access before stopping any running process.
$TaskOpenStarted = $LifecycleClock.Elapsed.TotalMilliseconds
try {
    $Scheduler = New-Object -ComObject 'Schedule.Service'
    $Scheduler.Connect()
    $RuntimeTask = $Scheduler.GetFolder('\').GetTask($RuntimeTaskName)
} catch {
    Write-RestartReceipt -State "failed" -FailureStage "invalid_task_definition" -ErrorText $_.Exception.Message
    throw
}
Write-LifecycleTiming "platform.task_handle_open" "end" ($LifecycleClock.Elapsed.TotalMilliseconds - $TaskOpenStarted)
Write-RestartReceipt -State "task_accepted" -Evidence @{ runtime_task = $RuntimeTaskName }

$Timestamp = [DateTimeOffset]::Now.ToString("o")
Add-Content -LiteralPath $LogPath -Encoding UTF8 -Value "$Timestamp fixed restart task accepted operation $OperationId for $ResolvedRoot"

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
    $Message = "Existing HASHI process failed to stop; controller exit code $Result"
    Write-RestartReceipt -State "failed" -FailureStage "existing_process_failed_to_stop" -ErrorText $Message
    Add-Content -LiteralPath $LogPath -Encoding UTF8 -Value $Message
    exit $Result
}
if (Test-DeadlineExpired) {
    Write-RestartReceipt -State "failed" -FailureStage "existing_process_failed_to_stop" -ErrorText "Cold-restart deadline expired while stopping the old process"
    exit 1
}

$TaskSettleStarted = $LifecycleClock.Elapsed.TotalMilliseconds
$RuntimeState = [int]$RuntimeTask.State
for ($Attempt = 0; $Attempt -lt 20 -and $RuntimeState -eq 4 -and -not (Test-DeadlineExpired); $Attempt++) {
    Start-Sleep -Milliseconds 250
    $RuntimeState = [int]$RuntimeTask.State
}
if ($RuntimeState -eq 4) {
    Stop-ScheduledTask -TaskName $RuntimeTaskName -ErrorAction Stop
    for ($Attempt = 0; $Attempt -lt 20 -and -not (Test-DeadlineExpired); $Attempt++) {
        Start-Sleep -Milliseconds 250
        $RuntimeState = [int]$RuntimeTask.State
        if ($RuntimeState -ne 4) {
            break
        }
    }
}
if ($RuntimeState -eq 4) {
    $Message = "Fixed HASHI runtime task did not stop cleanly"
    Write-RestartReceipt -State "failed" -FailureStage "existing_process_failed_to_stop" -ErrorText $Message
    Add-Content -LiteralPath $LogPath -Encoding UTF8 -Value $Message
    exit 1
}

Write-LifecycleTiming "platform.task_stop_settle" "end" ($LifecycleClock.Elapsed.TotalMilliseconds - $TaskSettleStarted)
$TaskStartStarted = $LifecycleClock.Elapsed.TotalMilliseconds
try {
    Start-ScheduledTask -TaskName $RuntimeTaskName -ErrorAction Stop
} catch {
    Write-RestartReceipt -State "failed" -FailureStage "launcher_rejected" -ErrorText $_.Exception.Message
    throw
}
Write-LifecycleTiming "platform.start_registered_task" "end" ($LifecycleClock.Elapsed.TotalMilliseconds - $TaskStartStarted)
Add-Content -LiteralPath $LogPath -Encoding UTF8 -Value "Triggered fixed HASHI runtime task $RuntimeTaskName for operation $OperationId"

# Wait only for runtime launch acceptance. Remote owns readiness and its one
# transaction deadline; this runner must not add a second multi-minute loop.
for ($Attempt = 0; -not (Test-DeadlineExpired); $Attempt++) {
    Start-Sleep -Milliseconds 100
    if (Test-Path -LiteralPath $ReceiptPath -PathType Leaf) {
        try {
            $Receipt = Get-Content -LiteralPath $ReceiptPath -Raw | ConvertFrom-Json
            if ([string]$Receipt.operation_id -eq $OperationId.ToLowerInvariant()) {
                if (-not $ExpectedInstance.Equals([string]$Receipt.instance_id, [StringComparison]::OrdinalIgnoreCase)) {
                    $Message = "Runtime launch receipt instance identity mismatch"
                    Write-RestartReceipt -State "failed" -FailureStage "launcher_rejected" -ErrorText $Message
                    Add-Content -LiteralPath $LogPath -Encoding UTF8 -Value $Message
                    exit 1
                }
                if ([string]$Receipt.state -eq "launch_accepted") {
                    Write-LifecycleTiming "platform.restart_task" "end" $LifecycleClock.Elapsed.TotalMilliseconds
                    Add-Content -LiteralPath $LogPath -Encoding UTF8 -Value "Fixed HASHI runtime launch accepted for operation $OperationId"
                    exit 0
                }
                if ([string]$Receipt.state -eq "failed") {
                    Add-Content -LiteralPath $LogPath -Encoding UTF8 -Value "Fixed HASHI runtime launch failed at $($Receipt.failure_stage): $($Receipt.error)"
                    exit 1
                }
            }
        } catch {}
    }
    $TaskQueryStarted = $LifecycleClock.Elapsed.TotalMilliseconds
    $RuntimeState = [int]$RuntimeTask.State
    Write-LifecycleTiming "platform.task_status_query" "end" ($LifecycleClock.Elapsed.TotalMilliseconds - $TaskQueryStarted) $Attempt
    if ($RuntimeState -ne 4) {
        $RuntimeResult = [int64]$RuntimeTask.LastTaskResult
        if ($RuntimeResult -ne 0) {
            $Message = "Fixed HASHI runtime task rejected launch; result=$RuntimeResult"
            Write-RestartReceipt -State "failed" -FailureStage "launcher_rejected" -ErrorText $Message
            Add-Content -LiteralPath $LogPath -Encoding UTF8 -Value $Message
            exit 1
        }
    }
}

$Message = "Cold-restart deadline expired before runtime launch acceptance"
Write-RestartReceipt -State "failed" -FailureStage "new_process_failed_to_start" -ErrorText $Message
Add-Content -LiteralPath $LogPath -Encoding UTF8 -Value $Message
exit 1
