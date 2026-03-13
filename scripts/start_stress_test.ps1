# Start Bridge-U-F Stress Test in Background
# This script starts the test and creates a marker file for monitoring

$ProjectDir = "C:\Users\thene\projects\bridge-u-f"
$Duration = $args[0]
if (-not $Duration) { $Duration = 7200 }

$Timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
$LogFile = "$ProjectDir\logs\stress_test_$Timestamp.log"
$PidFile = "$ProjectDir\state\stress_test.pid"
$StatusFile = "$ProjectDir\state\stress_test_status.json"

# Create state directory if needed
New-Item -ItemType Directory -Force -Path "$ProjectDir\state" | Out-Null

# Write initial status
@{
    started_at = (Get-Date).ToString("o")
    duration_seconds = [int]$Duration
    expected_end = (Get-Date).AddSeconds([int]$Duration).ToString("o")
    status = "running"
    log_file = $LogFile
} | ConvertTo-Json | Set-Content $StatusFile

# Start the test in background
$Process = Start-Process -FilePath "python" `
    -ArgumentList "$ProjectDir\stress_test.py", "--duration", $Duration `
    -WorkingDirectory $ProjectDir `
    -RedirectStandardOutput $LogFile `
    -RedirectStandardError "$ProjectDir\logs\stress_test_$Timestamp.err" `
    -NoNewWindow `
    -PassThru

# Save PID
$Process.Id | Set-Content $PidFile

Write-Host "Stress test started!"
Write-Host "  PID: $($Process.Id)"
Write-Host "  Duration: $Duration seconds"
Write-Host "  Log: $LogFile"
Write-Host "  Status: $StatusFile"
