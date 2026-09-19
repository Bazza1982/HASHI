param(
    [Parameter(Mandatory = $true)]
    [string]$HashiRoot,

    [Parameter(Mandatory = $true)]
    [string]$LogPath
)

$ErrorActionPreference = "Stop"
$ResolvedRoot = ([System.IO.Path]::GetFullPath($HashiRoot)).TrimEnd('\')
$Controller = Join-Path $ResolvedRoot "bin\bridge_ctl.ps1"
if (-not (Test-Path -LiteralPath $Controller -PathType Leaf)) {
    throw "Missing fixed HASHI restart controller: $Controller"
}

$LogDirectory = Split-Path -Parent $LogPath
New-Item -ItemType Directory -Force -Path $LogDirectory | Out-Null
$Timestamp = [DateTimeOffset]::Now.ToString("o")
Add-Content -LiteralPath $LogPath -Encoding UTF8 -Value "$Timestamp fixed restart task started for $ResolvedRoot"

$env:BRIDGE_HOME = $ResolvedRoot
$env:HASHI_ENABLE_LEGACY_FIXED_RUNTIME = "1"
& powershell.exe `
    -NoProfile `
    -NonInteractive `
    -ExecutionPolicy Bypass `
    -File $Controller `
    -Action restart `
    -Resume *>&1 |
    Out-File -LiteralPath $LogPath -Append -Encoding UTF8
$Result = $LASTEXITCODE
if ($Result -ne 0) {
    Add-Content -LiteralPath $LogPath -Encoding UTF8 -Value "Fixed restart task failed with exit code $Result"
}
exit $Result
