param(
    [Parameter(Mandatory = $true)][string]$CandidateId,
    [string]$CandidateRoot = "C:\AptenraDebug\packaging-candidates",
    [int]$Port = 18080
)

$ErrorActionPreference = "Stop"
$statePath = Join-Path (Join-Path $CandidateRoot $CandidateId) "http-server.json"
$ruleName = "Aptenra Superloop $CandidateId HTTP $Port"
if (Test-Path -LiteralPath $statePath) {
    $state = Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json
    Stop-Process -Id ([int]$state.pid) -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $statePath -Force
}
Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue |
    Remove-NetFirewallRule -ErrorAction SilentlyContinue
if (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) {
    throw "temporary HTTP listener still present: $Port"
}
[pscustomobject]@{
    ok = $true
    candidate = $CandidateId
    listener = $false
    firewall_rule = $false
} | ConvertTo-Json
