param(
    [string]$RepoRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$PythonExe = ""
)

$ErrorActionPreference = "Stop"

$ExtensionId = "jdeaedmoejdapldleofeggedgenogpka"
$HostName = "com.hashi.browser_bridge"
$RepoRoot = (Resolve-Path -LiteralPath $RepoRoot).Path
$InstallRoot = Join-Path $env:LOCALAPPDATA "HASHI\browser_bridge"
$ExtensionSource = Join-Path $RepoRoot "tools\chrome_extension\hashi_browser_bridge"
$ExtensionInstallDir = Join-Path $InstallRoot "extension"
$WrapperPath = Join-Path $InstallRoot "hashi_browser_bridge_host.cmd"
$ManifestPath = Join-Path $InstallRoot "$HostName.json"
$RegistryPath = "HKCU:\Software\Google\Chrome\NativeMessagingHosts\$HostName"

function Write-Log {
    param([string]$Message)
    Write-Host "[HASHI Browser Bridge] $Message"
}

if (-not (Test-Path -LiteralPath $ExtensionSource -PathType Container)) {
    throw "Extension source not found: $ExtensionSource"
}

if (-not $PythonExe) {
    $RepoPython = Join-Path $RepoRoot ".venv\Scripts\python.exe"
    if (Test-Path -LiteralPath $RepoPython -PathType Leaf) {
        $PythonExe = $RepoPython
    }
    else {
        $PythonCommand = Get-Command python.exe -ErrorAction Stop
        $PythonExe = $PythonCommand.Source
    }
}
$PythonExe = (Resolve-Path -LiteralPath $PythonExe).Path

New-Item -ItemType Directory -Path $InstallRoot -Force | Out-Null
if (Test-Path -LiteralPath $ExtensionInstallDir) {
    Remove-Item -LiteralPath $ExtensionInstallDir -Recurse -Force
}
Copy-Item -LiteralPath $ExtensionSource -Destination $ExtensionInstallDir -Recurse -Force
Write-Log "Copied extension to $ExtensionInstallDir"

$WrapperContent = @"
@echo off
setlocal
set "PYTHONPATH=$RepoRoot;%PYTHONPATH%"
cd /d "$RepoRoot"
"$PythonExe" -m tools.browser_native_host --stdio %*
"@
Set-Content -LiteralPath $WrapperPath -Value $WrapperContent -Encoding ASCII
Write-Log "Wrote native host wrapper: $WrapperPath"

$Manifest = @{
    name = $HostName
    description = "HASHI Browser Bridge native Windows host"
    path = $WrapperPath
    type = "stdio"
    allowed_origins = @("chrome-extension://$ExtensionId/")
}
$Manifest | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $ManifestPath -Encoding ASCII

New-Item -Path $RegistryPath -Force | Out-Null
Set-Item -Path $RegistryPath -Value $ManifestPath
Write-Log "Registered native host in $RegistryPath"

Write-Host ""
Write-Host "Chrome setup:"
Write-Host "1. Open chrome://extensions"
Write-Host "2. Enable Developer mode"
Write-Host "3. Click 'Load unpacked'"
Write-Host "4. Select: $ExtensionInstallDir"
Write-Host ""
Write-Host "Expected extension ID: $ExtensionId"
Write-Host "The host will create a per-user authentication key on first launch."
