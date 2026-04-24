<#
.SYNOPSIS
    HASHI Browser Bridge -- Launches Chrome with CDP debugging enabled.
    Place this script in Windows startup or run manually before using
    HASHI browser tools with your logged-in Chrome profile.

.DESCRIPTION
    - Detects if Chrome is already running with --remote-debugging-port
    - If not, launches Chrome with CDP enabled on the specified port
    - Listens on 0.0.0.0 so WSL2 can connect across the virtual network
    - Uses your default Chrome profile (preserves all logins/cookies)
    - Writes a small status file for health checking

.PARAMETER Port
    The debugging port to use (default: 9222)

.PARAMETER ChromePath
    Override path to chrome.exe (auto-detected if not provided)

.PARAMETER NoProfile
    If set, launch Chrome with a temporary profile instead of default

.EXAMPLE
    .\browser_bridge_launcher.ps1
    .\browser_bridge_launcher.ps1 -Port 9223
#>

param(
    [int]$Port = 9222,
    [string]$ChromePath = "",
    [switch]$NoProfile
)

$ErrorActionPreference = "Stop"

# --- Logging ---
$LogDir = Join-Path $PSScriptRoot "..\..\logs"
if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir -Force | Out-Null }
$LogFile = Join-Path $LogDir "browser_bridge_launcher.log"

function Write-Log {
    param([string]$Message, [string]$Level = "INFO")
    $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    $line = "$ts | $Level | BrowserBridge | $Message"
    Add-Content -Path $LogFile -Value $line
    Write-Host $line
}

# --- Find Chrome ---
function Find-Chrome {
    $candidates = @(
        "${env:ProgramFiles}\Google\Chrome\Application\chrome.exe",
        "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
        "${env:LOCALAPPDATA}\Google\Chrome\Application\chrome.exe"
    )
    foreach ($c in $candidates) {
        if (Test-Path $c) { return $c }
    }
    return $null
}

# --- Check if CDP is already active ---
function Test-CDPActive {
    param([int]$Port)
    try {
        $response = Invoke-WebRequest -Uri "http://localhost:$Port/json/version" -TimeoutSec 3 -UseBasicParsing
        if ($response.StatusCode -eq 200) { return $true }
    } catch {}
    return $false
}

# --- Main ---
Write-Log "Browser Bridge launcher starting (port=$Port)"

# Check if already running
if (Test-CDPActive -Port $Port) {
    Write-Log "Chrome CDP already active on port $Port -- no action needed"
    try {
        $ver = (Invoke-WebRequest -Uri "http://localhost:$Port/json/version" -TimeoutSec 3 -UseBasicParsing).Content | ConvertFrom-Json
        Write-Log "Connected browser: $($ver.Browser)"
    } catch {}
    exit 0
}

# If Chrome is running without CDP, restart it with CDP flags
$existingChrome = Get-Process chrome -ErrorAction SilentlyContinue
if ($existingChrome) {
    Write-Log "Chrome is running without CDP. Stopping all Chrome processes..."
    $existingChrome | Stop-Process -Force
    # Wait until all Chrome processes are gone (profile lock released)
    $stopWait = 0
    while ($stopWait -lt 10) {
        Start-Sleep -Seconds 1
        $stopWait++
        $remaining = Get-Process chrome -ErrorAction SilentlyContinue
        if (-not $remaining) { break }
    }
    if (Get-Process chrome -ErrorAction SilentlyContinue) {
        Write-Log "Could not stop all Chrome processes after 10s" "ERROR"
        exit 1
    }
    Write-Log ("All Chrome processes stopped (waited " + $stopWait + "s)")
}

# Find Chrome executable
if (-not $ChromePath) {
    $ChromePath = Find-Chrome
}
if (-not $ChromePath -or -not (Test-Path $ChromePath)) {
    Write-Log "Chrome not found. Please install Chrome or provide -ChromePath" "ERROR"
    exit 1
}
Write-Log "Using Chrome at: $ChromePath"

# Build launch arguments
$chromeArgs = @(
    "--remote-debugging-port=$Port",
    "--remote-debugging-address=0.0.0.0",
    "--remote-allow-origins=*"
)

if (-not $NoProfile) {
    # Use HASHI_CDP_Profile directory, which has a junction link from
    # Default/ -> User Data/Default/ so all logins/cookies are shared.
    # This satisfies Chrome's requirement that CDP use a non-default
    # user-data-dir while preserving the user's real session.
    $cdpProfileDir = Join-Path $env:LOCALAPPDATA "Google\Chrome\HASHI_CDP_Profile"
    if (-not (Test-Path $cdpProfileDir)) {
        New-Item -ItemType Directory -Path $cdpProfileDir -Force | Out-Null
        Write-Log "Created CDP profile directory: $cdpProfileDir"
    }
    $chromeArgs += """--user-data-dir=$cdpProfileDir"""
    Write-Log "Using CDP profile at: $cdpProfileDir (junctioned to real profile)"
} else {
    # NoProfile mode -- use a temp directory
    $cdpProfileDir = Join-Path $env:TEMP "HASHI_CDP_Temp"
    if (-not (Test-Path $cdpProfileDir)) {
        New-Item -ItemType Directory -Path $cdpProfileDir -Force | Out-Null
    }
    $chromeArgs += """--user-data-dir=$cdpProfileDir"""
    Write-Log "Using temporary CDP profile"
}

# Launch Chrome
Write-Log "Launching Chrome with CDP on port $Port..."
$argString = $chromeArgs -join " "
Write-Log "Chrome args: $argString"
$process = Start-Process -FilePath $ChromePath -ArgumentList $argString -PassThru

# Wait for CDP to become available
$maxWait = 15
$waited = 0
while ($waited -lt $maxWait) {
    Start-Sleep -Seconds 1
    $waited++
    if (Test-CDPActive -Port $Port) {
        $readyMsg = "Chrome CDP ready on port " + $Port + " (waited " + $waited + "s)"
        Write-Log $readyMsg
        try {
            $ver = (Invoke-WebRequest -Uri "http://localhost:$Port/json/version" -TimeoutSec 3 -UseBasicParsing).Content | ConvertFrom-Json
            Write-Log "Browser: $($ver.Browser)"
            Write-Log "WebSocket URL: $($ver.webSocketDebuggerUrl)"
        } catch {}
        exit 0
    }
}

$msg = "Chrome launched but CDP not responding after " + $maxWait + "s"
Write-Log $msg "ERROR"
exit 1
