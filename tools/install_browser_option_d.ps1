param(
    [Parameter(Mandatory = $true)]
    [string]$DistroName,
    [Parameter(Mandatory = $true)]
    [string]$LinuxRepoRoot
)

$ErrorActionPreference = "Stop"

# Compatibility entrypoint for callers that know the repository by its WSL
# path. The current Windows-native installer owns endpoint names, the
# windowless launcher, authentication files, and Chrome registration.
$WindowsRepoRoot = (
    & C:\Windows\System32\wsl.exe -d $DistroName wslpath -w $LinuxRepoRoot
).Trim()
if ([string]::IsNullOrWhiteSpace($WindowsRepoRoot)) {
    throw "Could not resolve Windows repository path for $LinuxRepoRoot"
}
$NativeInstaller = Join-Path `
    $WindowsRepoRoot `
    "tools\install_browser_option_d_windows.ps1"
if (-not (Test-Path -LiteralPath $NativeInstaller -PathType Leaf)) {
    throw "Current windowless browser installer not found: $NativeInstaller"
}

& $NativeInstaller -RepoRoot $WindowsRepoRoot
exit $LASTEXITCODE
