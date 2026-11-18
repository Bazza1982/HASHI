param(
    [Parameter(Mandatory = $true)]
    [string]$Python,

    [Parameter(Mandatory = $true)]
    [string]$HashiRoot,

    [Parameter(Mandatory = $true)]
    [string]$LogPath,

    [string]$CodeRoot = "",

    [string]$PythonArgs = "",

    [string]$PythonArgsBase64 = ""
)

$ErrorActionPreference = "Stop"

# Task Scheduler defaults background tasks to below-normal priority. Under
# sustained host load that can starve Python imports for minutes, so normalize
# legacy registrations here as well as in the task definition.
try {
    [System.Diagnostics.Process]::GetCurrentProcess().PriorityClass =
        [System.Diagnostics.ProcessPriorityClass]::Normal
} catch {
    # A freshly registered task also requests normal priority explicitly.
}

$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
$OutputEncoding = $Utf8NoBom
try {
    [Console]::OutputEncoding = $Utf8NoBom
} catch {
    # Hidden task sessions may not expose a mutable console.
}

$LogDir = Split-Path -Parent $LogPath
if ($LogDir) {
    New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
}
$LauncherLogPath = Join-Path $LogDir "hashi-remote-launcher.log"

function Rotate-LauncherLogIfNeeded {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,
        [long]$MaxBytes = 1MB,
        [int]$BackupCount = 2
    )

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return
    }
    if ((Get-Item -LiteralPath $Path).Length -lt $MaxBytes) {
        return
    }
    $oldest = "$Path.$BackupCount"
    if (Test-Path -LiteralPath $oldest) {
        Remove-Item -LiteralPath $oldest -Force
    }
    for ($index = $BackupCount - 1; $index -ge 1; $index--) {
        $source = "$Path.$index"
        if (Test-Path -LiteralPath $source) {
            Move-Item -LiteralPath $source -Destination "$Path.$($index + 1)" -Force
        }
    }
    Move-Item -LiteralPath $Path -Destination "$Path.1" -Force
}

Set-Location $HashiRoot
if ($CodeRoot) {
    $env:PYTHONPATH = [System.IO.Path]::GetFullPath($CodeRoot)
}
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONLEGACYWINDOWSSTDIO = "utf-8"

$ArgumentList = @()
if ($PythonArgsBase64) {
    $ArgsJson = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($PythonArgsBase64))
    # Windows PowerShell can preserve a JSON array as one nested pipeline item.
    # Enumerate it explicitly so lifecycle flags remain inspectable as well as
    # correctly splatted into the child command.
    $ParsedArguments = ConvertFrom-Json -InputObject $ArgsJson
    $ArgumentList = @(
        foreach ($Argument in $ParsedArguments) {
            [string]$Argument
        }
    )
} elseif ($PythonArgs) {
    # Compatibility with tasks registered before structured argv was added.
    # PSParser classifies the leading -m as Command and port values as Number.
    $ArgumentList = [System.Management.Automation.PSParser]::Tokenize($PythonArgs, [ref]$null) |
        Where-Object { $_.Type -in @("String", "Command", "CommandArgument", "CommandParameter", "Number") } |
        ForEach-Object { $_.Content }
}

$IsRemoteModule = $false
for ($index = 0; $index -lt ($ArgumentList.Count - 1); $index++) {
    if ($ArgumentList[$index] -eq "-m" -and $ArgumentList[$index + 1] -eq "remote") {
        $IsRemoteModule = $true
        break
    }
}
if ($IsRemoteModule -and -not ($ArgumentList -contains "--log-file")) {
    # Existing Scheduled Tasks keep their sealed argv. Injecting the new sink in
    # the runner lets the next ordinary Remote restart adopt bounded logging
    # without deleting and recreating the task first.
    $ArgumentList += @("--log-file", $LogPath)
}
$CapturePath = $LogPath
if ($ArgumentList -contains "--log-file") {
    # The Remote process owns and rotates its primary log. Keep only bootstrap,
    # import and native-launch failures in a small separately rotated file so the
    # parent never holds the primary log open during rollover.
    $CapturePath = $LauncherLogPath
    Rotate-LauncherLogIfNeeded -Path $CapturePath
}

# Windows PowerShell turns native stderr (including normal server logs) into
# error records. Keep it in the log without aborting a healthy child process.
$ErrorActionPreference = "Continue"
# Legacy registrations must also preserve the sealed interpreter file closure.
& $Python -B @ArgumentList 2>&1 |
    Out-File -LiteralPath $CapturePath -Append -Encoding utf8
exit $LASTEXITCODE
