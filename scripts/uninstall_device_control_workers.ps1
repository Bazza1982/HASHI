param(
    [string]$BridgeHome = (Split-Path -Parent $PSScriptRoot)
)

$ErrorActionPreference = "Stop"
$BridgeHome = (Resolve-Path -LiteralPath $BridgeHome).Path
$ConfigPath = Join-Path $BridgeHome "agents.json"
$Config = Get-Content -LiteralPath $ConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json
$InstanceId = ([string]$Config.global.instance_id).ToUpperInvariant()
if ([string]::IsNullOrWhiteSpace($InstanceId)) {
    throw "global.instance_id is required in $ConfigPath"
}

$TaskNames = @(
    "HASHI-$InstanceId-DeviceControl-Computer",
    "HASHI-$InstanceId-DeviceControl-Browser"
)
foreach ($TaskName in $TaskNames) {
    $Task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($null -eq $Task) {
        continue
    }
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Removed $TaskName"
}

$StateDir = Join-Path $BridgeHome "state\device_control"
foreach ($Name in @("browser_control.json", "computer_control.json")) {
    $Path = Join-Path $StateDir $Name
    if (Test-Path -LiteralPath $Path -PathType Leaf) {
        Remove-Item -LiteralPath $Path -Force
    }
}
