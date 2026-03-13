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
    Pass --resume-last to bridge-u.bat when starting

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
    [switch]$Resume
)

$ErrorActionPreference = "Stop"

# Constants
$ProjectDir = $PSScriptRoot
$BridgeHome = if ($env:BRIDGE_HOME) { $env:BRIDGE_HOME } else { $ProjectDir }
$LockFile = Join-Path $BridgeHome ".bridge_u_f.lock"
$PidFile = Join-Path $BridgeHome ".bridge_u_f.pid"
$LauncherBat = Join-Path $ProjectDir "bridge-u.bat"
$AgentsJson = Join-Path $BridgeHome "agents.json"
if (-not (Test-Path $AgentsJson)) { $AgentsJson = Join-Path $ProjectDir "agents.json" }
$SecretsJson = Join-Path $BridgeHome "secrets.json"
if (-not (Test-Path $SecretsJson)) { $SecretsJson = Join-Path $ProjectDir "secrets.json" }
$WorkbenchPort = 18800

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

function Get-BridgeProcesses {
    <#
    .DESCRIPTION
        Find all bridge-u-f related processes.
        Returns processes for: python main.py, cmd bridge-u.bat, and port listeners.
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
        if (($name -ieq 'python.exe' -or $name -ieq 'py.exe') -and $cmd -and $cmd -like "*$ProjectDir*main.py*") {
            $targets[$procId] = @{ Name = $name; Cmd = $cmd; Type = "python" }
            continue
        }
        
        # cmd.exe running bridge-u.bat
        if ($name -ieq 'cmd.exe' -and $cmd -and $cmd -like "*$ProjectDir*bridge-u.bat*") {
            $targets[$procId] = @{ Name = $name; Cmd = $cmd; Type = "launcher" }
            continue
        }
    }
    
    # Add port listeners (workbench API)
    foreach ($port in $WorkbenchPort, 18801) {
        try {
            $conns = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
            foreach ($conn in $conns) {
                $owner = [int]$conn.OwningProcess
                if ($owner -ne $selfPid -and $owner -ne $parentPid -and -not $targets.ContainsKey($owner)) {
                    $p = $allProcs | Where-Object { $_.ProcessId -eq $owner } | Select-Object -First 1
                    if ($p) {
                        $targets[$owner] = @{ Name = $p.Name; Cmd = $p.CommandLine; Type = "port-$port" }
                    }
                }
            }
        } catch {}
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
                        $targets[$child] = @{ Name = $p.Name; Cmd = $p.CommandLine; Type = "child" }
                        $queue.Enqueue($child)
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
    foreach ($procId in $procs.Keys) {
        $info = $procs[$procId]
        Write-Log "  Killing PID $procId ($($info.Name))"
        try {
            Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
        } catch {}
    }
    
    Start-Sleep -Seconds 2
    
    # Verify
    $remaining = Get-BridgeProcesses
    if ($remaining.Count -gt 0) {
        Write-Log "WARNING: $($remaining.Count) processes still running after kill" -Level "WARN"
        return $false
    }
    
    Write-Log "All processes stopped." -Level "OK"
    return $true
}

function Remove-StaleFiles {
    <#
    .DESCRIPTION
        Remove lock and PID files if no bridge processes are running.
    #>
    $procs = Get-BridgeProcesses
    if ($procs.Count -gt 0) {
        Write-Log "Cannot remove files - bridge processes still running" -Level "WARN"
        return
    }
    
    if (Test-Path $LockFile) {
        Remove-Item $LockFile -Force -ErrorAction SilentlyContinue
        Write-Log "Removed stale lock file"
    }
    
    if (Test-Path $PidFile) {
        Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
        Write-Log "Removed stale PID file"
    }
}

function Start-Bridge {
    <#
    .DESCRIPTION
        Start bridge-u-f using the launcher batch file.
    #>
    param([switch]$ResumeMode)
    
    # Check if already running
    $procs = Get-BridgeProcesses
    if ($procs.Count -gt 0) {
        Write-Log "Bridge-u-f is already running (found $($procs.Count) processes)" -Level "WARN"
        return $false
    }
    
    # Check lock file
    if (Test-LockFileHeld) {
        Write-Log "Lock file is held but no processes found - this is unexpected" -Level "WARN"
    }
    
    # Clean up any stale files
    Remove-StaleFiles
    
    # Build arguments
    $args = @("--no-pause")
    if ($ResumeMode) {
        $args += "--resume-last"
    }
    
    Write-Log "Starting bridge-u-f..."
    
    # Start the launcher
    try {
        # Use a single cmd /c command string so bridge-u.bat receives args consistently.
        $argString = $args -join ' '
        $cmdLine = "`"$LauncherBat`" $argString"
        Start-Process -FilePath "cmd.exe" -ArgumentList "/c $cmdLine" -WorkingDirectory $ProjectDir
        
        # Wait for startup
        for ($i = 0; $i -lt 30; $i++) {
            Start-Sleep -Seconds 1
            $procs = Get-BridgeProcesses
            if ($procs.Count -gt 0) {
                Write-Log "Bridge-u-f started ($($procs.Count) processes)" -Level "OK"
                return $true
            }
        }
        
        Write-Log "Bridge-u-f did not start within 30 seconds" -Level "ERROR"
        return $false
        
    } catch {
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
        $success = Start-Bridge -ResumeMode:$Resume
        exit $(if ($success) { 0 } else { 1 })
    }
    "restart" {
        Write-Log "=== Restarting bridge-u-f ==="
        $stopOk = Stop-BridgeProcesses -ForceImmediate:$Force
        if ($stopOk) {
            Remove-StaleFiles
            Start-Sleep -Seconds 2
            $startOk = Start-Bridge -ResumeMode:$Resume
            exit $(if ($startOk) { 0 } else { 1 })
        } else {
            Write-Log "Failed to stop existing instance" -Level "ERROR"
            exit 1
        }
    }
}
