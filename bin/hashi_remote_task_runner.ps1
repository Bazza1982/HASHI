param(
    [Parameter(Mandatory = $true)]
    [string]$Python,

    [Parameter(Mandatory = $true)]
    [string]$HashiRoot,

    [Parameter(Mandatory = $true)]
    [string]$LogPath,

    [string]$PythonArgs = ""
)

$ErrorActionPreference = "Stop"

$LogDir = Split-Path -Parent $LogPath
if ($LogDir) {
    New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
}

Set-Location $HashiRoot
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONLEGACYWINDOWSSTDIO = "utf-8"

$ArgumentList = @()
if ($PythonArgs) {
    $ArgumentList = [System.Management.Automation.PSParser]::Tokenize($PythonArgs, [ref]$null) |
        Where-Object { $_.Type -in @("String", "CommandArgument") } |
        ForEach-Object { $_.Content }
}

& $Python @ArgumentList *>> $LogPath
exit $LASTEXITCODE
