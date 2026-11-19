param(
    [Parameter(Position = 0)]
    [ValidateSet("register", "enable", "disable", "unregister", "install", "uninstall", "start", "stop", "restart", "status", "logs", "command", "doctor")]
    [string]$Action = "status",

    [string]$HashiRoot,
    [string]$CodeRoot,
    [string]$Python,
    [string]$TaskName,
    [string]$TaskUserId = $env:HASHI_REMOTE_TASK_USER,
    [string]$InstanceId = $env:HASHI_INSTANCE_ID,
    [string]$MaxTerminalLevel = $env:HASHI_REMOTE_MAX_TERMINAL_LEVEL,
    [string]$Discovery = $env:HASHI_REMOTE_DISCOVERY,
    [string]$Port = $env:HASHI_REMOTE_PORT,
    [switch]$NoTls,
    [switch]$Json,
    [ValidateSet("Text", "Json")] [string]$OutputFormat = "Text",
    [switch]$Repair,
    # Test seam only: dot-source a stub file that shadows platform cmdlets so
    # deterministic doctor/repair contracts can be exercised without touching
    # the real firewall or triggering UAC. Empty in production.
    [string]$DiagnosticsStubFile = ""
)

$ErrorActionPreference = "Stop"

if ($Json) {
    $OutputFormat = "Json"
}

if ($DiagnosticsStubFile) {
    $StubPath = [System.IO.Path]::GetFullPath($DiagnosticsStubFile)
    if (-not (Test-Path -LiteralPath $StubPath -PathType Leaf)) {
        throw "Diagnostics stub file not found: $StubPath"
    }
    . $StubPath
}

if (-not $HashiRoot) {
    $HashiRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
}

# The instance home owns identity, configuration and state. Program assets
# may live in a separate checkout or installed distribution.
if (-not $CodeRoot) {
    $CodeRoot = $HashiRoot
}
$CodeRoot = [System.IO.Path]::GetFullPath($CodeRoot)
$HashiRoot = [System.IO.Path]::GetFullPath($HashiRoot)

if (-not $Python) {
    $VenvPython = Join-Path $HashiRoot ".venv\Scripts\python.exe"
    if (Test-Path $VenvPython) {
        $Python = $VenvPython
    } else {
        $Python = "python"
    }
}

$IdentityScript = Join-Path $CodeRoot "remote\supervisor_identity.py"
if (-not (Test-Path $IdentityScript)) {
    throw "Missing supervisor identity helper: $IdentityScript"
}
$IdentityArgs = @("-B", $IdentityScript, "--hashi-root", $HashiRoot, "--format", "json")
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
    "-B", "-m", "remote",
    "--hashi-root", [string]$HashiRoot,
    "--supervised",
    "--log-file", [string]$LogPath,
    "--instance-id", [string]$SupervisorIdentity.instance_id
)
if ($SupervisorIdentity.display_name) {
    $ArgsList += @("--display-name", [string]$SupervisorIdentity.display_name)
}
if ($SupervisorIdentity.workbench_port) {
    $ArgsList += @("--workbench-port", [string]$SupervisorIdentity.workbench_port)
}
$TaskRunner = Join-Path $PSScriptRoot "hashi_remote_task_runner.ps1"
$RestartController = Join-Path $CodeRoot "bin\hashi_restart_ctl.ps1"

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
    # An elevated process can read through Administrators even when its
    # Limited task, with the same user SID, cannot. Check the intended user's
    # actual grant instead of treating an elevated read as runtime evidence.
    try {
        $Acl = Get-Acl -LiteralPath $SecretsPath
        $Rules = $Acl.GetAccessRules($true, $true, [System.Security.Principal.SecurityIdentifier])
        foreach ($Rule in $Rules) {
            if ([string]$Rule.IdentityReference.Value -eq [string]$Principal.Sid -and
                $Rule.AccessControlType -eq [System.Security.AccessControl.AccessControlType]::Allow -and
                ($Rule.FileSystemRights -band [System.Security.AccessControl.FileSystemRights]::FullControl) -eq
                    [System.Security.AccessControl.FileSystemRights]::FullControl) {
                return
            }
        }
    } catch {
        # The canonical provisioning helper below reports any remaining error.
    }
    $PrivateFileTool = Join-Path (Split-Path -Parent $PSScriptRoot) "tools\private_files.py"
    if (-not (Test-Path -LiteralPath $PrivateFileTool -PathType Leaf)) {
        throw "Missing private-file ACL helper: $PrivateFileTool"
    }
    $ToolOutput = & $Python -B $PrivateFileTool `
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
    if (Test-Path -LiteralPath $RestartController -PathType Leaf) {
        try {
            & $RestartController ensure `
                -HashiRoot $HashiRoot `
                -CodeRoot $CodeRoot `
                -Python $Python `
                -TaskUserId $ResolvedPrincipal.UserId `
                -InstanceId $SupervisorIdentity.instance_id
        } catch {
            throw "Fixed instance lifecycle setup failed: $($_.Exception.Message)"
        }
    }
    $RunnerArgs = @(
        "-NoProfile",
        "-ExecutionPolicy", "Bypass",
        "-WindowStyle", "Hidden",
        "-File", "`"$TaskRunner`"",
        "-Python", "`"$Python`"",
        "-HashiRoot", "`"$HashiRoot`"",
        "-CodeRoot", "`"$CodeRoot`"",
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
        -DontStopOnIdleEnd `
        -StartWhenAvailable `
        -ExecutionTimeLimit ([TimeSpan]::Zero) `
        -MultipleInstances IgnoreNew `
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
    if ($PeerCount -gt 0 -and $TrustedPeerCount -eq 0 -and $TrustState -eq "rejected") {
        # Rejecting foreign advertisements does not disable this local service.
        # The ready listener, instance identity and discovery backends above are
        # still required. Peer authentication remains rejected and visible.
        return [PSCustomObject]@{
            Accepted = $true
            Mode = "local_ready_untrusted_peers"
            Reason = "0 trusted peers accepted; untrusted peers remain rejected; local service is ready"
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

function Get-RemoteFirewallRuleNames {
    param([string]$InstanceId, [int]$EffectivePort)
    $SafeInstance = ([string]$InstanceId).Trim()
    return [PSCustomObject]@{
        Tcp = "HashiRemote_${SafeInstance}_TCP_${EffectivePort}"
        Mdns = "HashiRemote_mDNS_UDP_5353"
    }
}

function Test-RemotePortSpecCovers {
    param([string]$PortSpec, [int]$EffectivePort)
    if ([string]::IsNullOrWhiteSpace($PortSpec)) { return $false }
    if ($PortSpec -eq "Any") { return $true }
    foreach ($Part in @($PortSpec -split ",")) {
        $Range = $Part.Trim()
        if ($Range -eq "$EffectivePort") { return $true }
        if ($Range -match '^(\d+)\s*-\s*(\d+)$') {
            $Low = [int]$Matches[1]
            $High = [int]$Matches[2]
            if ($EffectivePort -ge $Low -and $EffectivePort -le $High) { return $true }
        }
    }
    return $false
}

function Test-RemotePortSpecExact {
    param([string]$PortSpec, [int]$EffectivePort)
    # Exact-configuration check for HASHI-owned Allow rules only. A range or
    # "Any" may legitimately COVER the port for Block-rule evidence, but the
    # narrow HASHI template requires the exact single port. An Allow rule
    # exposing "Any" or a whole range is never reported as correctly
    # configured merely because it happens to include the Remote port.
    if ([string]::IsNullOrWhiteSpace($PortSpec)) { return $false }
    $Parts = @($PortSpec -split "[, ]+" | ForEach-Object { $_.Trim() } | Where-Object { $_ })
    if ($Parts.Count -eq 0) { return $false }
    foreach ($Part in $Parts) {
        if ($Part -match '^\d+\s*-\s*\d+$') {
            # A port range is never the narrow single-port configuration.
            return $false
        }
        if ($Part -ne "$EffectivePort") {
            # "Any", a wrong port, or an unrelated value are all non-compliant.
            return $false
        }
    }
    return $true
}

function Test-RemoteGpoTrue {
    param($Value)
    # Windows PowerShell 5.1 NetSecurity cmdlets expose Enabled / AllowLocalFirewallRules
    # as GpoBoolean enums (True/False/NotConfigured). A bare [bool] cast treats
    # NotConfigured as $true and would misreport unconfigured policy as enforced.
    # Compare the canonical string form instead; plain booleans and strings from
    # test stubs follow the same rule ('True' is the only true value).
    return ([string]$Value).Trim() -eq 'True'
}

function Convert-RemoteGpoStringList {
    param($Value)
    # Profile/Direction/Action are enum-valued in PS 5.1 (GpoProfile/GpoDirection/
    # GpoAction) and plain strings in later shells and stubs. Render canonical
    # names so every downstream -eq / -notin comparison is type-stable.
    return @(@($Value) | ForEach-Object { ([string]$_).Trim() } | Where-Object { $_ })
}
function Get-RemoteFirewallDiagnostics {
    param([int]$EffectivePort, [string]$InstanceId, [string]$ListenerProcessPath = "")

    $Diagnostics = [ordered]@{
        state = "unknown"
        reason_code = "firewall_policy_unverified"
        effective_policy_checked = $false
        tcp_rule = $null
        tcp_rule_issues = @()
        mdns_rule = $null
        mdns_rule_issues = @()
        profiles = @()
        explicit_block_evidence = @()
        local_rules_ignored_by_policy = $false
        legacy_rule_matches = @()
    }

    if (-not (Get-Command Get-NetFirewallRule -ErrorAction SilentlyContinue)) {
        $Diagnostics.reason_code = "diagnostics_unavailable"
        return $Diagnostics
    }

    $Names = Get-RemoteFirewallRuleNames -InstanceId $InstanceId -EffectivePort $EffectivePort
    try {
        $Rules = @(Get-NetFirewallRule -PolicyStore ActiveStore -ErrorAction Stop)
        $Profiles = @(Get-NetFirewallProfile -PolicyStore ActiveStore -ErrorAction SilentlyContinue)
    } catch {
        # Privileged observations are unavailable (rights, policy or module).
        # Never invent a green or red from a failed read.
        $Diagnostics.reason_code = "diagnostics_unavailable"
        return $Diagnostics
    }
    $Diagnostics.effective_policy_checked = $true

    # Legacy DisplayName matches are diagnostic context only, never ownership.
    $Diagnostics.legacy_rule_matches = @(
        $Rules | Where-Object {
            $_.DisplayName -match "Hashi|Remote|Python" -and
            [string]$_.Name -notin @($Names.Tcp, $Names.Mdns)
        } | Select-Object -ExpandProperty DisplayName
    )

    # ── Owned TCP rule ──────────────────────────────────────────────
    $TcpRule = @($Rules | Where-Object { $_.Name -eq $Names.Tcp })
    $TcpIssues = @()
    if ($TcpRule.Count -eq 1) {
        $Rule = $TcpRule[0]
        $PortFilter = Get-NetFirewallPortFilter -AssociatedNetFirewallRule $Rule -ErrorAction SilentlyContinue
        $AddressFilter = Get-NetFirewallAddressFilter -AssociatedNetFirewallRule $Rule -ErrorAction SilentlyContinue
        $Annotation = $null
        $Description = [string]$Rule.Description
        if ($Description) {
            try { $Annotation = $Description | ConvertFrom-Json } catch { }
        }
        if ([string]$Rule.Direction -ne "Inbound") { $TcpIssues += "direction_not_inbound" }
        if ([string]$Rule.Action -ne "Allow") { $TcpIssues += "action_not_allow" }
        if (-not (Test-RemoteGpoTrue $Rule.Enabled)) { $TcpIssues += "rule_disabled" }
        if (@(Convert-RemoteGpoStringList $Rule.Profile | Where-Object { $_ -notin @("Domain", "Private") }).Count -gt 0) {
            $TcpIssues += "profile_includes_public_or_any"
        }
        if ($null -ne $PortFilter) {
            if ([string]$PortFilter.Protocol -ne "TCP") { $TcpIssues += "protocol_not_tcp" }
            # Owned Allow rules must match the exact narrow template; a range
            # or "Any" that merely covers the port is not compliant here.
            if (-not (Test-RemotePortSpecExact -PortSpec ([string]$PortFilter.LocalPort) -EffectivePort $EffectivePort)) {
                $TcpIssues += "port_mismatch"
            }
        } else {
            $TcpIssues += "port_filter_unreadable"
        }
        if ($null -ne $AddressFilter) {
            if ([string]$AddressFilter.RemoteAddress -notin @("LocalSubnet", "LocalSubnet4", "LocalSubnet6")) {
                $TcpIssues += "remote_address_not_local_subnet"
            }
        } else {
            $TcpIssues += "address_filter_unreadable"
        }
        if ($Annotation) {
            if ([string]$Annotation.purpose -ne "hashi-remote-msi-tcp") { $TcpIssues += "ownership_annotation_unexpected" }
        } else {
            $TcpIssues += "ownership_annotation_missing"
        }
        $Diagnostics.tcp_rule = [ordered]@{
            name = $Names.Tcp
            present = $true
            enabled = (Test-RemoteGpoTrue $Rule.Enabled)
            action = [string]$Rule.Action
            profile = @(Convert-RemoteGpoStringList $Rule.Profile)
            local_port = if ($null -ne $PortFilter) { [string]$PortFilter.LocalPort } else { "" }
            remote_address = if ($null -ne $AddressFilter) { [string]$AddressFilter.RemoteAddress } else { "" }
            issues = @($TcpIssues)
        }
    } else {
        $TcpIssues += "rule_missing"
        $Diagnostics.tcp_rule = [ordered]@{
            name = $Names.Tcp
            present = $false
            enabled = $false
            action = ""
            profile = @()
            local_port = ""
            remote_address = ""
            issues = @($TcpIssues)
        }
    }
    $Diagnostics.tcp_rule_issues = @($TcpIssues)

    # ── Owned host-shared mDNS rule ─────────────────────────────────
    $MdnsRule = @($Rules | Where-Object { $_.Name -eq $Names.Mdns })
    $MdnsIssues = @()
    if ($MdnsRule.Count -eq 1) {
        $Rule = $MdnsRule[0]
        $PortFilter = Get-NetFirewallPortFilter -AssociatedNetFirewallRule $Rule -ErrorAction SilentlyContinue
        $AddressFilter = Get-NetFirewallAddressFilter -AssociatedNetFirewallRule $Rule -ErrorAction SilentlyContinue
        $Annotation = $null
        $Description = [string]$Rule.Description
        if ($Description) {
            try { $Annotation = $Description | ConvertFrom-Json } catch { }
        }
        if (-not (Test-RemoteGpoTrue $Rule.Enabled)) { $MdnsIssues += "rule_disabled" }
        if ([string]$Rule.Action -ne "Allow") { $MdnsIssues += "action_not_allow" }
        if ([string]$Rule.Direction -ne "Inbound") { $MdnsIssues += "direction_not_inbound" }
        if (@(Convert-RemoteGpoStringList $Rule.Profile | Where-Object { $_ -notin @("Domain", "Private") }).Count -gt 0) {
            $MdnsIssues += "profile_includes_public_or_any"
        }
        if ($null -ne $PortFilter) {
            if ([string]$PortFilter.Protocol -ne "UDP") { $MdnsIssues += "protocol_not_udp" }
            # Same exact-template rule as the TCP Allow rule: UDP and exactly
            # port 5353. "Any" or a covering range is not compliant.
            if (-not (Test-RemotePortSpecExact -PortSpec ([string]$PortFilter.LocalPort) -EffectivePort 5353)) {
                $MdnsIssues += "port_mismatch"
            }
        } else {
            $MdnsIssues += "port_filter_unreadable"
        }
        if ($null -ne $AddressFilter) {
            if ([string]$AddressFilter.RemoteAddress -notin @("LocalSubnet", "LocalSubnet4", "LocalSubnet6")) {
                $MdnsIssues += "remote_address_not_local_subnet"
            }
        } else {
            $MdnsIssues += "address_filter_unreadable"
        }
        if ($Annotation) {
            if ([string]$Annotation.purpose -ne "hashi-remote-mdns-udp-5353") { $MdnsIssues += "ownership_annotation_unexpected" }
        } else {
            $MdnsIssues += "ownership_annotation_missing"
        }
        $Diagnostics.mdns_rule = [ordered]@{
            name = $Names.Mdns
            present = $true
            enabled = (Test-RemoteGpoTrue $Rule.Enabled)
            action = [string]$Rule.Action
            profile = @(Convert-RemoteGpoStringList $Rule.Profile)
            local_port = if ($null -ne $PortFilter) { [string]$PortFilter.LocalPort } else { "" }
            remote_address = if ($null -ne $AddressFilter) { [string]$AddressFilter.RemoteAddress } else { "" }
            issues = @($MdnsIssues)
        }
    } else {
        $MdnsIssues += "owned_rule_missing"
        $Diagnostics.mdns_rule = [ordered]@{
            name = $Names.Mdns
            present = $false
            enabled = $false
            action = ""
            profile = @()
            local_port = ""
            remote_address = ""
            issues = @($MdnsIssues)
        }
    }
    $Diagnostics.mdns_rule_issues = @($MdnsIssues)

    # ── Effective profile policy (ActiveStore) ─────────────────────
    foreach ($Profile in $Profiles) {
        $AllowLocal = $null
        if ($null -ne $Profile.AllowLocalFirewallRules -and
            [string]$Profile.AllowLocalFirewallRules -ne 'NotConfigured') {
            # An explicitly configured value is authoritative; NotConfigured is
            # deliberately reported as null, never as an invented true/false.
            $AllowLocal = (Test-RemoteGpoTrue $Profile.AllowLocalFirewallRules)
        }
        $Diagnostics.profiles += [ordered]@{
            name = [string]$Profile.Name
            enabled = (Test-RemoteGpoTrue $Profile.Enabled)
            default_inbound_action = [string]$Profile.DefaultInboundAction
            allow_local_firewall_rules = $AllowLocal
        }
        if ((Test-RemoteGpoTrue $Profile.Enabled) -and $AllowLocal -eq $false) {
            $Diagnostics.local_rules_ignored_by_policy = $true
        }
    }

    # ── Explicit Block evidence (only with matching effective policy) ─
    # A per-program Block rule only counts when it targets the actual listener
    # executable; port rules without a program restriction cover every owner.
    foreach ($Rule in @($Rules | Where-Object { [string]$_.Direction -eq "Inbound" -and (Test-RemoteGpoTrue $_.Enabled) -and [string]$_.Action -eq "Block" })) {
        $PortFilter = Get-NetFirewallPortFilter -AssociatedNetFirewallRule $Rule -ErrorAction SilentlyContinue
        if ($null -eq $PortFilter -or -not (Test-RemotePortSpecCovers -PortSpec ([string]$PortFilter.LocalPort) -EffectivePort $EffectivePort)) {
            continue
        }
        $AppFilter = Get-NetFirewallApplicationFilter -AssociatedNetFirewallRule $Rule -ErrorAction SilentlyContinue
        $Program = if ($null -ne $AppFilter) { [string]$AppFilter.Program } else { "" }
        $AppliesToListener = $true
        if ($Program) {
            if ([string]::IsNullOrWhiteSpace($ListenerProcessPath)) {
                # A program-restricted rule with an unknown listener path is
                # reported as unverified context, not as proof.
                $AppliesToListener = $false
            } else {
                try {
                    $AppliesToListener = ([System.IO.Path]::GetFullPath($Program)).Equals(
                        ([System.IO.Path]::GetFullPath($ListenerProcessPath)),
                        [System.StringComparison]::OrdinalIgnoreCase)
                } catch {
                    $AppliesToListener = $false
                }
            }
        }
        if (-not $AppliesToListener) { continue }
        $Diagnostics.explicit_block_evidence += [ordered]@{
            name = [string]$Rule.Name
            display_name = [string]$Rule.DisplayName
            local_port = [string]$PortFilter.LocalPort
            program = $Program
            profile = @(Convert-RemoteGpoStringList $Rule.Profile)
        }
    }

    # ── Derive state + reason code ─────────────────────────────────
    if ($Diagnostics.explicit_block_evidence.Count -gt 0) {
        $Diagnostics.state = "degraded"
        $Diagnostics.reason_code = "firewall_explicitly_blocked"
    } elseif ($Diagnostics.local_rules_ignored_by_policy) {
        $Diagnostics.state = "degraded"
        $Diagnostics.reason_code = "firewall_local_rules_ignored_by_policy"
    } elseif ($Diagnostics.tcp_rule.present -and $Diagnostics.tcp_rule.enabled -and $TcpIssues.Count -eq 0) {
        $Diagnostics.state = "ok"
        $Diagnostics.reason_code = ""
    } elseif ($Diagnostics.tcp_rule.present -and -not $Diagnostics.tcp_rule.enabled) {
        $Diagnostics.state = "degraded"
        $Diagnostics.reason_code = "firewall_rule_disabled"
    } elseif ($Diagnostics.tcp_rule.present) {
        $Diagnostics.state = "degraded"
        if ($TcpIssues -contains "port_mismatch") { $Diagnostics.reason_code = "firewall_rule_port_mismatch" }
        elseif ($TcpIssues -contains "remote_address_not_local_subnet") { $Diagnostics.reason_code = "firewall_remote_address_mismatch" }
        elseif ($TcpIssues -contains "profile_includes_public_or_any") { $Diagnostics.reason_code = "firewall_profile_mismatch" }
        else { $Diagnostics.reason_code = "firewall_rule_missing" }
    } else {
        $Diagnostics.state = "degraded"
        $Diagnostics.reason_code = "firewall_rule_missing"
    }
    return $Diagnostics
}

function Get-RemoteListenerDiagnostics {
    param([int]$EffectivePort, [bool]$HealthReachable = $false)

    $Diagnostics = [ordered]@{
        state = "unknown"
        reason_code = "diagnostics_unavailable"
        listeners = @()
    }
    if (-not (Get-Command Get-NetTCPConnection -ErrorAction SilentlyContinue)) {
        if ($HealthReachable) {
            # The local probe already proves a working listener.
            $Diagnostics.state = "ok"
            $Diagnostics.reason_code = ""
        } else {
            $Diagnostics.state = "degraded"
            $Diagnostics.reason_code = "remote_process_down"
        }
        return $Diagnostics
    }
    $Rows = @(Get-NetTCPConnection -LocalPort $EffectivePort -State Listen -ErrorAction SilentlyContinue)
    if ($Rows.Count -eq 0) {
        if ($HealthReachable) {
            $Diagnostics.state = "ok"
            $Diagnostics.reason_code = ""
        } else {
            $Diagnostics.state = "degraded"
            $Diagnostics.reason_code = "remote_process_down"
        }
        return $Diagnostics
    }
    $Owned = $false
    $Wildcard = $false
    $Routable = $false
    foreach ($Row in $Rows) {
        $Address = [string]$Row.LocalAddress
        $ListenerPid = [int]$Row.OwningProcess
        $ProcessName = ""
        $ExecutablePath = ""
        $OwnedProcess = $false
        if ($ListenerPid -gt 0) {
            try {
                $Proc = Get-Process -Id $ListenerPid -ErrorAction SilentlyContinue
                if ($Proc) { $ProcessName = [string]$Proc.ProcessName }
            } catch { }
            try {
                $Cim = Get-CimInstance Win32_Process -Filter "ProcessId = $ListenerPid" -ErrorAction SilentlyContinue
                if ($Cim) {
                    $OwnedProcess = Test-OwnedRemoteCommandLine ([string]$Cim.CommandLine)
                    $ExecutablePath = [string]$Cim.ExecutablePath
                }
            } catch { }
        }
        if ($OwnedProcess) { $Owned = $true }
        if ($Address -in @("0.0.0.0", "::", "[::]")) { $Wildcard = $true }
        if ($Address -notin @("127.0.0.1", "::1", "0.0.0.0", "::", "[::]")) { $Routable = $true }
        $Diagnostics.listeners += [ordered]@{
            address = $Address
            port = [int]$Row.LocalPort
            pid = $ListenerPid
            process_name = $ProcessName
            executable_path = $ExecutablePath
            owned = $OwnedProcess
        }
    }
    if (-not $Owned) {
        $Diagnostics.state = "degraded"
        $Diagnostics.reason_code = "port_owned_by_other_process"
    } elseif ($Wildcard -or $Routable) {
        $Diagnostics.state = "ok"
        $Diagnostics.reason_code = ""
    } else {
        # Only loopback listeners: LAN hosts cannot reach this Remote.
        $Diagnostics.state = "degraded"
        $Diagnostics.reason_code = "loopback_only_bind"
    }
    return $Diagnostics
}

function Get-RemoteMdnsDiagnostics {
    param($Health, $FirewallDiagnostics)

    $Diagnostics = [ordered]@{
        advertising = "unknown"
        browsing = "unknown"
        external_visibility = "unknown"
        rule_present = [bool]($null -ne $FirewallDiagnostics -and $FirewallDiagnostics.mdns_rule.present -and $FirewallDiagnostics.mdns_rule.enabled)
        rule_issues = @()
        last_errors = @()
    }
    if ($null -ne $FirewallDiagnostics) { $Diagnostics.rule_issues = @($FirewallDiagnostics.mdns_rule_issues) }
    if ($null -ne $Health -and $null -ne $Health.discovery) {
        $Backends = @($Health.discovery.backends)
        if ($Backends.Count -gt 0) {
            $AdvOk = @($Backends | Where-Object { $_.advertising -ne $true }).Count -eq 0
            $BrwOk = @($Backends | Where-Object { $_.browsing -ne $true }).Count -eq 0
            $Diagnostics.advertising = if ($AdvOk) { "ok" } else { "degraded" }
            $Diagnostics.browsing = if ($BrwOk) { "ok" } else { "degraded" }
        }
        $Diagnostics.last_errors = @($Backends | Where-Object { $_.last_error } | ForEach-Object { "$($_.backend): $($_.last_error)" })
    }
    return $Diagnostics
}

function Push-RemoteLanReadinessReport {
    param($Snapshot, [int]$EffectivePort, [bool]$UseTls)

    $Schemes = if ($UseTls) { @("https") } else { @("http") }
    $Body = $Snapshot | ConvertTo-Json -Depth 10 -Compress
    $LastError = $null
    foreach ($Scheme in $Schemes) {
        $Uri = "${Scheme}://127.0.0.1:${EffectivePort}/lan-readiness/report"
        $PreviousCallback = [System.Net.ServicePointManager]::ServerCertificateValidationCallback
        try {
            if ($Scheme -eq "https") {
                # Scoped to this short-lived controller process and loopback only.
                [System.Net.ServicePointManager]::ServerCertificateValidationCallback = { $true }
            }
            $Response = Invoke-RestMethod -Method Post -Uri $Uri -ContentType "application/json" -Body $Body -TimeoutSec 4
            return [PSCustomObject]@{ pushed = $true; uri = $Uri; ok = [bool]$Response.ok; error = $null }
        } catch {
            $LastError = $_.Exception.Message
        } finally {
            [System.Net.ServicePointManager]::ServerCertificateValidationCallback = $PreviousCallback
        }
    }
    return [PSCustomObject]@{ pushed = $false; uri = $null; ok = $false; error = $LastError }
}

function Get-RemoteFirewallRuleAnnotation {
    param(
        [ValidateSet("tcp", "mdns")] [string]$Purpose,
        [string]$InstanceId,
        [int]$EffectivePort,
        [string[]]$ExtraInstances = @()
    )
    if ($Purpose -eq "tcp") {
        return ([ordered]@{
            purpose = "hashi-remote-msi-tcp"
            instance_id = $InstanceId
            port = $EffectivePort
            hashi_root = $HashiRoot
        } | ConvertTo-Json -Compress)
    }
    return ([ordered]@{
        purpose = "hashi-remote-mdns-udp-5353"
        instances = @(@($ExtraInstances) + @($InstanceId) | Where-Object { $_ } | Select-Object -Unique)
    } | ConvertTo-Json -Compress)
}

function Repair-HashiRemoteFirewallRules {
    param([int]$EffectivePort, [string]$InstanceId)

    $Names = Get-RemoteFirewallRuleNames -InstanceId $InstanceId -EffectivePort $EffectivePort
    $Changes = @()
    $ErrorList = @()
    try {
        $Tcp = Get-NetFirewallRule -PolicyStore ActiveStore -Name $Names.Tcp -ErrorAction SilentlyContinue
        $TcpAnnotation = Get-RemoteFirewallRuleAnnotation -Purpose "tcp" -InstanceId $InstanceId -EffectivePort $EffectivePort
        if ($Tcp) {
            Set-NetFirewallRule -Name $Names.Tcp -Direction Inbound -Action Allow -Enabled $true `
                -Profile @("Domain", "Private") -Description $TcpAnnotation -ErrorAction Stop | Out-Null
            $TcpRule = Get-NetFirewallRule -PolicyStore ActiveStore -Name $Names.Tcp -ErrorAction Stop
            Set-NetFirewallPortFilter -AssociatedNetFirewallRule $TcpRule -Protocol TCP -LocalPort $EffectivePort -ErrorAction Stop
            Set-NetFirewallAddressFilter -AssociatedNetFirewallRule $TcpRule -RemoteAddress LocalSubnet -ErrorAction Stop
            $Changes += "tcp_rule_updated"
        } else {
            New-NetFirewallRule -Name $Names.Tcp -DisplayName "HASHI Remote MSI TCP $EffectivePort" `
                -Description $TcpAnnotation -Direction Inbound -Action Allow -Protocol TCP `
                -LocalPort $EffectivePort -RemoteAddress LocalSubnet -Profile @("Domain", "Private") `
                -Enabled $true -ErrorAction Stop | Out-Null
            $Changes += "tcp_rule_created"
        }
        $Mdns = Get-NetFirewallRule -PolicyStore ActiveStore -Name $Names.Mdns -ErrorAction SilentlyContinue
        if (-not $Mdns) {
            $MdnsAnnotation = Get-RemoteFirewallRuleAnnotation -Purpose "mdns" -InstanceId $InstanceId -EffectivePort $EffectivePort
            New-NetFirewallRule -Name $Names.Mdns -DisplayName "HASHI Remote mDNS UDP 5353" `
                -Description $MdnsAnnotation -Direction Inbound -Action Allow -Protocol UDP `
                -LocalPort 5353 -RemoteAddress LocalSubnet -Profile @("Domain", "Private") `
                -Enabled $true -ErrorAction Stop | Out-Null
            $Changes += "mdns_rule_created"
        } else {
            # Host-shared rule: register this instance in its refcount annotation.
            $Instances = @($InstanceId)
            $Description = [string]$Mdns.Description
            if ($Description) {
                try {
                    $Parsed = $Description | ConvertFrom-Json
                    if ($Parsed.instances) { $Instances = @($Parsed.instances) + @($InstanceId) }
                } catch { }
            }
            $MdnsAnnotation = Get-RemoteFirewallRuleAnnotation -Purpose "mdns" -InstanceId $InstanceId `
                -EffectivePort $EffectivePort -ExtraInstances @($Instances)
            Set-NetFirewallRule -Name $Names.Mdns -Direction Inbound -Action Allow -Enabled $true `
                -Profile @("Domain", "Private") -Description $MdnsAnnotation -ErrorAction Stop | Out-Null
            $MdnsRule = Get-NetFirewallRule -PolicyStore ActiveStore -Name $Names.Mdns -ErrorAction Stop
            Set-NetFirewallPortFilter -AssociatedNetFirewallRule $MdnsRule -Protocol UDP -LocalPort 5353 -ErrorAction Stop
            Set-NetFirewallAddressFilter -AssociatedNetFirewallRule $MdnsRule -RemoteAddress LocalSubnet -ErrorAction Stop
            $Changes += "mdns_rule_registered"
        }
        $repaired = $true
    } catch {
        $repaired = $false
        $ErrorList += $_.Exception.Message
    }
    return [PSCustomObject]@{
        repaired = $repaired
        elevated = $true
        uac_declined = $false
        changes = @($Changes)
        error = ($ErrorList -join "; ")
        health_accepted = $false
    }
}

function Invoke-RemoteFirewallRepair {
    param([int]$EffectivePort, [string]$InstanceId)

    $IsElevated = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)
    # Contract-test seam only: forces the single bounded UAC relaunch path so
    # automation never triggers a real elevation prompt.
    if ($env:HASHI_REMOTE_REPAIR_TEST_NON_ELEVATED -eq "1") { $IsElevated = $false }
    if ($IsElevated) {
        return Repair-HashiRemoteFirewallRules -EffectivePort $EffectivePort -InstanceId $InstanceId
    }

    # One bounded UAC elevation. The elevated child performs the repair and
    # emits its JSON diagnostics to the redirected file; refusing UAC is a
    # diagnostic outcome, never a loop or a retry storm.
    $ResultPath = Join-Path $env:TEMP ("hashi-remote-repair-{0}-{1}.json" -f $InstanceId, $PID)
    Remove-Item -LiteralPath $ResultPath -Force -ErrorAction SilentlyContinue
    $ChildArgs = @(
        "-NoProfile", "-ExecutionPolicy", "Bypass",
        "-File", "`"$PSCommandPath`"",
        "doctor", "-Repair", "-OutputFormat", "Json",
        "-HashiRoot", "`"$HashiRoot`"",
        "-CodeRoot", "`"$CodeRoot`"",
        "-Python", "`"$Python`"",
        "-Port", "$EffectivePort"
    )
    try {
        $Child = Start-Process -FilePath "powershell.exe" -ArgumentList $ChildArgs `
            -Verb RunAs -Wait -PassThru -RedirectStandardOutput $ResultPath
        $ChildExit = [int]$Child.ExitCode
        $ChildOutput = ""
        if (Test-Path -LiteralPath $ResultPath) {
            $ChildOutput = Get-Content -LiteralPath $ResultPath -Raw
        }
        $ChildRepair = $null
        if ($ChildOutput) {
            try {
                $Parsed = $ChildOutput | ConvertFrom-Json
                if ($Parsed -and $Parsed.repair) { $ChildRepair = $Parsed.repair }
            } catch { }
        }
        if ($null -ne $ChildRepair) {
            return [PSCustomObject]@{
                repaired = [bool]$ChildRepair.repaired
                elevated = $true
                uac_declined = $false
                changes = @([string[]]$ChildRepair.changes)
                error = [string]$ChildRepair.error
                health_accepted = [bool]$ChildRepair.health_accepted
                child_exit = $ChildExit
            }
        }
        return [PSCustomObject]@{
            repaired = ($ChildExit -eq 0)
            elevated = $true
            uac_declined = $false
            changes = @()
            error = if ($ChildExit -ne 0) { "elevated repair exited with code $ChildExit" } else { "" }
            health_accepted = $false
            child_exit = $ChildExit
        }
    } catch {
        $Message = $_.Exception.Message
        $Declined = $Message -match "(?i)canceled|cancelled|denied|refused|declined"
        return [PSCustomObject]@{
            repaired = $false
            elevated = $false
            uac_declined = $Declined
            changes = @()
            error = $Message
            health_accepted = $false
            child_exit = $null
        }
    }
}

function Show-RemoteDoctor {
    param([bool]$MachineJson = $false, $RepairOutcome = $null)

    $EffectivePort = Get-RemotePort
    $UseTls = Get-RemoteUseTls
    $InstanceId = [string]$SupervisorIdentity.instance_id

    $WslStatus = $null
    $WslError = $null
    $WslCommand = Get-Command wsl.exe -ErrorAction SilentlyContinue
    $WslAvailable = $null -ne $WslCommand
    if ($WslAvailable) {
        try {
            $WslStatus = (& $WslCommand.Source --status 2>$null) -join "`n"
        } catch {
            $WslAvailable = $false
            $WslError = $_.Exception.Message
        }
    }

    $Probe = Get-RemoteHealthProbe -EffectivePort $EffectivePort
    $Health = $Probe.Health
    $HealthError = $Probe.Error
    $Acceptance = Get-RemoteHealthAcceptance -Probe $Probe
    $Discovery = if ($null -ne $Health) { $Health.discovery } else { $null }
    $Backends = @()
    if ($null -ne $Discovery) { $Backends = @($Discovery.backends) }
    $Advertising = if ($Backends.Count -gt 0) {
        @($Backends | Where-Object { $_.advertising -ne $true }).Count -eq 0
    } else { $false }
    $Browsing = if ($Backends.Count -gt 0) {
        @($Backends | Where-Object { $_.browsing -ne $true }).Count -eq 0
    } else { $false }

    $Listener = Get-RemoteListenerDiagnostics -EffectivePort $EffectivePort -HealthReachable ($null -ne $Health)
    $ListenerPath = if (@($Listener.listeners).Count -gt 0) { [string]$Listener.listeners[0].executable_path } else { "" }
    $Firewall = Get-RemoteFirewallDiagnostics -EffectivePort $EffectivePort -InstanceId $InstanceId -ListenerProcessPath $ListenerPath
    $Mdns = Get-RemoteMdnsDiagnostics -Health $Health -FirewallDiagnostics $Firewall

    # ── v1 lan_readiness snapshot ───────────────────────────────────
    $ObservedAt = [int][DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
    $ReasonCodes = @()
    if ($Listener.reason_code) { $ReasonCodes += $Listener.reason_code }
    if ($Firewall.reason_code) { $ReasonCodes += $Firewall.reason_code }
    if ($Mdns.advertising -eq "degraded") { $ReasonCodes += "mdns_advertise_failed" }
    if ($Mdns.browsing -eq "degraded") { $ReasonCodes += "mdns_browse_failed" }
    if ($Mdns.rule_issues.Count -gt 0) { $ReasonCodes += "mdns_rule_missing" }
    if ($null -eq $Health) { $ReasonCodes += "remote_process_down" }
    $ReasonCodes += "mdns_visibility_unverified"
    $ReasonCodes += "lan_reachability_unverified"

    $Snapshot = [ordered]@{
        schema_version = 1
        instance_id = $InstanceId
        state = "unknown"
        observed_at = $ObservedAt
        source = "windows_doctor"
        process = [ordered]@{
            state = if ($null -ne $Health) { "ok" } else { "degraded" }
        }
        listener = [ordered]@{
            state = $Listener.state
            address = if (@($Listener.listeners).Count -gt 0) { [string]$Listener.listeners[0].address } else { $null }
            port = $EffectivePort
            pid = if (@($Listener.listeners).Count -gt 0) { [int]$Listener.listeners[0].pid } else { $null }
        }
        firewall = [ordered]@{
            state = $Firewall.state
            reason_code = $Firewall.reason_code
            effective_policy_checked = [bool]$Firewall.effective_policy_checked
        }
        mdns = [ordered]@{
            advertising = $Mdns.advertising
            browsing = $Mdns.browsing
            external_visibility = "unknown"
        }
        external_reachability = [ordered]@{
            state = "unknown"
            reason_code = "lan_reachability_unverified"
        }
        reason_codes = @($ReasonCodes | Select-Object -Unique)
    }
    if ($Snapshot.listener.state -eq "ok" -and $null -ne $Health -and $Snapshot.firewall.state -in @("ok", "unknown")) {
        $Snapshot.state = "ok"
    } else {
        $Snapshot.state = "degraded"
    }

    $Push = Push-RemoteLanReadinessReport -Snapshot $Snapshot -EffectivePort $EffectivePort -UseTls $UseTls

    if ($null -ne $RepairOutcome) {
        $RepairOutcome.health_accepted = [bool]$Acceptance.Accepted
    }

    $LegacyFirewallRules = @($Firewall.legacy_rule_matches)

    $Human = [PSCustomObject]@{
        HashiRoot = $HashiRoot
        RemotePort = $EffectivePort
        Listening = @($Listener.listeners).Count -gt 0
        ListenerAddresses = @($Listener.listeners | ForEach-Object { $_.address })
        ListenerPids = @($Listener.listeners | ForEach-Object { $_.pid })
        OwnedTcpRule = $Firewall.tcp_rule.name
        OwnedTcpRulePresent = [bool]$Firewall.tcp_rule.present
        OwnedTcpRuleIssues = @($Firewall.tcp_rule_issues)
        FirewallState = $Firewall.state
        FirewallReasonCode = $Firewall.reason_code
        EffectivePolicyChecked = [bool]$Firewall.effective_policy_checked
        ExplicitBlockEvidence = @($Firewall.explicit_block_evidence | ForEach-Object { $_.display_name })
        LocalRulesIgnoredByPolicy = [bool]$Firewall.local_rules_ignored_by_policy
        OwnedMdnsRule = $Firewall.mdns_rule.name
        OwnedMdnsRulePresent = [bool]$Firewall.mdns_rule.present
        OwnedMdnsRuleIssues = @($Firewall.mdns_rule_issues)
        FirewallRuleCount = @($LegacyFirewallRules).Count
        FirewallRules = @($LegacyFirewallRules)
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
        LanReadinessState = $Snapshot.state
        LanReadinessReasonCodes = @($Snapshot.reason_codes)
        SnapshotPushed = [bool]$Push.pushed
        HealthError = $HealthError
        Command = $CommandPreview
    }

    if ($MachineJson) {
        $Machine = [ordered]@{
            schema_version = 1
            timestamp = (Get-Date).ToString("o")
            instance_id = $InstanceId
            remote_port = $EffectivePort
            hashi_root = $HashiRoot
            code_root = $CodeRoot
            firewall = $Firewall
            listener = $Listener
            mdns = $Mdns
            local_health = [ordered]@{
                reachable = $null -ne $Health
                uri = $Probe.Uri
                state = if ($null -ne $Health) { $Health.status } else { "unreachable" }
                mode = $Acceptance.Mode
                accepted = [bool]$Acceptance.Accepted
                reason = $Acceptance.Reason
                error = $HealthError
            }
            wsl = [ordered]@{ available = $WslAvailable; status = $WslStatus; error = $WslError }
            lan_readiness = $Snapshot
            push = $Push
            repair = if ($null -ne $RepairOutcome) { $RepairOutcome } else { $null }
            command = $CommandPreview
        }
        [Console]::Out.WriteLine(($Machine | ConvertTo-Json -Depth 10))
        if (-not $Push.pushed -and $Push.error) {
            [Console]::Error.WriteLine("lan_readiness report was not pushed to the Remote: $($Push.error)")
        } elseif ($Push.pushed -and -not $Push.ok) {
            [Console]::Error.WriteLine("lan_readiness report was rejected by the Remote")
        }
        if (-not $Acceptance.Accepted) {
            exit 2
        }
        return
    }

    $Human | Format-List
    if (-not $Acceptance.Accepted) {
        exit 2
    }
}

# A same-named task can belong to a different home after an installation move.
# Refuse to overwrite or control it until its ownership is reconciled.
if ($Action -in @("register", "install", "enable", "disable", "unregister", "uninstall", "start", "stop", "restart")) {
    $ExistingTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($null -ne $ExistingTask) {
        $ExistingRoot = [string](@($ExistingTask.Actions)[0].WorkingDirectory)
        if ([string]::IsNullOrWhiteSpace($ExistingRoot) -or -not
            ([System.IO.Path]::GetFullPath($ExistingRoot)).TrimEnd('\').Equals(
                $HashiRoot.TrimEnd('\'), [StringComparison]::OrdinalIgnoreCase
            )) {
            throw "Refusing Remote task '$TaskName': its working directory does not match this instance home."
        }
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
        if (Test-Path -LiteralPath $RestartController -PathType Leaf) {
            & $RestartController unregister `
                -HashiRoot $HashiRoot `
                -CodeRoot $CodeRoot `
                -Python $Python `
                -InstanceId $SupervisorIdentity.instance_id
        }
        Write-Host "Unregistered Remote supervisor task '$TaskName'"
    }
    "start" {
        $ResolvedPrincipal = Resolve-RemoteTaskPrincipal
        Protect-RemoteCredentialAccess -Principal $ResolvedPrincipal
        & $RestartController ensure -HashiRoot $HashiRoot -CodeRoot $CodeRoot `
            -Python $Python -TaskUserId $ResolvedPrincipal.UserId -InstanceId $SupervisorIdentity.instance_id
        Start-ScheduledTask -TaskName $TaskName
        Wait-RemoteHealthAcceptance -EffectivePort (Get-RemotePort) | Out-Null
    }
    "stop" {
        Stop-RemoteSupervisor
    }
    "restart" {
        $ResolvedPrincipal = Resolve-RemoteTaskPrincipal
        Protect-RemoteCredentialAccess -Principal $ResolvedPrincipal
        & $RestartController ensure -HashiRoot $HashiRoot -CodeRoot $CodeRoot `
            -Python $Python -TaskUserId $ResolvedPrincipal.UserId -InstanceId $SupervisorIdentity.instance_id
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
        $RepairOutcome = $null
        if ($Repair) {
            $RepairOutcome = Invoke-RemoteFirewallRepair `
                -EffectivePort (Get-RemotePort) `
                -InstanceId ([string]$SupervisorIdentity.instance_id)
        }
        $MachineJson = ($Json -or $OutputFormat -eq "Json")
        Show-RemoteDoctor -MachineJson $MachineJson -RepairOutcome $RepairOutcome
        if ($Repair -and $null -ne $RepairOutcome) {
            if ($RepairOutcome.repaired -and $RepairOutcome.health_accepted) {
                exit 0
            }
            if ($RepairOutcome.repaired) {
                exit 2
            }
            exit 4
        }
    }
}
