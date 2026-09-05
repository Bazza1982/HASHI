. (Join-Path $PSScriptRoot 'Common.ps1')

Initialize-PortableEnvironment
$config = Get-PortableConfig
$secrets = Get-PortableSecrets
$port = [int]$config.global.workbench_port

try {
    Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:$port/api/admin/shutdown" -Headers @{ 'X-Workbench-Token' = [string]$secrets.workbench_admin_token } -ContentType 'application/json' -Body '{"reason":"portable-stop"}' -TimeoutSec 5 | Out-Null
} catch {
    Write-Host 'HASHI API was already stopped or did not answer.' -ForegroundColor Yellow
}

$hashiProcess = Get-LiveProcessFromPidFile -Path $script:HashiPidPath
if ($null -ne $hashiProcess) {
    try { Wait-Process -Id $hashiProcess.Id -Timeout 35 -ErrorAction Stop } catch { Stop-Process -Id $hashiProcess.Id -Force -ErrorAction SilentlyContinue }
}

$workbenchProcess = Get-LiveProcessFromPidFile -Path $script:WorkbenchPidPath
if ($null -ne $workbenchProcess) {
    Stop-Process -Id $workbenchProcess.Id -Force -ErrorAction SilentlyContinue
}

$remoteClaim = Join-Path $script:DataRoot 'state\remote_runtime_claim.json'
if (Test-Path -LiteralPath $remoteClaim) {
    try {
        $remotePid = [int]((Get-Content -LiteralPath $remoteClaim -Raw -Encoding UTF8 | ConvertFrom-Json).pid)
        if ($remotePid -gt 0) { Stop-Process -Id $remotePid -Force -ErrorAction SilentlyContinue }
    } catch {}
}

Remove-Item -LiteralPath $script:HashiPidPath, $script:WorkbenchPidPath -Force -ErrorAction SilentlyContinue
Write-Host 'HASHI Portable has stopped. It is now safe to eject the USB drive.' -ForegroundColor Green
exit 0
