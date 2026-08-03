param(
    [Parameter(Mandatory = $true)][string]$CandidateId,
    [Parameter(Mandatory = $true)][string]$DisplayVersion,
    [string]$CandidateRoot = "C:\AptenraDebug\packaging-candidates",
    [string]$PythonPath = "C:\Users\thene\projects\Aptenra\.venv-win\Scripts\python.exe",
    [string]$RemoteAddress = "192.168.0.65",
    [int]$Port = 18080
)

$ErrorActionPreference = "Stop"
$publicationName = "Aptenra-Personal_${DisplayVersion}_windows-x64"
$source = Join-Path (Join-Path $CandidateRoot $CandidateId) $publicationName
$ruleName = "Aptenra Superloop $CandidateId HTTP $Port"
$statePath = Join-Path (Join-Path $CandidateRoot $CandidateId) "http-server.json"
$stdout = Join-Path (Join-Path $CandidateRoot $CandidateId) "http-server.stdout.log"
$stderr = Join-Path (Join-Path $CandidateRoot $CandidateId) "http-server.stderr.log"

if (-not (Test-Path -LiteralPath $source -PathType Container)) {
    throw "publication root missing: $source"
}
if (-not (Test-Path -LiteralPath $PythonPath -PathType Leaf)) {
    throw "Python runtime missing: $PythonPath"
}
if (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) {
    throw "HTTP port already in use: $Port"
}
if (Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue) {
    throw "temporary firewall rule already exists: $ruleName"
}

New-NetFirewallRule -DisplayName $ruleName -Direction Inbound -Action Allow -Protocol TCP `
    -LocalPort $Port -RemoteAddress $RemoteAddress -Profile Private | Out-Null
try {
    $process = Start-Process -FilePath $PythonPath `
        -ArgumentList @("-m", "http.server", "$Port", "--bind", "0.0.0.0") `
        -WorkingDirectory $source -RedirectStandardOutput $stdout `
        -RedirectStandardError $stderr -WindowStyle Hidden -PassThru
    Start-Sleep -Seconds 2
    if ($process.HasExited -or -not (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)) {
        throw "temporary HTTP server failed to listen"
    }
    [pscustomobject]@{
        candidate = $CandidateId
        display_version = $DisplayVersion
        pid = $process.Id
        port = $Port
        source = $source
        firewall_rule = $ruleName
        remote_address = $RemoteAddress
    } | ConvertTo-Json | Set-Content -LiteralPath $statePath -Encoding UTF8
    Get-Content -LiteralPath $statePath
} catch {
    Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue |
        Remove-NetFirewallRule -ErrorAction SilentlyContinue
    throw
}
