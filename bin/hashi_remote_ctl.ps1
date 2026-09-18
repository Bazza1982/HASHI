param(
    [Parameter(Position = 0)]
    [ValidateSet("register", "enable", "disable", "unregister", "install", "uninstall", "start", "stop", "restart", "status", "logs", "command", "doctor")]
    [string]$Action = "status",

    [string]$HashiRoot,
    [string]$Python,
    [string]$TaskName,
    [string]$TaskUserId = $env:HASHI_REMOTE_TASK_USER,
    [string]$InstanceId = $env:HASHI_INSTANCE_ID,
    [string]$MaxTerminalLevel = $env:HASHI_REMOTE_MAX_TERMINAL_LEVEL,
    [string]$Discovery = $env:HASHI_REMOTE_DISCOVERY,
    [string]$Port = $env:HASHI_REMOTE_PORT,
    [switch]$NoTls
)

$ErrorActionPreference = "Stop"

if (-not $HashiRoot) {
    $HashiRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
}

if (-not $Python) {
    $VenvPython = Join-Path $HashiRoot ".venv\Scripts\python.exe"
    if (Test-Path $VenvPython) {
        $Python = $VenvPython
    } else {
        $Python = "python"
    }
}

$IdentityScript = Join-Path $HashiRoot "remote\supervisor_identity.py"
if (-not (Test-Path $IdentityScript)) {
    throw "Missing supervisor identity helper: $IdentityScript"
}
$IdentityArgs = @($IdentityScript, "--hashi-root", $HashiRoot, "--format", "json")
if ($InstanceId) {
    $IdentityArgs += @("--instance-id", $InstanceId)
}
$IdentityJson = & $Python @IdentityArgs
if ($LASTEXITCODE -ne 0) {
    throw "Could not resolve the Hashi Remote supervisor identity."
}
$SupervisorIdentity = $IdentityJson | ConvertFrom-Json
if (-not $TaskName) {
    $TaskName = $SupervisorIdentity.windows_task_name
}
if ($TaskName -match '[\\/]') {
    throw "Invalid scheduled task name: $TaskName"
}

$LogDir = Join-Path $HashiRoot "logs"
$LogPath = Join-Path $LogDir "hashi-remote-supervisor.log"
$ArgsList = @(
    "-m", "remote",
    "--hashi-root", [string]$HashiRoot,
    "--supervised",
    "--instance-id", [string]$SupervisorIdentity.instance_id
)
if ($SupervisorIdentity.display_name) {
    $ArgsList += @("--display-name", [string]$SupervisorIdentity.display_name)
}
if ($SupervisorIdentity.workbench_port) {
    $ArgsList += @("--workbench-port", [string]$SupervisorIdentity.workbench_port)
}
$TaskRunner = Join-Path $PSScriptRoot "hashi_remote_task_runner.ps1"

if ($NoTls -or $env:HASHI_REMOTE_NO_TLS -eq "1") {
    $ArgsList += "--no-tls"
}
if ($MaxTerminalLevel) {
    $ArgsList += @("--max-terminal-level", $MaxTerminalLevel)
}
if ($Discovery) {
    $ArgsList += @("--discovery", $Discovery)
}
if ($Port) {
    $ArgsList += @("--port", $Port)
}

$ArgumentString = ($ArgsList -join " ")
$CommandPreview = "$Python $ArgumentString"

function Ensure-LogDir {
    New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
}

function Resolve-RemoteTaskPrincipal {
    $Candidate = $TaskUserId
    $Current = [System.Security.Principal.WindowsIdentity]::GetCurrent()
    if (-not $Candidate) {
        $CurrentSid = [string]$Current.User.Value
        if ($CurrentSid -in @("S-1-5-18", "S-1-5-19", "S-1-5-20")) {
            throw "Remote setup is running as a Windows service account. Pass -TaskUserId for the intended Limited user."
        }
        $Candidate = [string]$Current.Name
    }
    try {
        $Account = New-Object System.Security.Principal.NTAccount($Candidate)
        $Sid = [string]$Account.Translate(
            [System.Security.Principal.SecurityIdentifier]
        ).Value
    } catch {
        throw "Could not resolve Remote task principal '$Candidate' to a Windows SID: $($_.Exception.Message)"
    }
    [PSCustomObject]@{
        UserId = $Candidate
        Sid = $Sid
    }
}

function Protect-RemoteCredentialAccess {
    param([Parameter(Mandatory = $true)]$Principal)

    $SecretsPath = Join-Path $HashiRoot "secrets.json"
    if (-not (Test-Path -LiteralPath $SecretsPath -PathType Leaf)) {
        return
    }
    $CurrentSid = [string][System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    if ($CurrentSid.Equals(
        [string]$Principal.Sid,
        [System.StringComparison]::OrdinalIgnoreCase
    )) {
        $Stream = $null
        try {
            $Share = [System.IO.FileShare]::ReadWrite -bor [System.IO.FileShare]::Delete
            $Stream = [System.IO.File]::Open(
                $SecretsPath,
                [System.IO.FileMode]::Open,
                [System.IO.FileAccess]::Read,
                $Share
            )
            return
        } catch {
            # The configured principal cannot read the credential yet. Continue
            # into the explicit ACL provisioning path below.
        } finally {
            if ($null -ne $Stream) {
                $Stream.Dispose()
            }
        }
    }
    $PrivateFileTool = Join-Path (Split-Path -Parent $PSScriptRoot) "tools\private_files.py"
    if (-not (Test-Path -LiteralPath $PrivateFileTool -PathType Leaf)) {
        throw "Missing private-file ACL helper: $PrivateFileTool"
    }
    $ToolOutput = & $Python $PrivateFileTool `
        --allow-full-control-sid $Principal.Sid `
        $SecretsPath 2>&1
    if ($LASTEXITCODE -ne 0) {
        $Details = ($ToolOutput | Out-String).Trim()
        throw "Could not grant Remote task principal '$($Principal.UserId)' access to secrets.json. $Details"
    }
}

function Register-HashiRemoteSupervisor {
    Ensure-LogDir
    $ResolvedPrincipal = Resolve-RemoteTaskPrincipal
    Protect-RemoteCredentialAccess -Principal $ResolvedPrincipal
    $RunnerArgs = @(
        "-NoProfile",
        "-ExecutionPolicy", "Bypass",
        "-WindowStyle", "Hidden",
        "-File", "`"$TaskRunner`"",
        "-Python", "`"$Python`"",
        "-HashiRoot", "`"$HashiRoot`"",
        "-LogPath", "`"$LogPath`""
    )
    if ($ArgsList.Count -gt 0) {
        # Pass argv as data; nested command-line quotes lose paths and flags.
        $ArgsJson = ConvertTo-Json -InputObject @($ArgsList) -Compress
        $ArgsBase64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($ArgsJson))
        $RunnerArgs += @("-PythonArgsBase64", $ArgsBase64)
    }
    $Action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument ($RunnerArgs -join " ") -WorkingDirectory $HashiRoot
    $Trigger = New-ScheduledTaskTrigger -AtLogOn
    $Settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -RestartCount 999 `
        -RestartInterval (New-TimeSpan -Minutes 1) `
        -Priority 4
    $Principal = New-ScheduledTaskPrincipal -UserId $ResolvedPrincipal.UserId -LogonType Interactive -RunLevel Limited
    $Task = New-ScheduledTask -Action $Action -Trigger $Trigger -Settings $Settings -Principal $Principal
    Register-ScheduledTask -TaskName $TaskName -InputObject $Task -Force -ErrorAction Stop | Out-Null
    Write-Host "Registered and enabled Remote supervisor task '$TaskName'"
    Write-Host $CommandPreview
}

function Get-RemotePort {
    if ($Port) {
        return [int]$Port
    }
    if ($null -ne $SupervisorIdentity.remote_port) {
        return [int]$SupervisorIdentity.remote_port
    }
    $ConfigPath = Join-Path $HashiRoot "remote\config.yaml"
    if (Test-Path $ConfigPath) {
        $Match = Select-String -Path $ConfigPath -Pattern '^\s*port:\s*(\d+)' | Select-Object -First 1
        if ($Match -and $Match.Matches.Count -gt 0) {
            return [int]$Match.Matches[0].Groups[1].Value
        }
    }
    return 8766
}

function Get-RemoteUseTls {
    if ($NoTls -or $env:HASHI_REMOTE_NO_TLS -eq "1") {
        return $false
    }
    $ConfigPath = Join-Path $HashiRoot "remote\config.yaml"
    if (Test-Path $ConfigPath) {
        $Match = Select-String -Path $ConfigPath -Pattern '^\s*use_tls:\s*(true|false|yes|no|1|0)\s*(?:#.*)?$' |
            Select-Object -First 1
        if ($Match -and $Match.Matches.Count -gt 0) {
            return $Match.Matches[0].Groups[1].Value -match '^(?i:true|yes|1)$'
        }
    }
    return $true
}

function Get-RemoteHealthProbe {
    param([int]$EffectivePort)

    $Schemes = if (Get-RemoteUseTls) { @("https", "http") } else { @("http") }
    $LastError = $null
    foreach ($Scheme in $Schemes) {
        $Uri = "${Scheme}://127.0.0.1:$EffectivePort/health"
        $PreviousCertificateCallback = [System.Net.ServicePointManager]::ServerCertificateValidationCallback
        try {
            if ($Scheme -eq "https") {
                # The bundled Remote certificate is local and self-signed. This
                # callback is scoped to this short-lived controller process and
                # the loopback health URL only.
                [System.Net.ServicePointManager]::ServerCertificateValidationCallback = { $true }
            }
            $Health = Invoke-RestMethod -Method Get -Uri $Uri -TimeoutSec 2
            return [PSCustomObject]@{
                Health = $Health
                Uri = $Uri
                Error = $null
            }
        } catch {
            $LastError = $_.Exception.Message
        } finally {
            [System.Net.ServicePointManager]::ServerCertificateValidationCallback = $PreviousCertificateCallback
        }
    }
    return [PSCustomObject]@{
        Health = $null
        Uri = $null
        Error = $LastError
    }
}

function Get-RemoteHealthAcceptance {
    param($Probe)

    $Health = if ($null -ne $Probe) { $Probe.Health } else { $null }
    if ($null -eq $Health) {
        return [PSCustomObject]@{
            Accepted = $false
            Mode = "unreachable"
            Reason = if ($Probe -and $Probe.Error) { $Probe.Error } else { "Remote health is unreachable" }
        }
    }
    $ActualInstance = [string]$Health.instance.instance_id
    if (-not $ActualInstance.Equals(
        [string]$SupervisorIdentity.instance_id,
        [System.StringComparison]::OrdinalIgnoreCase
    )) {
        return [PSCustomObject]@{
            Accepted = $false
            Mode = "wrong_instance"
            Reason = "Health endpoint belongs to '$ActualInstance', not '$($SupervisorIdentity.instance_id)'"
        }
    }

    $Discovery = $Health.discovery
    if ($null -eq $Discovery) {
        return [PSCustomObject]@{
            Accepted = $false
            Mode = "discovery_unavailable"
            Reason = "Remote health did not expose discovery state"
        }
    }
    $Backends = @($Discovery.backends)
    $Advertising = $Backends.Count -gt 0 -and
        @($Backends | Where-Object { $_.advertising -ne $true }).Count -eq 0
    $Browsing = $Backends.Count -gt 0 -and
        @($Backends | Where-Object { $_.browsing -ne $true }).Count -eq 0
    $State = [string]$Discovery.state
    $Readiness = [string]$Discovery.readiness
    $TrustState = [string]$Discovery.trust_state
    $PeerCount = [int]($Discovery.peer_count)
    $TrustedPeerCount = [int]($Discovery.trusted_peer_count)
    if ([string]$Health.status -ne "ready" -or $Readiness -ne "ready" -or -not $Advertising -or -not $Browsing) {
        return [PSCustomObject]@{
            Accepted = $false
            Mode = if ($State) { $State } else { "degraded" }
            Reason = "discovery is not ready (state=$State advertising=$Advertising browsing=$Browsing)"
        }
    }
    if ($PeerCount -eq 0 -and $TrustState -eq "no_peers" -and $State -eq "ready_empty") {
        return [PSCustomObject]@{
            Accepted = $true
            Mode = "ready_empty"
            Reason = "advertising and browsing are ready; no peer is currently visible"
        }
    }
    if ($TrustedPeerCount -gt 0 -and $TrustState -eq "accepted" -and $State -eq "ready") {
        return [PSCustomObject]@{
            Accepted = $true
            Mode = "trusted_peer"
            Reason = "$TrustedPeerCount trusted peer handshake(s) accepted"
        }
    }
    return [PSCustomObject]@{
        Accepted = $false
        Mode = if ($TrustState) { $TrustState } else { "trust_unknown" }
        Reason = "peer discovery has no accepted trusted handshake (peers=$PeerCount trusted=$TrustedPeerCount trust=$TrustState)"
    }
}

function Wait-RemoteHealthAcceptance {
    param([int]$EffectivePort)

    $LastAcceptance = $null
    # The HTTP listener can become reachable before mDNS discovery and trusted
    # peer handshakes settle. Allow a bounded 30-second adoption window instead
    # of reporting a false failure after the old six-second probe window.
    $MaxAttempts = 120
    for ($Attempt = 0; $Attempt -lt $MaxAttempts; $Attempt++) {
        $Probe = Get-RemoteHealthProbe -EffectivePort $EffectivePort
        $Acceptance = Get-RemoteHealthAcceptance -Probe $Probe
        if ($Acceptance.Accepted) {
            Write-Host "Remote startup accepted: $($Acceptance.Mode) - $($Acceptance.Reason)"
            return $Acceptance
        }
        $LastAcceptance = $Acceptance
        if ($Attempt -lt ($MaxAttempts - 1)) {
            Start-Sleep -Milliseconds 250
        }
    }
    $Reason = if ($LastAcceptance) { $LastAcceptance.Reason } else { "health unavailable" }
    throw "Remote startup acceptance failed: $Reason"
}

function Test-OwnedRemoteCommandLine {
    param([string]$CommandLine)

    if ([string]::IsNullOrWhiteSpace($CommandLine)) {
        return $false
    }
    $isRemote = $CommandLine -match '(?i)(^|\s)-m\s+remote(\s|$)'
    $isTaskRunner = $CommandLine -match [regex]::Escape($TaskRunner)
    if (-not ($isRemote -or $isTaskRunner)) {
        return $false
    }
    if ($CommandLine -notmatch '(?i)(?:--hashi-root|-HashiRoot)\s+(?:"([^"]+)"|(\S+))') {
        return $false
    }
    $Candidate = if ($matches[1]) { $matches[1] } else { $matches[2] }
    try {
        $Resolved = ([System.IO.Path]::GetFullPath($Candidate)).TrimEnd('\')
    } catch {
        return $false
    }
    return $Resolved.Equals(
        ([System.IO.Path]::GetFullPath($HashiRoot)).TrimEnd('\'),
        [System.StringComparison]::OrdinalIgnoreCase
    )
}

function Get-OwnedRemoteProcesses {
    return @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
        [int]$_.ProcessId -ne $PID -and (Test-OwnedRemoteCommandLine ([string]$_.CommandLine))
    })
}

function Stop-OwnedRemoteProcesses {
    param([int]$GraceSeconds = 3)

    for ($i = 0; $i -lt ($GraceSeconds * 4); $i++) {
        if (@(Get-OwnedRemoteProcesses).Count -eq 0) {
            return $true
        }
        Start-Sleep -Milliseconds 250
    }

    # Task Scheduler can report Ready after Stop-ScheduledTask while the runner's
    # native child remains alive. Match the exact --hashi-root and retire only
    # that instance's Python/runner chain. Windows can keep a force-terminated
    # Python shim visible through CIM for several seconds, so poll for a bounded
    # 15 seconds instead of reporting a false restart failure immediately.
    $ForcePollAttempts = 15 * 4
    for ($attempt = 0; $attempt -lt $ForcePollAttempts; $attempt++) {
        $Owned = @(Get-OwnedRemoteProcesses)
        if ($Owned.Count -eq 0) {
            return $true
        }
        $Owned | Sort-Object `
            @{ Expression = { if ([string]$_.Name -match '(?i)^python') { 0 } else { 1 } }; Ascending = $true }, `
            @{ Expression = 'ProcessId'; Descending = $true } | ForEach-Object {
            Stop-Process -Id ([int]$_.ProcessId) -Force -ErrorAction SilentlyContinue
        }
        Start-Sleep -Milliseconds 250
    }
    return @(Get-OwnedRemoteProcesses).Count -eq 0
}

function Stop-RemoteSupervisor {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if (-not (Stop-OwnedRemoteProcesses)) {
        throw "Remote processes for '$HashiRoot' did not stop"
    }
}

function Show-RemoteDoctor {
    $EffectivePort = Get-RemotePort
    $FirewallRules = Get-NetFirewallRule -Direction Inbound -Enabled True -ErrorAction SilentlyContinue |
        Where-Object { $_.DisplayName -match "Hashi|Remote|Python" }
    $Listening = Get-NetTCPConnection -LocalPort $EffectivePort -State Listen -ErrorAction SilentlyContinue
    $WslStatus = $null
    $WslError = $null
    $WslCommand = Get-Command wsl.exe -ErrorAction SilentlyContinue
    $WslAvailable = $null -ne $WslCommand
    if ($WslAvailable) {
        try {
            $WslStatus = (& $WslCommand.Source --status 2>$null) -join "`n"
        } catch {
            # WSL is diagnostic context only. A policy-blocked or unavailable
            # executable must not hide an otherwise healthy Remote result.
            $WslAvailable = $false
            $WslError = $_.Exception.Message
        }
    }
    $Probe = Get-RemoteHealthProbe -EffectivePort $EffectivePort
    $Health = $Probe.Health
    $HealthError = $Probe.Error
    $Acceptance = Get-RemoteHealthAcceptance -Probe $Probe
    $Discovery = if ($null -ne $Health) { $Health.discovery } else { $null }
    $Backends = if ($null -ne $Discovery) { @($Discovery.backends) } else { @() }
    $Advertising = if ($Backends.Count -gt 0) {
        @($Backends | Where-Object { $_.advertising -ne $true }).Count -eq 0
    } else { $false }
    $Browsing = if ($Backends.Count -gt 0) {
        @($Backends | Where-Object { $_.browsing -ne $true }).Count -eq 0
    } else { $false }
    [PSCustomObject]@{
        HashiRoot = $HashiRoot
        RemotePort = $EffectivePort
        Listening = [bool]$Listening
        FirewallRuleCount = @($FirewallRules).Count
        FirewallRules = @($FirewallRules | Select-Object -ExpandProperty DisplayName)
        WslAvailable = $WslAvailable
        WslStatus = $WslStatus
        WslError = $WslError
        RemoteReachable = $null -ne $Health
        RemoteHealthUri = $Probe.Uri
        RemoteHealthState = if ($null -ne $Health) { $Health.status } else { "unreachable" }
        RemoteHealthMode = $Acceptance.Mode
        RemoteAccepted = [bool]$Acceptance.Accepted
        AcceptanceReason = $Acceptance.Reason
        DiscoveryState = if ($null -ne $Discovery) { $Discovery.state } else { "unavailable" }
        DiscoveryReadiness = if ($null -ne $Discovery) { $Discovery.readiness } else { "unavailable" }
        Advertising = $Advertising
        Browsing = $Browsing
        PeerCount = if ($null -ne $Discovery) { $Discovery.peer_count } else { 0 }
        TrustState = if ($null -ne $Discovery) { $Discovery.trust_state } else { "unknown" }
        TrustedPeerCount = if ($null -ne $Discovery) { $Discovery.trusted_peer_count } else { 0 }
        StaticSeedFallbackActive = if ($null -ne $Discovery) { $Discovery.static_seed_fallback_active } else { $false }
        DiscoveryErrors = @($Backends | Where-Object { $_.last_error } | ForEach-Object { "$($_.backend): $($_.last_error)" })
        HealthError = $HealthError
        Command = $CommandPreview
    } | Format-List
    if (-not $Acceptance.Accepted) {
        exit 2
    }
}

switch ($Action) {
    { $_ -in "register", "install" } {
        Register-HashiRemoteSupervisor
    }
    "enable" {
        Register-HashiRemoteSupervisor
        Enable-ScheduledTask -TaskName $TaskName | Out-Null
        Start-ScheduledTask -TaskName $TaskName
        Wait-RemoteHealthAcceptance -EffectivePort (Get-RemotePort) | Out-Null
        Write-Host "Activated Remote supervisor task '$TaskName'"
    }
    "disable" {
        Stop-RemoteSupervisor
        Disable-ScheduledTask -TaskName $TaskName | Out-Null
        Write-Host "Disabled Remote supervisor task '$TaskName'"
    }
    { $_ -in "unregister", "uninstall" } {
        Stop-RemoteSupervisor
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
        Write-Host "Unregistered Remote supervisor task '$TaskName'"
    }
    "start" {
        $ResolvedPrincipal = Resolve-RemoteTaskPrincipal
        Protect-RemoteCredentialAccess -Principal $ResolvedPrincipal
        Start-ScheduledTask -TaskName $TaskName
        Wait-RemoteHealthAcceptance -EffectivePort (Get-RemotePort) | Out-Null
    }
    "stop" {
        Stop-RemoteSupervisor
    }
    "restart" {
        $ResolvedPrincipal = Resolve-RemoteTaskPrincipal
        Protect-RemoteCredentialAccess -Principal $ResolvedPrincipal
        Stop-RemoteSupervisor
        Start-ScheduledTask -TaskName $TaskName
        Wait-RemoteHealthAcceptance -EffectivePort (Get-RemotePort) | Out-Null
    }
    "status" {
        $Task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        if ($null -eq $Task) {
            Write-Host "Remote supervisor task '$TaskName' is not registered"
            exit 2
        }
        $Info = Get-ScheduledTaskInfo -TaskName $TaskName
        [PSCustomObject]@{
            InstanceId = $SupervisorIdentity.instance_id
            TaskName = $Task.TaskName
            State = $Task.State
            LastRunTime = $Info.LastRunTime
            LastTaskResult = $Info.LastTaskResult
            NextRunTime = $Info.NextRunTime
            UserId = $Task.Principal.UserId
            Command = $CommandPreview
        } | Format-List
    }
    "logs" {
        if (Test-Path $LogPath) {
            Get-Content -Path $LogPath -Tail 120
        } else {
            Write-Host "No supervisor log found at $LogPath"
        }
    }
    "command" {
        [PSCustomObject]@{
            InstanceId = $SupervisorIdentity.instance_id
            TaskName = $TaskName
            Command = $CommandPreview
        } | Format-List
    }
    "doctor" {
        Show-RemoteDoctor
    }
}
