param(
    [string]$BridgeHome = (Split-Path -Parent $PSScriptRoot),
    [string]$CodeRoot = "",
    [string]$PythonwExe = "",
    [string]$BindHost = "auto",
    [string]$AdvertiseHost = "",
    [string]$BrowserEndpoint = "",
    [string]$BrowserAuthFile = "",
    [string]$LogDir = "",
    [switch]$ComputerOnly,
    [switch]$BrowserOnly,
    [switch]$NoStart
)

$ErrorActionPreference = "Stop"

function Resolve-FileSystemPath {
    param([Parameter(Mandatory = $true)][string]$Path)

    $Resolved = Resolve-Path -LiteralPath $Path
    if ($Resolved.Provider.Name -ne "FileSystem") {
        throw "Path is not on the FileSystem provider: $Path"
    }
    return $Resolved.ProviderPath
}

$BridgeHome = Resolve-FileSystemPath $BridgeHome
$CodeRoot = if ([string]::IsNullOrWhiteSpace($CodeRoot)) {
    $BridgeHome
}
else {
    Resolve-FileSystemPath $CodeRoot
}
$ConfigPath = Join-Path $BridgeHome "agents.json"
if (-not (Test-Path -LiteralPath $ConfigPath -PathType Leaf)) {
    throw "HASHI configuration not found: $ConfigPath"
}

$Config = Get-Content -LiteralPath $ConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json
$InstanceId = [string]$Config.global.instance_id
if ([string]::IsNullOrWhiteSpace($InstanceId)) {
    throw "global.instance_id is required in $ConfigPath"
}
$InstanceId = $InstanceId.ToUpperInvariant()

if ([string]::IsNullOrWhiteSpace($LogDir)) {
    $LogDir = Join-Path $env:LOCALAPPDATA "HASHI\device_control\$InstanceId\logs"
}

if (-not $PythonwExe) {
    $PythonwExe = Join-Path $BridgeHome ".venv\Scripts\pythonw.exe"
}
if (-not (Test-Path -LiteralPath $PythonwExe -PathType Leaf)) {
    throw "Windowless Python executable not found: $PythonwExe"
}
$PythonwExe = (Resolve-Path -LiteralPath $PythonwExe).Path

if ($ComputerOnly -and $BrowserOnly) {
    throw "Choose at most one of -ComputerOnly or -BrowserOnly"
}
if ([string]::IsNullOrWhiteSpace($BindHost)) {
    throw "BindHost must not be empty"
}
if (-not $AdvertiseHost) {
    if ($BindHost -in @("0.0.0.0", "::")) {
        throw "AdvertiseHost is required when BindHost is a wildcard"
    }
    $AdvertiseHost = $BindHost
}

$CurrentUser = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$TaskPrefix = "HASHI-$InstanceId-DeviceControl"

function Quote-TaskArgument {
    param([string]$Value)
    return '"' + $Value.Replace('"', '\"') + '"'
}

function Install-CapabilityTask {
    param(
        [Parameter(Mandatory = $true)][string]$Kind,
        [Parameter(Mandatory = $true)][string]$TaskSuffix
    )

    $Arguments = @(
        "-m",
        "tools.device_control_worker",
        "--kind", $Kind,
        "--bridge-home", $BridgeHome,
        "--instance-id", $InstanceId,
        "--host", $BindHost,
        "--advertise-host", $AdvertiseHost,
        "--log-dir", $LogDir
    )
    if ($Kind -eq "browser_control") {
        if ($BrowserEndpoint) {
            $Arguments += @("--browser-endpoint", $BrowserEndpoint)
        }
        if ($BrowserAuthFile) {
            $Arguments += @("--browser-auth-file", $BrowserAuthFile)
        }
    }
    $ArgumentLine = ($Arguments | ForEach-Object { Quote-TaskArgument ([string]$_) }) -join " "
    $Action = New-ScheduledTaskAction `
        -Execute $PythonwExe `
        -Argument $ArgumentLine `
        -WorkingDirectory $CodeRoot
    $Trigger = New-ScheduledTaskTrigger -AtLogOn -User $CurrentUser
    $Principal = New-ScheduledTaskPrincipal `
        -UserId $CurrentUser `
        -LogonType Interactive `
        -RunLevel Limited
    $Settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -ExecutionTimeLimit ([TimeSpan]::Zero) `
        -MultipleInstances IgnoreNew `
        -RestartCount 3 `
        -RestartInterval (New-TimeSpan -Minutes 1)
    $TaskName = "$TaskPrefix-$TaskSuffix"
    $ExistingTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($null -ne $ExistingTask) {
        Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    }
    Register-ScheduledTask `
        -TaskName $TaskName `
        -Action $Action `
        -Trigger $Trigger `
        -Principal $Principal `
        -Settings $Settings `
        -Description "Persistent authenticated HASHI $Kind worker for $InstanceId" `
        -Force | Out-Null
    if (-not $NoStart) {
        Start-ScheduledTask -TaskName $TaskName
    }
    Write-Host "Installed $TaskName"
}

if (-not $BrowserOnly) {
    Install-CapabilityTask -Kind "computer_control" -TaskSuffix "Computer"
}
if (-not $ComputerOnly) {
    Install-CapabilityTask -Kind "browser_control" -TaskSuffix "Browser"
}

Write-Host "Device Control Workers installed for $InstanceId using pythonw.exe."
