[CmdletBinding()]
param([switch]$Uninstalling)

. (Join-Path $PSScriptRoot 'Common.ps1')

if (-not (Test-IsAdministrator)) {
    Write-BilingualMessage `
        -English 'Administrator permission is required to stop HASHI.' `
        -Chinese '停止 HASHI 需要管理员权限。' `
        -ForegroundColor Red
    exit 1
}

Initialize-PortableEnvironment
$secrets = Get-PortableSecrets
$endpoint = Get-VerifiedLocalEndpoint

function Get-PortableOwnedProcesses {
    $browserProfile = [System.IO.Path]::GetFullPath(
        (Join-Path $script:DataRoot 'browser-profile')
    )
    $owned = @()
    foreach ($candidate in @(Get-CimInstance Win32_Process -ErrorAction Stop)) {
        $processId = [int]$candidate.ProcessId
        if ($processId -eq $PID) { continue }
        $live = Get-Process -Id $processId -ErrorAction SilentlyContinue
        if ($null -eq $live -or $live.HasExited) { continue }

        $name = ([string]$candidate.Name).ToLowerInvariant()
        $commandLine = [string]$candidate.CommandLine
        $runtimeOwned = Test-PathInsideRoot `
            -Candidate ([string]$candidate.ExecutablePath) `
            -Root $script:PortableRoot
        $browserOwned = (
            $name -in @('msedge.exe', 'chrome.exe') -and
            $commandLine.IndexOf(
                $browserProfile,
                [System.StringComparison]::OrdinalIgnoreCase
            ) -ge 0
        )
        if ($runtimeOwned -or $browserOwned) { $owned += $candidate }
    }
    return @($owned)
}

if ($null -ne $endpoint) {
    $backend = Get-OwnedProcess -ProcessId ([int]$endpoint.backend_pid)
    if ($null -ne $backend) {
        try {
            Invoke-RestMethod `
                -Method Post `
                -Uri "http://127.0.0.1:$([int]$endpoint.api_port)/api/admin/shutdown" `
                -Headers @{ 'X-Workbench-Token' = [string]$secrets.workbench_admin_token } `
                -ContentType 'application/json' `
                -Body '{"reason":"portable-local-stop"}' `
                -TimeoutSec 5 | Out-Null
        } catch {}
        try {
            Wait-Process -Id $backend.Id -Timeout 35 -ErrorAction Stop
        } catch {
            Stop-Process -Id $backend.Id -Force -ErrorAction SilentlyContinue
        }
    }
}

foreach ($ownedProcess in @(Get-PortableOwnedProcesses)) {
    Stop-Process -Id ([int]$ownedProcess.ProcessId) -Force -ErrorAction SilentlyContinue
}

$shutdownDeadline = (Get-Date).AddSeconds(45)
do {
    $remaining = @(Get-PortableOwnedProcesses)
    if ($remaining.Count -eq 0) { break }
    foreach ($ownedProcess in $remaining) {
        Stop-Process -Id ([int]$ownedProcess.ProcessId) -Force -ErrorAction SilentlyContinue
    }
    Start-Sleep -Milliseconds 500
} while ((Get-Date) -lt $shutdownDeadline)

$remaining = @(Get-PortableOwnedProcesses)
if ($remaining.Count -gt 0) {
    $remainingText = ($remaining | ForEach-Object {
        "$($_.Name) (PID $($_.ProcessId))"
    }) -join ', '
    Write-BilingualMessage `
        -English "HASHI could not fully stop these processes: $remainingText." `
        -Chinese "HASHI 无法完全停止以下进程：$remainingText。" `
        -ForegroundColor Red
    exit 1
}

Remove-Item `
    -LiteralPath $script:EndpointPath, $script:HashiPidPath, $script:WorkbenchPidPath `
    -Force `
    -ErrorAction SilentlyContinue
if (-not $Uninstalling) {
    Write-BilingualMessage `
        -English 'HASHI has stopped.' `
        -Chinese 'HASHI 已停止。' `
        -ForegroundColor Green
}
exit 0
