# Check Bridge-U-F Stress Test Status
# Returns JSON with current status for agent consumption

$ProjectDir = "C:\Users\thene\projects\bridge-u-f"
$PidFile = "$ProjectDir\state\stress_test.pid"
$StatusFile = "$ProjectDir\state\stress_test_status.json"

$Result = @{
    timestamp = (Get-Date).ToString("o")
    running = $false
    pid = $null
    progress = $null
    recent_log = @()
    errors_summary = @{}
}

# Check if PID file exists and process is running
if (Test-Path $PidFile) {
    $Pid = Get-Content $PidFile
    $Process = Get-Process -Id $Pid -ErrorAction SilentlyContinue
    
    if ($Process) {
        $Result.running = $true
        $Result.pid = [int]$Pid
        
        # Get status file info
        if (Test-Path $StatusFile) {
            $Status = Get-Content $StatusFile | ConvertFrom-Json
            $StartedAt = [DateTime]::Parse($Status.started_at)
            $Duration = $Status.duration_seconds
            $Elapsed = ((Get-Date) - $StartedAt).TotalSeconds
            $Progress = [math]::Min(100, [math]::Round(($Elapsed / $Duration) * 100, 1))
            
            $Result.progress = @{
                elapsed_seconds = [int]$Elapsed
                total_seconds = $Duration
                percent = $Progress
                remaining_seconds = [math]::Max(0, $Duration - $Elapsed)
            }
        }
        
        # Get recent log lines
        $LogFile = $Status.log_file
        if ($LogFile -and (Test-Path $LogFile)) {
            $Result.recent_log = Get-Content $LogFile -Tail 20
        }
    }
    else {
        # Process finished
        $Result.running = $false
        $Result.status = "completed"
        
        # Find the report file
        $Reports = Get-ChildItem "$ProjectDir\logs\stress_test_*.json" | Sort-Object LastWriteTime -Descending | Select-Object -First 1
        if ($Reports) {
            $Result.report_file = $Reports.FullName
            $Report = Get-Content $Reports.FullName | ConvertFrom-Json
            $Result.summary = @{
                cycles = $Report.cycles
                commands_tested = $Report.commands_tested
                commands_passed = $Report.commands_passed
                chats_sent = $Report.chats_sent
                chats_received = $Report.chats_received
                errors_found = $Report.errors_found.Count
                fixes_applied = $Report.fixes_applied.Count
            }
        }
    }
}
else {
    $Result.status = "not_started"
}

# Check for recent errors across all agents
$Agents = @("sakura", "lily", "coder", "claude-coder", "codex-coder", "temp", "barry")
foreach ($Agent in $Agents) {
    $AgentLogs = Get-ChildItem "$ProjectDir\logs\$Agent" -Directory -ErrorAction SilentlyContinue | Sort-Object Name -Descending | Select-Object -First 1
    if ($AgentLogs) {
        $ErrorLog = Join-Path $AgentLogs.FullName "errors.log"
        if (Test-Path $ErrorLog) {
            $RecentErrors = Get-Content $ErrorLog -Tail 5 -ErrorAction SilentlyContinue | Where-Object { $_ -match "ERROR" }
            if ($RecentErrors) {
                $Result.errors_summary[$Agent] = $RecentErrors.Count
            }
        }
    }
}

$Result | ConvertTo-Json -Depth 5
