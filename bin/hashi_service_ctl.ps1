<#
.SYNOPSIS
    Restart one explicitly configured Windows HASHI service.

.DESCRIPTION
    This helper is intentionally narrow. The caller supplies one validated
    service name from live-runtime-protection.json; no arbitrary command or
    executable is accepted. Hashi Remote performs the final PID, identity,
    health, runtime and Function generation verification.
#>

param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[A-Za-z0-9_.-]{1,128}$')]
    [string]$ServiceName
)

$ErrorActionPreference = 'Stop'
$timeout = [TimeSpan]::FromSeconds(45)
$service = Get-Service -Name $ServiceName -ErrorAction Stop

if ($service.Status -ne [System.ServiceProcess.ServiceControllerStatus]::Stopped) {
    Stop-Service -Name $ServiceName -Force -ErrorAction Stop
    $service.WaitForStatus(
        [System.ServiceProcess.ServiceControllerStatus]::Stopped,
        $timeout
    )
}

Start-Service -Name $ServiceName -ErrorAction Stop
$service = Get-Service -Name $ServiceName -ErrorAction Stop
$service.WaitForStatus(
    [System.ServiceProcess.ServiceControllerStatus]::Running,
    $timeout
)

[pscustomobject]@{
    ServiceName = $ServiceName
    Status = [string]$service.Status
} | ConvertTo-Json -Compress
