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
$CaptureMaxBytes = 1MB
$CaptureBackupCount = 2
$CaptureStream = $null

function Close-BoundedCapture {
    if ($null -eq $script:CaptureStream) {
        return
    }
    try {
        $script:CaptureStream.Flush()
    } finally {
        $script:CaptureStream.Dispose()
        $script:CaptureStream = $null
    }
}

function Limit-CaptureFileTail {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,
        [Parameter(Mandatory = $true)]
        [long]$MaxBytes
    )

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return
    }
    $Length = (Get-Item -LiteralPath $Path).Length
    if ($Length -le $MaxBytes) {
        return
    }

    $Buffer = New-Object byte[] ([int]$MaxBytes)
    $Source = [System.IO.File]::Open(
        $Path,
        [System.IO.FileMode]::Open,
        [System.IO.FileAccess]::Read,
        [System.IO.FileShare]::ReadWrite
    )
    try {
        [void]$Source.Seek(-$MaxBytes, [System.IO.SeekOrigin]::End)
        $Offset = 0
        while ($Offset -lt $Buffer.Length) {
            $Read = $Source.Read($Buffer, $Offset, $Buffer.Length - $Offset)
            if ($Read -le 0) {
                break
            }
            $Offset += $Read
        }
    } finally {
        $Source.Dispose()
    }

    $Target = [System.IO.File]::Open(
        $Path,
        [System.IO.FileMode]::Create,
        [System.IO.FileAccess]::Write,
        [System.IO.FileShare]::Read
    )
    try {
        $Start = 0
        while (
            $Start -lt $Offset -and
            (($Buffer[$Start] -band 0xC0) -eq 0x80)
        ) {
            $Start++
        }
        $Target.Write($Buffer, $Start, $Offset - $Start)
        $Target.Flush()
    } finally {
        $Target.Dispose()
    }
}

function Rotate-BoundedCapture {
    Close-BoundedCapture
    $Oldest = "$script:CapturePath.$script:CaptureBackupCount"
    if (Test-Path -LiteralPath $Oldest) {
        Remove-Item -LiteralPath $Oldest -Force
    }
    for ($Index = $script:CaptureBackupCount - 1; $Index -ge 1; $Index--) {
        $Source = "$script:CapturePath.$Index"
        if (Test-Path -LiteralPath $Source) {
            Move-Item -LiteralPath $Source -Destination "$script:CapturePath.$($Index + 1)" -Force
        }
    }
    if (Test-Path -LiteralPath $script:CapturePath) {
        Move-Item -LiteralPath $script:CapturePath -Destination "$script:CapturePath.1" -Force
    }
}

function Open-BoundedCapture {
    if ($null -ne $script:CaptureStream) {
        return
    }
    $script:CaptureStream = [System.IO.File]::Open(
        $script:CapturePath,
        [System.IO.FileMode]::OpenOrCreate,
        [System.IO.FileAccess]::Write,
        ([System.IO.FileShare]::Read -bor [System.IO.FileShare]::Delete)
    )
    [void]$script:CaptureStream.Seek(0, [System.IO.SeekOrigin]::End)
}

function Initialize-BoundedCapture {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path
    )

    $script:CapturePath = $Path
    for ($Index = 0; $Index -le $script:CaptureBackupCount; $Index++) {
        $Candidate = if ($Index -eq 0) { $Path } else { "$Path.$Index" }
        Limit-CaptureFileTail -Path $Candidate -MaxBytes $script:CaptureMaxBytes
    }
    if (
        (Test-Path -LiteralPath $Path -PathType Leaf) -and
        (Get-Item -LiteralPath $Path).Length -ge $script:CaptureMaxBytes
    ) {
        Rotate-BoundedCapture
    }
    Open-BoundedCapture
}

function Write-BoundedCaptureRecord {
    param(
        [Parameter(Mandatory = $true, ValueFromPipeline = $true)]
        [AllowEmptyString()]
        [object]$Record
    )

    $Payload = $Utf8NoBom.GetBytes(([string]$Record) + [Environment]::NewLine)
    $Offset = 0
    while ($Offset -lt $Payload.Length) {
        Open-BoundedCapture
        $Remaining = $script:CaptureMaxBytes - $script:CaptureStream.Length
        if ($Remaining -le 0) {
            Rotate-BoundedCapture
            continue
        }

        $Take = [int][Math]::Min([long]($Payload.Length - $Offset), $Remaining)
        if (($Offset + $Take) -lt $Payload.Length) {
            while (
                $Take -gt 0 -and
                (($Payload[$Offset + $Take] -band 0xC0) -eq 0x80)
            ) {
                $Take--
            }
        }
        if ($Take -le 0) {
            Rotate-BoundedCapture
            continue
        }

        $script:CaptureStream.Write($Payload, $Offset, $Take)
        $Offset += $Take
        if ($Offset -lt $Payload.Length) {
            Rotate-BoundedCapture
        }
    }
    $script:CaptureStream.Flush()
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
}
Initialize-BoundedCapture -Path $CapturePath

# Windows PowerShell turns native stderr (including normal server logs) into
# error records. Keep it in the log without aborting a healthy child process.
$ErrorActionPreference = "Continue"
# Legacy registrations must also preserve the sealed interpreter file closure.
$ChildExitCode = 1
try {
    & $Python -B @ArgumentList 2>&1 | ForEach-Object {
        Write-BoundedCaptureRecord -Record $_
    }
    $ChildExitCode = $LASTEXITCODE
} finally {
    Close-BoundedCapture
}
exit $ChildExitCode
