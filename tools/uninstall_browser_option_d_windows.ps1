param(
    [string]$RepoRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$PythonExe = ""
)

$ErrorActionPreference = "Stop"
$RepoRoot = (Resolve-Path -LiteralPath $RepoRoot).Path
if (-not $PythonExe) {
    $PythonExe = Join-Path $RepoRoot ".venv\Scripts\python.exe"
}
if (-not (Test-Path -LiteralPath $PythonExe -PathType Leaf)) {
    throw "HASHI Python executable not found: $PythonExe"
}
$PythonExe = (Resolve-Path -LiteralPath $PythonExe).Path

$PreviousPythonPath = $env:PYTHONPATH
try {
    $env:PYTHONPATH = $RepoRoot
    $Namespace = (& $PythonExe -c "from tools.browser_bridge_transport import BRIDGE_NAMESPACE; print(BRIDGE_NAMESPACE)").Trim()
}
finally {
    $env:PYTHONPATH = $PreviousPythonPath
}
if ([string]::IsNullOrWhiteSpace($Namespace) -or $Namespace -notmatch '^[a-z0-9][a-z0-9_-]{0,95}$') {
    throw "Refusing to remove a Browser Bridge with an invalid namespace"
}

$HostSuffix = ($Namespace.ToLowerInvariant() -replace "[^a-z0-9_]", "_")
$HostName = "com.hashi.browser_bridge.$HostSuffix"
$RegistryPath = "HKCU:\Software\Google\Chrome\NativeMessagingHosts\$HostName"
$InstallBase = Join-Path $env:LOCALAPPDATA "HASHI\browser_bridge"
$InstallRoot = Join-Path $InstallBase $Namespace

if (Test-Path -LiteralPath $RegistryPath) {
    Remove-Item -LiteralPath $RegistryPath -Recurse -Force
    Write-Host "Removed native host registration $HostName"
}
if (Test-Path -LiteralPath $InstallRoot -PathType Container) {
    $ResolvedInstallRoot = (Resolve-Path -LiteralPath $InstallRoot).Path
    $ResolvedBase = (Resolve-Path -LiteralPath $InstallBase).Path
    if (-not $ResolvedInstallRoot.StartsWith($ResolvedBase + "\", [StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to remove a Browser Bridge outside its installation root"
    }
    Remove-Item -LiteralPath $ResolvedInstallRoot -Recurse -Force
    Write-Host "Removed instance-scoped Browser Bridge files for $Namespace"
}

Write-Host "Remove the matching unpacked Chrome extension from chrome://extensions if it is still loaded."
