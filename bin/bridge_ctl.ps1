<#
.SYNOPSIS
    Unified bridge-u-f process management script.
    Replaces kill_bridge_u_f_sessions.bat and restart_bridge_u_f.bat with a more robust PowerShell implementation.

.DESCRIPTION
    Provides start/stop/restart/status operations for bridge-u-f with proper process management,
    lock file handling, and graceful shutdown support.

.PARAMETER Action
    One of: start, stop, restart, status, kill

.PARAMETER Quiet
    Suppress most output

.PARAMETER Force
    Skip graceful shutdown, go straight to taskkill

.PARAMETER Resume
    Resume the saved Agent selection when starting

.EXAMPLE
    .\bridge_ctl.ps1 status
    .\bridge_ctl.ps1 stop
    .\bridge_ctl.ps1 restart -Resume
    .\bridge_ctl.ps1 kill -Force
#>

param(
    [Parameter(Position=0)]
    [ValidateSet("start", "stop", "restart", "status", "kill")]
    [string]$Action = "status",
    
    [switch]$Quiet,
    [switch]$Force,
    [switch]$Resume,
    [switch]$ApiGateway,
    [string]$BridgeHome,
    [string]$RuntimeDataRoot,
    [string]$StartupRequestPath
)

$ErrorActionPreference = "Stop"
if ($RuntimeDataRoot) {
    if (-not [IO.Path]::IsPathRooted($RuntimeDataRoot)) {
        throw "RuntimeDataRoot must be an absolute installation catalogue path."
    }
    # Task Scheduler does not inherit the registering Remote's environment.
    # This retains its catalogue; role and integrity qualification still apply.
    $env:HASHI_DATA_ROOT = [IO.Path]::GetFullPath($RuntimeDataRoot)
}

# Constants
$ScriptDir = ([System.IO.Path]::GetFullPath($PSScriptRoot)).TrimEnd('\')
$ProjectDir = [System.IO.Path]::GetFullPath((Join-Path $ScriptDir "..")).TrimEnd('\')
if (-not $BridgeHome) {
    $BridgeHome = if ($env:BRIDGE_HOME) { $env:BRIDGE_HOME } else { $ProjectDir }
}
$BridgeHome = ([System.IO.Path]::GetFullPath($BridgeHome)).TrimEnd('\')
$MainScript = Join-Path $ProjectDir "main.py"
$AgentsJson = Join-Path $BridgeHome "agents.json"
if (-not (Test-Path $AgentsJson)) { throw "Missing HASHI instance configuration: $AgentsJson" }
$SecretsJson = Join-Path $BridgeHome "secrets.json"
# Never borrow another instance's credentials from the program directory.
$WorkbenchPort = 18800
$PythonExe = if (Test-Path (Join-Path $ProjectDir "python\python.exe")) {
    Join-Path $ProjectDir "python\python.exe"
} elseif (Test-Path (Join-Path $ProjectDir ".venv\Scripts\python.exe")) {
    Join-Path $ProjectDir ".venv\Scripts\python.exe"
} else {
    "python"
}
$PathResolver = Join-Path $ProjectDir "scripts\resolve_instance_runtime.py"
try {
    $LockFile = (& $PythonExe -B $PathResolver --code-root $ProjectDir --bridge-home $BridgeHome --field lock-path).Trim()
    $PidFile = (& $PythonExe -B $PathResolver --code-root $ProjectDir --bridge-home $BridgeHome --field pid-path).Trim()
    $InstanceId = (& $PythonExe -B $PathResolver --code-root $ProjectDir --bridge-home $BridgeHome --field instance-id).Trim()
    if (-not $LockFile -or -not $PidFile) {
        throw "empty instance runtime path"
    }
} catch {
    throw "Could not resolve instance-scoped process paths: $($_.Exception.Message)"
}

# Read workbench port from config
try {
    $cfg = Get-Content $AgentsJson -Raw | ConvertFrom-Json
    if ($cfg.global.workbench_port) {
        $WorkbenchPort = [int]$cfg.global.workbench_port
    }
} catch {}

# Read admin token from secrets
$AdminToken = ""
try {
    if (Test-Path $SecretsJson) {
        $secrets = Get-Content $SecretsJson -Raw | ConvertFrom-Json
        if ($secrets.workbench_admin_token) {
            $AdminToken = $secrets.workbench_admin_token
        }
    }
} catch {}

function Write-Log {
    param([string]$Message, [string]$Level = "INFO")
    if (-not $Quiet) {
        $color = switch ($Level) {
            "ERROR" { "Red" }
            "WARN"  { "Yellow" }
            "OK"    { "Green" }
            default { "White" }
        }
        Write-Host $Message -ForegroundColor $color
    }
}

function Test-BridgeCommandLine {
    param([string]$CommandLine)

    if ([string]::IsNullOrWhiteSpace($CommandLine)) {
        return $false
    }

    $cmd = $CommandLine
    if ($cmd -match '(?i)--bridge-home\s+("([^"]+)"|(\S+))') {
        $candidate = if ($matches[2]) { $matches[2] } else { $matches[3] }
        try {
            $resolved = ([System.IO.Path]::GetFullPath($candidate)).TrimEnd('\')
        } catch {
            $resolved = $candidate.TrimEnd('\')
        }
        return $resolved.Equals($BridgeHome, [System.StringComparison]::OrdinalIgnoreCase)
    }

    # A shared code root can host several bridge homes. Without an explicit
    # bridge-home argument this controller cannot prove process ownership.
    return $false
}

function Get-BridgeProcesses {
    <#
    .DESCRIPTION
        Find Python bridge processes belonging to this exact bridge home.
    #>
    $selfPid = $PID
    $parentPid = (Get-CimInstance Win32_Process -Filter "ProcessId = $selfPid" -ErrorAction SilentlyContinue).ParentProcessId
    
    $targets = @{}
    $allProcs = Get-CimInstance Win32_Process
    
    # Build parent-child map
    $children = @{}
    foreach ($p in $allProcs) {
        $procId = [int]$p.ProcessId
        $parentId = [int]$p.ParentProcessId
        if (-not $children.ContainsKey($parentId)) {
            $children[$parentId] = @()
        }
        $children[$parentId] += $procId
    }
    
    # Find root bridge processes
    foreach ($p in $allProcs) {
        $procId = [int]$p.ProcessId
        if ($procId -eq $selfPid -or $procId -eq $parentPid) { continue }
        
        $name = [string]$p.Name
        $cmd = [string]$p.CommandLine
        
        # Python running main.py from this project
        if (($name -ieq 'python.exe' -or $name -ieq 'py.exe') -and (Test-BridgeCommandLine $cmd)) {
            $targets[$procId] = @{ Name = $name; Cmd = $cmd; Type = "python" }
            continue
        }
        
    }
    
    # Expand to include all children (recursively)
    $queue = [System.Collections.Queue]::new()
    foreach ($procId in $targets.Keys) { $queue.Enqueue($procId) }
    
    while ($queue.Count -gt 0) {
        $current = $queue.Dequeue()
        if ($children.ContainsKey($current)) {
            foreach ($child in $children[$current]) {
                if ($child -ne $selfPid -and $child -ne $parentPid -and -not $targets.ContainsKey($child)) {
                    $p = $allProcs | Where-Object { $_.ProcessId -eq $child } | Select-Object -First 1
                    if ($p) {
                        $childName = [string]$p.Name
                        $childCmd = [string]$p.CommandLine
                        $isBridgeChild =
                            ($childName -ieq 'python.exe' -or $childName -ieq 'py.exe') -and (Test-BridgeCommandLine $childCmd)
                        $isConsoleChild = $childName -ieq 'conhost.exe'
                        if ($isBridgeChild -or $isConsoleChild) {
                            $targets[$child] = @{ Name = $p.Name; Cmd = $p.CommandLine; Type = "child" }
                            $queue.Enqueue($child)
                        }
                    }
                }
            }
        }
    }
    
    return $targets
}

function Test-LockFileHeld {
    <#
    .DESCRIPTION
        Check if the lock file is currently held by a process (i.e., locked).
        Returns $true if locked, $false if not locked or doesn't exist.
    #>
    if (-not (Test-Path $LockFile)) {
        return $false
    }
    
    try {
        # Try to open with exclusive access - if it fails, file is locked
        $fs = [System.IO.File]::Open($LockFile, 'Open', 'ReadWrite', 'None')
        $fs.Close()
        return $false  # File opened successfully, so it's NOT locked
    } catch {
        return $true   # File is locked
    }
}

function Get-BridgePid {
    <#
    .DESCRIPTION
        Get the PID of the running bridge from the PID file.
        Returns $null if not available or stale.
    #>
    if (-not (Test-Path $PidFile)) {
        return $null
    }
    
    $pidStr = (Get-Content $PidFile -Raw).Trim()
    if (-not ($pidStr -match '^\d+$')) {
        return $null
    }
    
    $pidVal = [int]$pidStr
    $proc = Get-Process -Id $pidVal -ErrorAction SilentlyContinue
    if (-not $proc) {
        return $null  # Stale PID
    }
    
    return $pidVal
}

function Get-ApprovedCoreProcessImages {
    <#
    .DESCRIPTION
        Return the executable images that an installer-qualified Windows venv
        launcher may use for Core.  CPython's venv launcher can retain the
        selected Scripts\python.exe as a parent while the sealed base
        python.exe executes the program, so both identities are explicit.
    #>
    param([Parameter(Mandatory=$true)][string]$SelectedExecutable)

    $Images = [System.Collections.Generic.List[string]]::new()
    $Selected = [IO.Path]::GetFullPath($SelectedExecutable)
    $Images.Add($Selected)
    $EnvironmentRoot = Split-Path -Parent (Split-Path -Parent $Selected)
    $Config = Join-Path $EnvironmentRoot "pyvenv.cfg"
    if (Test-Path -LiteralPath $Config -PathType Leaf) {
        $RuntimeBaseHome = $null
        foreach ($Line in Get-Content -LiteralPath $Config) {
            $Parts = @(([string]$Line) -split '=', 2)
            if ($Parts.Count -eq 2 -and $Parts[0].Trim().Equals(
                    "home", [StringComparison]::OrdinalIgnoreCase)) {
                $RuntimeBaseHome = $Parts[1].Trim()
                break
            }
        }
        if ($RuntimeBaseHome -and [IO.Path]::IsPathRooted($RuntimeBaseHome)) {
            $BaseExecutable = [IO.Path]::GetFullPath((Join-Path $RuntimeBaseHome "python.exe"))
            if (Test-Path -LiteralPath $BaseExecutable -PathType Leaf) {
                $Images.Add($BaseExecutable)
            }
        }
    }
    return @($Images | Select-Object -Unique)
}

function Test-CoreLaunchProcess {
    <#
    .DESCRIPTION
        Prove that the PID published by Core is either the selected launcher
        itself or its direct CPython-venv child, and that its image belongs to
        the already-qualified runtime.  A controller/PowerShell PID or an
        unrelated Python process can never satisfy this receipt boundary.
    #>
    param(
        [Parameter(Mandatory=$true)][int]$CandidatePid,
        [Parameter(Mandatory=$true)][int]$LauncherPid,
        [Parameter(Mandatory=$true)][string[]]$ApprovedImages
    )

    $Process = Get-CimInstance Win32_Process -Filter "ProcessId = $CandidatePid" -ErrorAction SilentlyContinue
    if ($null -eq $Process -or [string]::IsNullOrWhiteSpace([string]$Process.ExecutablePath)) {
        return $false
    }
    if ($CandidatePid -ne $LauncherPid -and [int]$Process.ParentProcessId -ne $LauncherPid) {
        return $false
    }
    try {
        $ObservedImage = [IO.Path]::GetFullPath([string]$Process.ExecutablePath)
    } catch {
        return $false
    }
    foreach ($ApprovedImage in $ApprovedImages) {
        if ($ObservedImage.Equals(
                [IO.Path]::GetFullPath([string]$ApprovedImage),
                [StringComparison]::OrdinalIgnoreCase)) {
            return $true
        }
    }
    return $false
}

function Invoke-GracefulShutdown {
    <#
    .DESCRIPTION
        Request graceful shutdown via the admin API.
        Returns $true if successful, $false otherwise.
    #>
    param([int]$TimeoutSeconds = 30)
    
    $url = "http://127.0.0.1:$WorkbenchPort/api/admin/shutdown"
    $headers = @{ "Content-Type" = "application/json" }
    if ($AdminToken) {
        $headers["X-Workbench-Token"] = $AdminToken
    }
    
    try {
        $body = '{"reason":"bridge_ctl"}'
        $response = Invoke-RestMethod -Uri $url -Method Post -Headers $headers -Body $body -TimeoutSec 5
        Write-Log "Shutdown API accepted. Waiting for process exit..."
        
        # Wait for processes to exit
        for ($i = 0; $i -lt $TimeoutSeconds; $i++) {
            Start-Sleep -Seconds 1
            $procs = Get-BridgeProcesses
            if ($procs.Count -eq 0) {
                return $true
            }
        }
        
        Write-Log "Graceful shutdown timed out after ${TimeoutSeconds}s" -Level "WARN"
        return $false
        
    } catch {
        Write-Log "Shutdown API not available: $($_.Exception.Message)" -Level "WARN"
        return $false
    }
}

function Stop-BridgeProcesses {
    <#
    .DESCRIPTION
        Stop all bridge-u-f processes. Try graceful first, then force.
    #>
    param([switch]$ForceImmediate)
    
    $procs = Get-BridgeProcesses
    if ($procs.Count -eq 0) {
        Write-Log "No bridge-u-f processes running."
        return $true
    }
    
    Write-Log "Found $($procs.Count) bridge-related processes."
    
    # Try graceful shutdown first (unless Force specified)
    if (-not $ForceImmediate) {
        $graceful = Invoke-GracefulShutdown -TimeoutSeconds 15
        if ($graceful) {
            Write-Log "Graceful shutdown completed." -Level "OK"
            return $true
        }
    }
    
    # Force kill
    Write-Log "Force killing processes..."
    $procs = Get-BridgeProcesses  # Refresh list
    $killFailures = @()
    foreach ($procId in $procs.Keys) {
        $info = $procs[$procId]
        Write-Log "  Killing PID $procId ($($info.Name))"
        try {
            Stop-Process -Id $procId -Force -ErrorAction Stop
        } catch {
            $StillAlive = Get-Process -Id $procId -ErrorAction SilentlyContinue
            if ($StillAlive) {
                $killFailures += "PID $procId ($($_.Exception.Message))"
                Write-Log "  Failed to kill PID $procId`: $($_.Exception.Message)" -Level "ERROR"
            } else {
                Write-Log "  PID $procId exited before forced termination completed."
            }
        }
    }
    
    Start-Sleep -Seconds 2
    
    # Verify
    $remaining = Get-BridgeProcesses
    if ($remaining.Count -gt 0) {
        Write-Log "WARNING: $($remaining.Count) processes still running after kill" -Level "WARN"
        return $false
    }
    if ($killFailures.Count -gt 0) {
        Write-Log "Process termination reported errors: $($killFailures -join '; ')" -Level "ERROR"
        return $false
    }
    
    Write-Log "All processes stopped." -Level "OK"
    return $true
}

function Remove-StaleFiles {
    <#
    .DESCRIPTION
        Remove this instance's stale PID file if no bridge process is running.
    #>
    $procs = Get-BridgeProcesses
    if ($procs.Count -gt 0) {
        Write-Log "Cannot remove files - bridge processes still running" -Level "WARN"
        return
    }
    
    if (Test-Path $PidFile) {
        Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
        Write-Log "Removed stale PID file for $InstanceId"
    }
}

function Get-ResumeAgentNames {
    param([switch]$ResumeMode)

    if (-not $ResumeMode) {
        return @()
    }
    $StateFile = Join-Path $BridgeHome ".bridge_u_last_agents.txt"
    if (-not (Test-Path -LiteralPath $StateFile -PathType Leaf)) {
        return @()
    }
    $StateLine = [string](Get-Content -LiteralPath $StateFile -TotalCount 1)
    $StateParts = $StateLine -split '\|', 2
    if ($StateParts.Count -lt 2 -or $StateParts[0] -ine "selected") {
        return @()
    }
    $Requested = @($StateParts[1] -split '\s+' | Where-Object { $_ })
    $Configured = @(
        (Get-Content -LiteralPath $AgentsJson -Raw | ConvertFrom-Json).agents |
            Where-Object { $_.is_active -ne $false } |
            ForEach-Object { [string]$_.name }
    )
    $Invalid = @($Requested | Where-Object { $_ -notin $Configured })
    if ($Invalid.Count -gt 0) {
        throw "Saved Agent selection contains unknown or inactive names: $($Invalid -join ', ')"
    }
    return $Requested
}

function Test-ApiGatewayWasEnabled {
    param($Processes)

    foreach ($Process in $Processes.Values) {
        if ([string]$Process.Cmd -match '(?i)(?:^|\s)--api-gateway(?:\s|$)') {
            return $true
        }
    }
    return $false
}

function ConvertTo-NativeArgument {
    param([string]$Value)

    if ($Value -notmatch '[\s"]') {
        return $Value
    }
    return '"' + ($Value -replace '(\\*)"', '$1$1\"' -replace '(\\+)$', '$1$1') + '"'
}

function Get-StartupRequest {
    if (-not $StartupRequestPath) { return $null }
    $ResolvedRequest = [IO.Path]::GetFullPath($StartupRequestPath)
    $HomePrefix = $BridgeHome.TrimEnd('\') + '\'
    if (-not $ResolvedRequest.StartsWith($HomePrefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Startup request must remain inside the HASHI instance home"
    }
    if (-not (Test-Path -LiteralPath $ResolvedRequest -PathType Leaf)) { return $null }
    $Request = Get-Content -LiteralPath $ResolvedRequest -Raw | ConvertFrom-Json
    $RequiredRequestFields = @(
        "schema", "operation_id", "instance_id", "code_root", "hashi_root",
        "receipt_path", "deadline_epoch_ms"
    )
    $RequestFields = @($Request.PSObject.Properties.Name)
    $MissingRequestFields = @($RequiredRequestFields | Where-Object { $_ -notin $RequestFields })
    if ($MissingRequestFields.Count -ne 0 -or [int]$Request.schema -ne 1) {
        throw "Invalid startup request schema"
    }
    $RequiredRequestFields | Where-Object { $_ -ne "schema" -and $_ -ne "deadline_epoch_ms" } |
        ForEach-Object {
            if ([string]::IsNullOrWhiteSpace([string]$Request.$_)) {
                throw "Invalid startup request field: $_"
            }
        }
    if ([string]$Request.operation_id -notmatch '^[a-fA-F0-9]{32}$') {
        throw "Invalid startup operation id"
    }
    if (-not $InstanceId.Equals([string]$Request.instance_id, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Startup request instance identity mismatch"
    }
    if (-not $ProjectDir.Equals(
            ([IO.Path]::GetFullPath([string]$Request.code_root)).TrimEnd('\'),
            [StringComparison]::OrdinalIgnoreCase)) {
        throw "Startup request code root mismatch"
    }
    if (-not $BridgeHome.Equals(
            ([IO.Path]::GetFullPath([string]$Request.hashi_root)).TrimEnd('\'),
            [StringComparison]::OrdinalIgnoreCase)) {
        throw "Startup request instance root mismatch"
    }
    if ([int64]$Request.deadline_epoch_ms -le [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()) {
        Remove-Item -LiteralPath $ResolvedRequest -Force -ErrorAction SilentlyContinue
        return $null
    }
    $ReceiptPath = [IO.Path]::GetFullPath([string]$Request.receipt_path)
    if (-not $ReceiptPath.StartsWith($HomePrefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Startup receipt must remain inside the HASHI instance home"
    }
    $Request | Add-Member -NotePropertyName resolved_receipt_path -NotePropertyValue $ReceiptPath -Force
    # The fixed restart runner already loaded this transaction. Consume the
    # shared request before product launch so a later logon/start cannot be
    # rejected by stale cold-restart state.
    Remove-Item -LiteralPath $ResolvedRequest -Force -ErrorAction Stop
    return $Request
}

function Write-StartupReceipt {
    param(
        $Request,
        [string]$State,
        [string]$FailureStage = $null,
        [string]$ErrorText = $null,
        [hashtable]$Evidence = @{}
    )
    if ($null -eq $Request) { return }
    $Path = [string]$Request.resolved_receipt_path
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Path) | Out-Null
    $Value = [ordered]@{
        schema = 1
        operation_id = ([string]$Request.operation_id).ToLowerInvariant()
        instance_id = $InstanceId
        state = $State
        failure_stage = $FailureStage
        error = $ErrorText
        launcher_pid = $PID
        updated_at = [DateTimeOffset]::UtcNow.ToString("o")
        evidence = $Evidence
    }
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

function Start-Bridge {
    <#
    .DESCRIPTION
        Start HASHI directly through its fixed Python entry point.
    #>
    param(
        [switch]$ResumeMode,
        [switch]$ApiGateway
    )
    
    $StartupRequest = Get-StartupRequest

    # Check if already running
    $procs = Get-BridgeProcesses
    if ($procs.Count -gt 0) {
        Write-Log "Bridge-u-f is already running (found $($procs.Count) processes)" -Level "WARN"
        Write-StartupReceipt -Request $StartupRequest -State "failed" `
            -FailureStage "new_process_failed_to_start" `
            -ErrorText "A HASHI Core process already owns this instance"
        return $false
    }
    
    # Check lock file
    if (Test-LockFileHeld) {
        Write-Log "Lock file is held but no processes found - this is unexpected" -Level "WARN"
    }
    
    # Clean up any stale files
    Remove-StaleFiles
    
    $RoleResolver = Join-Path $ProjectDir "scripts\resolve_launch_runtime.py"
    $CorePythonExe = (& $PythonExe -B $RoleResolver --code-root $ProjectDir --field core-executable)
    if ($LASTEXITCODE -ne 0 -or -not $CorePythonExe) {
        Write-StartupReceipt -Request $StartupRequest -State "failed" `
            -FailureStage "runtime_qualification_failed" `
            -ErrorText "Runtime setup is incomplete; install the verified release before starting."
        throw "Runtime setup is incomplete; install the verified release before starting."
    }
    $CorePythonExe = ([string]$CorePythonExe).Trim()
    $ApprovedCoreImages = @(Get-ApprovedCoreProcessImages -SelectedExecutable $CorePythonExe)
    $LaunchArguments = @(
        "-I", "-B",
        $MainScript,
        "--bridge-home", $BridgeHome
    )
    $ResumeAgents = @(Get-ResumeAgentNames -ResumeMode:$ResumeMode)
    if ($ResumeMode -and $ResumeAgents.Count -gt 0) {
        $LaunchArguments += "--agents"
        $LaunchArguments += $ResumeAgents
    }
    if ($ApiGateway) {
        $LaunchArguments += "--api-gateway"
    }
    
    Write-Log "Starting bridge-u-f..."
    
    # Start the fixed entry point. The controller can exit while the child
    # remains owned by the logged-in user's elevated scheduled-task session.
    try {
        $LaunchLogDir = Join-Path $BridgeHome "logs"
        New-Item -ItemType Directory -Force -Path $LaunchLogDir | Out-Null
        $LaunchStamp = [DateTimeOffset]::Now.ToString("yyyyMMdd_HHmmssfff")
        $StdoutLog = Join-Path $LaunchLogDir "core_restart_$LaunchStamp.stdout.log"
        $StderrLog = Join-Path $LaunchLogDir "core_restart_$LaunchStamp.stderr.log"
        $ArgumentString = ($LaunchArguments | ForEach-Object {
            ConvertTo-NativeArgument -Value ([string]$_)
        }) -join ' '
        $env:BRIDGE_HOME = $BridgeHome
        $env:PYTHONUTF8 = "1"
        $env:PYTHONIOENCODING = "utf-8"
        $env:PYTHONPATH = $ProjectDir
        $env:HASHI_ENABLE_LEGACY_FIXED_RUNTIME = "1"
        $Launcher = Start-Process `
            -FilePath $CorePythonExe `
            -ArgumentList $ArgumentString `
            -WorkingDirectory $ProjectDir `
            -WindowStyle Hidden `
            -RedirectStandardOutput $StdoutLog `
            -RedirectStandardError $StderrLog `
            -PassThru

        # A cold restart is accepted only after the selected Core (or the
        # selected Windows venv launcher's direct sealed-base child) acquires
        # the instance lock and publishes its own PID file. Merely creating
        # this PowerShell or Python launcher is not acceptance.
        $LaunchDeadlineEpochMs = if ($null -ne $StartupRequest) {
            [int64]$StartupRequest.deadline_epoch_ms
        } else {
            [DateTimeOffset]::UtcNow.AddSeconds(7.5).ToUnixTimeMilliseconds()
        }
        while ([DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() -lt $LaunchDeadlineEpochMs) {
            $procs = Get-BridgeProcesses
            if ($null -ne $StartupRequest) {
                $BridgePid = Get-BridgePid
                if ($null -ne $BridgePid -and $procs.ContainsKey($BridgePid) `
                        -and (Test-LockFileHeld) `
                        -and (Test-CoreLaunchProcess -CandidatePid $BridgePid `
                            -LauncherPid ([int]$Launcher.Id) -ApprovedImages $ApprovedCoreImages)) {
                    Write-StartupReceipt -Request $StartupRequest -State "launch_accepted" `
                        -Evidence @{
                            core_pid = $BridgePid
                            launcher_process_id = [int]$Launcher.Id
                            core_process_image = [string](
                                (Get-CimInstance Win32_Process -Filter "ProcessId = $BridgePid").ExecutablePath
                            )
                            stdout = $StdoutLog
                            stderr = $StderrLog
                            runtime_executable = $CorePythonExe
                        }
                    Write-Log "Bridge-u-f launch accepted by Core PID $BridgePid" -Level "OK"
                    return $true
                }
            } elseif ($procs.Count -gt 0) {
                Write-Log "Bridge-u-f started ($($procs.Count) processes)" -Level "OK"
                return $true
            }
            if ($Launcher.HasExited) {
                $Message = "Bridge-u-f launcher exited with code $($Launcher.ExitCode). stdout=$StdoutLog stderr=$StderrLog"
                Write-StartupReceipt -Request $StartupRequest -State "failed" `
                    -FailureStage "launcher_rejected" -ErrorText $Message `
                    -Evidence @{ launcher_exit_code = [int]$Launcher.ExitCode; stdout = $StdoutLog; stderr = $StderrLog }
                Write-Log $Message -Level "ERROR"
                return $false
            }
            Start-Sleep -Milliseconds 250
        }

        $Message = "Bridge-u-f did not acquire the instance lock before the launch deadline. stdout=$StdoutLog stderr=$StderrLog"
        Write-StartupReceipt -Request $StartupRequest -State "failed" `
            -FailureStage "new_process_failed_to_start" -ErrorText $Message `
            -Evidence @{ launcher_process_id = [int]$Launcher.Id; stdout = $StdoutLog; stderr = $StderrLog }
        Write-Log $Message -Level "ERROR"
        return $false
        
    } catch {
        Write-StartupReceipt -Request $StartupRequest -State "failed" `
            -FailureStage "launcher_rejected" -ErrorText $_.Exception.Message
        Write-Log "Failed to start bridge-u-f: $($_.Exception.Message)" -Level "ERROR"
        return $false
    }
}

function Show-Status {
    <#
    .DESCRIPTION
        Show current status of bridge-u-f.
    #>
    Write-Host "`n=== Bridge-U-F Status ===" -ForegroundColor Cyan
    
    # Check processes
    $procs = Get-BridgeProcesses
    if ($procs.Count -gt 0) {
        Write-Host "Processes: $($procs.Count) running" -ForegroundColor Green
        foreach ($procId in $procs.Keys) {
            $info = $procs[$procId]
            Write-Host "  PID $procId : $($info.Name) [$($info.Type)]"
        }
    } else {
        Write-Host "Processes: none running" -ForegroundColor Yellow
    }
    
    # Check lock file
    Write-Host "`nLock file: " -NoNewline
    if (Test-Path $LockFile) {
        if (Test-LockFileHeld) {
            Write-Host "exists and LOCKED" -ForegroundColor Green
        } else {
            Write-Host "exists but NOT locked (stale)" -ForegroundColor Yellow
        }
    } else {
        Write-Host "does not exist" -ForegroundColor $(if ($procs.Count -gt 0) { "Yellow" } else { "Gray" })
    }
    
    # Check PID file
    Write-Host "PID file: " -NoNewline
    $bridgePid = Get-BridgePid
    if ($bridgePid) {
        Write-Host "PID $bridgePid (alive)" -ForegroundColor Green
    } elseif (Test-Path $PidFile) {
        Write-Host "exists but stale" -ForegroundColor Yellow
    } else {
        Write-Host "does not exist" -ForegroundColor $(if ($procs.Count -gt 0) { "Yellow" } else { "Gray" })
    }
    
    # Check API
    Write-Host "`nWorkbench API (port $WorkbenchPort): " -NoNewline
    try {
        $response = Invoke-RestMethod -Uri "http://127.0.0.1:$WorkbenchPort/api/health" -TimeoutSec 3
        Write-Host "healthy ($($response.agents.Count) agents)" -ForegroundColor Green
    } catch {
        Write-Host "not responding" -ForegroundColor $(if ($procs.Count -gt 0) { "Yellow" } else { "Gray" })
    }
    
    Write-Host ""
}

# Main dispatch
switch ($Action) {
    "status" {
        Show-Status
    }
    "stop" {
        $success = Stop-BridgeProcesses -ForceImmediate:$Force
        if ($success) {
            Remove-StaleFiles
        }
        exit $(if ($success) { 0 } else { 1 })
    }
    "kill" {
        # Kill is always forced
        $success = Stop-BridgeProcesses -ForceImmediate
        Remove-StaleFiles
        exit $(if ($success) { 0 } else { 1 })
    }
    "start" {
        $success = Start-Bridge -ResumeMode:$Resume -ApiGateway:$ApiGateway
        exit $(if ($success) { 0 } else { 1 })
    }
    "restart" {
        Write-Log "=== Restarting bridge-u-f ==="
        $RestartProcesses = Get-BridgeProcesses
        $RestartApiGateway = $ApiGateway -or (Test-ApiGatewayWasEnabled -Processes $RestartProcesses)
        $stopOk = Stop-BridgeProcesses -ForceImmediate:$Force
        if ($stopOk) {
            Remove-StaleFiles
            Start-Sleep -Seconds 2
            $startOk = Start-Bridge -ResumeMode:$Resume -ApiGateway:$RestartApiGateway
            exit $(if ($startOk) { 0 } else { 1 })
        } else {
            Write-Log "Failed to stop existing instance" -Level "ERROR"
            exit 1
        }
    }
}
