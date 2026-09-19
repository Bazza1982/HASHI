param(
    [Parameter(Mandatory = $true)]
    [string]$HashiRoot,

    [Parameter(Mandatory = $true)]
    [string]$LogPath,

    [Parameter(Mandatory = $true)]
    [string]$RuntimeTaskName
)

$ErrorActionPreference = "Stop"
$ResolvedRoot = ([System.IO.Path]::GetFullPath($HashiRoot)).TrimEnd('\')
$Controller = Join-Path $ResolvedRoot "bin\bridge_ctl.ps1"
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
$WorkbenchPort = [int]$AgentsConfig.global.workbench_port
if ($WorkbenchPort -lt 1 -or $WorkbenchPort -gt 65535) {
    throw "Invalid HASHI Backend port: $WorkbenchPort"
}

$LogDirectory = Split-Path -Parent $LogPath
New-Item -ItemType Directory -Force -Path $LogDirectory | Out-Null
$Timestamp = [DateTimeOffset]::Now.ToString("o")
Add-Content -LiteralPath $LogPath -Encoding UTF8 -Value "$Timestamp fixed restart task started for $ResolvedRoot"

$env:BRIDGE_HOME = $ResolvedRoot
$env:HASHI_ENABLE_LEGACY_FIXED_RUNTIME = "1"
$ControllerOutput = @(& powershell.exe `
    -NoProfile `
    -NonInteractive `
    -ExecutionPolicy Bypass `
    -File $Controller `
    -Action stop *>&1)
$Result = $LASTEXITCODE
$ControllerOutput | Out-File -LiteralPath $LogPath -Append -Encoding UTF8
if ($Result -ne 0) {
    Add-Content -LiteralPath $LogPath -Encoding UTF8 -Value "Fixed restart stop failed with exit code $Result"
    exit $Result
}

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

Start-ScheduledTask -TaskName $RuntimeTaskName -ErrorAction Stop
Add-Content -LiteralPath $LogPath -Encoding UTF8 -Value "Triggered fixed HASHI runtime task $RuntimeTaskName"

$HealthUri = "http://127.0.0.1:$WorkbenchPort/api/health"
for ($Attempt = 0; $Attempt -lt 90; $Attempt++) {
    Start-Sleep -Seconds 1
    try {
        $Health = Invoke-RestMethod -Uri $HealthUri -Method Get -TimeoutSec 1
        if ($Health.ready -eq $true -and [string]$Health.status -eq "ready") {
            Add-Content -LiteralPath $LogPath -Encoding UTF8 -Value "Fixed HASHI restart completed; Backend ready on port $WorkbenchPort"
            exit 0
        }
    } catch {
        # Startup is expected to refuse health requests until the Backend binds.
    }
    $RuntimeTask = Get-ScheduledTask -TaskName $RuntimeTaskName -ErrorAction Stop
    if ([string]$RuntimeTask.State -ne "Running") {
        $RuntimeInfo = Get-ScheduledTaskInfo -TaskName $RuntimeTaskName -ErrorAction SilentlyContinue
        Add-Content -LiteralPath $LogPath -Encoding UTF8 -Value "Fixed HASHI runtime task exited before readiness; result=$($RuntimeInfo.LastTaskResult)"
        exit 1
    }
}

Add-Content -LiteralPath $LogPath -Encoding UTF8 -Value "Fixed HASHI restart timed out waiting for Backend readiness"
exit 1
