param(
    [Parameter(Mandatory = $true)]
    [string]$Python,

    [Parameter(Mandatory = $true)]
    [string]$HashiRoot,

    [Parameter(Mandatory = $true)]
    [string]$LogPath,

    [string]$PythonArgs = "",

    [string]$PythonArgsBase64 = ""
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
if ($PythonArgsBase64) {
    $ArgsJson = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($PythonArgsBase64))
    $ArgumentList = @(ConvertFrom-Json -InputObject $ArgsJson)
} elseif ($PythonArgs) {
    # Compatibility with tasks registered before structured argv was added.
    # PSParser classifies the leading -m as Command and port values as Number.
    $ArgumentList = [System.Management.Automation.PSParser]::Tokenize($PythonArgs, [ref]$null) |
        Where-Object { $_.Type -in @("String", "Command", "CommandArgument", "CommandParameter", "Number") } |
        ForEach-Object { $_.Content }
}

# Windows PowerShell turns native stderr (including normal server logs) into
# error records. Keep it in the log without aborting a healthy child process.
$ErrorActionPreference = "Continue"
& $Python @ArgumentList *>> $LogPath
exit $LASTEXITCODE
