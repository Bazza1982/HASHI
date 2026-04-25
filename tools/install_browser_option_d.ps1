param(
    [Parameter(Mandatory = $true)]
    [string]$DistroName,
    [Parameter(Mandatory = $true)]
    [string]$LinuxRepoRoot
)

$ErrorActionPreference = "Stop"

$ExtensionId = "jdeaedmoejdapldleofeggedgenogpka"
$HostName = "com.hashi.browser_bridge"
$InstallRoot = Join-Path $env:LOCALAPPDATA "HASHI\browser_bridge"
$ExtensionInstallDir = Join-Path $InstallRoot "extension"
$WrapperPath = Join-Path $InstallRoot "hashi_browser_bridge_host.cmd"
$ManifestPath = Join-Path $InstallRoot "$HostName.json"
$RegistryPath = "HKCU:\Software\Google\Chrome\NativeMessagingHosts\$HostName"

function Convert-LinuxPathToUnc {
    param([string]$LinuxPath, [string]$Distro)
    $trimmed = $LinuxPath.Trim("/")
    $windowsPath = $trimmed -replace "/", "\"
    return "\\wsl.localhost\$Distro\$windowsPath"
}

function Write-Log {
    param([string]$Message)
    Write-Host "[HASHI Option D] $Message"
}

New-Item -ItemType Directory -Path $InstallRoot -Force | Out-Null

$ExtensionSource = Join-Path (Convert-LinuxPathToUnc -LinuxPath "$LinuxRepoRoot/tools/chrome_extension/hashi_browser_bridge" -Distro $DistroName) ""
if (-not (Test-Path $ExtensionSource)) {
    throw "Extension source not found: $ExtensionSource"
}

if (Test-Path $ExtensionInstallDir) {
    Remove-Item -Recurse -Force $ExtensionInstallDir
}
Copy-Item -Recurse -Force $ExtensionSource $ExtensionInstallDir
Write-Log "Copied extension to $ExtensionInstallDir"

$WrapperContent = @"
@echo off
cd /d %LOCALAPPDATA%
C:\Windows\System32\wsl.exe -d $DistroName bash -lc "cd '$LinuxRepoRoot' && /usr/bin/env python3 -m tools.browser_native_host --stdio --socket /tmp/hashi-browser-bridge.sock --log-file '$LinuxRepoRoot/logs/browser_native_host.log'"
"@
Set-Content -Path $WrapperPath -Value $WrapperContent -Encoding ASCII
Write-Log "Wrote wrapper: $WrapperPath"

$Manifest = @{
    name = $HostName
    description = "HASHI Browser Bridge native host"
    path = $WrapperPath
    type = "stdio"
    allowed_origins = @("chrome-extension://$ExtensionId/")
}
$Manifest | ConvertTo-Json -Depth 4 | Set-Content -Path $ManifestPath -Encoding ASCII
Write-Log "Wrote host manifest: $ManifestPath"

New-Item -Path $RegistryPath -Force | Out-Null
Set-ItemProperty -Path $RegistryPath -Name "(default)" -Value $ManifestPath
Write-Log "Registered native host in $RegistryPath"

Write-Host ""
Write-Host "Install steps:"
Write-Host "1. Open Chrome and go to chrome://extensions"
Write-Host "2. Enable Developer mode"
Write-Host "3. Click 'Load unpacked'"
Write-Host "4. Select: $ExtensionInstallDir"
Write-Host "5. Pin the extension if you want visibility; no click is required for runtime"
Write-Host ""
Write-Host "Expected extension ID: $ExtensionId"
