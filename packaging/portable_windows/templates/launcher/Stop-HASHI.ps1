. (Join-Path $PSScriptRoot 'Common.ps1')

Initialize-PortableEnvironment
$config = Get-PortableConfig
$secrets = Get-PortableSecrets
$port = [int]$config.global.workbench_port

function Test-PathInsidePortableRoot {
    param(
        [string]$Candidate,
        [string]$Root
    )
    if (-not $Candidate -or -not $Root) { return $false }
    try {
        $candidatePath = [System.IO.Path]::GetFullPath($Candidate)
        $rootPath = [System.IO.Path]::GetFullPath($Root).TrimEnd('\') + '\'
        return $candidatePath.StartsWith(
            $rootPath,
            [System.StringComparison]::OrdinalIgnoreCase
        )
    } catch {
        return $false
    }
}

function Get-PortableOwnedProcesses {
    $browserProfile = [System.IO.Path]::GetFullPath(
        (Join-Path $script:DataRoot 'browser-profile')
    )
    $launcherPaths = @(
        (Join-Path $script:PortableRoot 'Start_HASHI_TUI.bat'),
        (Join-Path $script:PortableRoot 'Start_HASHI_Workbench.bat'),
        (Join-Path $PSScriptRoot 'Start-TUI.ps1'),
        (Join-Path $PSScriptRoot 'Start-Workbench.ps1')
    )
    $owned = @()
    foreach ($candidate in @(Get-CimInstance Win32_Process -ErrorAction Stop)) {
        $processId = [int]$candidate.ProcessId
        if ($processId -eq $PID) { continue }
        $live = Get-Process -Id $processId -ErrorAction SilentlyContinue
        if ($null -eq $live -or $live.HasExited) { continue }

        $name = ([string]$candidate.Name).ToLowerInvariant()
        $commandLine = [string]$candidate.CommandLine
        $runtimeOwned = (
            (Test-PathInsidePortableRoot -Candidate ([string]$candidate.ExecutablePath) -Root $script:PortableRoot) -or
            (Test-PathInsidePortableRoot -Candidate ([string]$candidate.ExecutablePath) -Root $script:LocalCacheRoot)
        )
        $browserOwned = (
            $name -in @('msedge.exe', 'chrome.exe') -and
            $commandLine.IndexOf(
                $browserProfile,
                [System.StringComparison]::OrdinalIgnoreCase
            ) -ge 0
        )
        $launcherOwned = $false
        if ($name -in @('cmd.exe', 'powershell.exe', 'pwsh.exe')) {
            foreach ($launcherPath in $launcherPaths) {
                if ($commandLine.IndexOf(
                    $launcherPath,
                    [System.StringComparison]::OrdinalIgnoreCase
                ) -ge 0) {
                    $launcherOwned = $true
                    break
                }
            }
        }
        if ($runtimeOwned -or $browserOwned -or $launcherOwned) {
            $owned += $candidate
        }
    }
    return @($owned)
}

try {
    Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:$port/api/admin/shutdown" -Headers @{ 'X-Workbench-Token' = [string]$secrets.workbench_admin_token } -ContentType 'application/json' -Body '{"reason":"portable-stop"}' -TimeoutSec 5 | Out-Null
} catch {
    Write-BilingualMessage `
        -English 'HASHI was already stopped or did not answer.' `
        -Chinese 'HASHI 已停止或没有响应。' `
        -ForegroundColor Yellow
}

$hashiProcess = Get-LiveProcessFromPidFile -Path $script:HashiPidPath
$ownedProcessIds = @(
    Get-PortableOwnedProcesses | ForEach-Object { [int]$_.ProcessId }
)
if ($null -ne $hashiProcess -and $hashiProcess.Id -in $ownedProcessIds) {
    try { Wait-Process -Id $hashiProcess.Id -Timeout 35 -ErrorAction Stop } catch { Stop-Process -Id $hashiProcess.Id -Force -ErrorAction SilentlyContinue }
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
        -English "HASHI could not fully stop these processes: $remainingText. Do not eject the USB drive; close them and run Stop_HASHI.bat again." `
        -Chinese "HASHI 无法完全停止以下进程：$remainingText。请勿拔出 USB；关闭这些进程后再次运行 Stop_HASHI.bat。" `
        -ForegroundColor Red
    exit 1
}

Remove-Item -LiteralPath $script:HashiPidPath, $script:WorkbenchPidPath -Force -ErrorAction SilentlyContinue
Write-BilingualMessage `
    -English 'HASHI Portable has stopped. It is now safe to eject the USB drive.' `
    -Chinese 'HASHI Portable 已停止，现在可以安全弹出 USB。' `
    -ForegroundColor Green
exit 0
