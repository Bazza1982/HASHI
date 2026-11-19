from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
pytestmark = [
    pytest.mark.platform,
    pytest.mark.skipif(sys.platform != "win32", reason="Windows doctor contract"),
]

CTL = ROOT / "bin" / "hashi_remote_ctl.ps1"
HEALTH_PAYLOAD = {
    "status": "ready",
    "instance": {"instance_id": "DOCTOR-CONTRACT"},
    "discovery": {
        "state": "ready_empty",
        "readiness": "ready",
        "backends": [{"backend": "lan", "advertising": True, "browsing": True, "last_error": None}],
        "peer_count": 0,
        "trusted_peer_count": 0,
        "trust_state": "no_peers",
        "static_seed_fallback_active": False,
    },
}

# Platform cmdlets shadowed by these stubs; state defaults are scenario-safe.
STUB_TEMPLATE = r"""
# Load the real NetSecurity module so the Cmdletization-generated 5.1 enums
# (GpoBoolean/GpoProfile/GpoAction) exist for realistic ActiveStore fixtures.
# Stub functions are defined afterwards and shadow the real cmdlets.
Import-Module NetSecurity -ErrorAction SilentlyContinue
$global:FirewallRules = @()
$global:PortFilters = @{}
$global:AddressFilters = @{}
$global:ApplicationFilters = @{}
$global:Profiles = @(
    [pscustomobject]@{ Name='Domain'; Enabled=$true; DefaultInboundAction='Allow'; AllowLocalFirewallRules=$true },
    [pscustomobject]@{ Name='Private'; Enabled=$true; DefaultInboundAction='Allow'; AllowLocalFirewallRules=$true },
    [pscustomobject]@{ Name='Public'; Enabled=$true; DefaultInboundAction='Allow'; AllowLocalFirewallRules=$true }
)
$global:Listeners = @()
$global:OwnedPid = 4242
$global:OwnedRoot = ''
$global:HealthReachable = $true
$global:HealthPayload = $null
$global:PushedBody = $null
$global:PushResult = @{ ok = $true }
$global:UacDeclined = $false
$global:ChildExitCode = 0
$global:ChildOutput = '{}'
$global:NewRuleCalls = @()

function Get-Command {
    param([string]$Name, [string]$ErrorAction = '')
    if ($Name -eq 'wsl.exe') { return $null }
    return Microsoft.PowerShell.Core\Get-Command @PSBoundParameters
}
function Get-NetFirewallRule {
    param([string]$PolicyStore, [string]$Name, [string]$Direction, [bool]$Enabled = $false, [string]$ErrorAction = '')
    $rules = @($global:FirewallRules)
    if ($Name) { $rules = @($rules | Where-Object { $_.Name -eq $Name }); return @($rules)[0] }
    if ($Direction) { $rules = @($rules | Where-Object { $_.Direction -eq $Direction }) }
    if ($Enabled) { $rules = @($rules | Where-Object { $_.Enabled }) }
    return $rules
}
function Get-NetFirewallPortFilter {
    param($AssociatedNetFirewallRule, [string]$ErrorAction = '')
    if ($null -ne $AssociatedNetFirewallRule -and $global:PortFilters.ContainsKey([string]$AssociatedNetFirewallRule.Name)) {
        return $global:PortFilters[[string]$AssociatedNetFirewallRule.Name]
    }
    return $null
}
function Get-NetFirewallAddressFilter {
    param($AssociatedNetFirewallRule, [string]$ErrorAction = '')
    if ($null -ne $AssociatedNetFirewallRule -and $global:AddressFilters.ContainsKey([string]$AssociatedNetFirewallRule.Name)) {
        return $global:AddressFilters[[string]$AssociatedNetFirewallRule.Name]
    }
    return $null
}
function Get-NetFirewallApplicationFilter {
    param($AssociatedNetFirewallRule, [string]$ErrorAction = '')
    if ($null -ne $AssociatedNetFirewallRule -and $global:ApplicationFilters.ContainsKey([string]$AssociatedNetFirewallRule.Name)) {
        return $global:ApplicationFilters[[string]$AssociatedNetFirewallRule.Name]
    }
    return $null
}
function Get-NetFirewallProfile {
    param([string]$PolicyStore, [string]$ErrorAction = '')
    return @($global:Profiles)
}
function Get-NetTCPConnection {
    param([int]$LocalPort, [string]$State, [string]$ErrorAction = '')
    return @($global:Listeners)
}
function Get-Process {
    param([int]$Id, [string]$ErrorAction = '')
    if ($Id -eq $global:OwnedPid) { return [pscustomobject]@{ ProcessName = 'python.exe' } }
    return [pscustomobject]@{ ProcessName = 'other.exe' }
}
function Get-CimInstance {
    param([string]$ClassName, [string]$Filter, [string]$ErrorAction = '')
    if ($ClassName -eq 'Win32_Process') {
        if ($Filter -match "ProcessId = $global:OwnedPid") {
            return [pscustomobject]@{ CommandLine = "python.exe -B -m remote --hashi-root `"$global:OwnedRoot`" --port 8767";
                ExecutablePath = "C:\Users\thene\projects\HASHI3\.venv\Scripts\python.exe" }
        }
        return [pscustomobject]@{ CommandLine = 'C:\Windows\System32\svchost.exe -k netsvcs' }
    }
    return $null
}
function Invoke-RestMethod {
    param(
        [string]$Method = 'Get',
        [string]$Uri,
        [string]$ContentType = '',
        [string]$Body = '',
        [int]$TimeoutSec = 4,
        [string]$ErrorAction = ''
    )
    if ($Uri -match '/lan-readiness/report') {
        $global:PushedBody = $Body
        return $global:PushResult
    }
    if ($global:HealthReachable) { return $global:HealthPayload }
    throw 'Unable to connect to the remote server'
}
function New-NetFirewallRule {
    param([string]$Name, [string]$DisplayName, [string]$Description, [string]$Direction, [string]$Action,
          [string]$Protocol, $LocalPort, [string]$RemoteAddress, [string[]]$Profile, [bool]$Enabled, [string]$ErrorAction = '')
    $global:NewRuleCalls += [pscustomobject]@{ Name=$Name; Description=$Description; Protocol=$Protocol;
        LocalPort=[string]$LocalPort; RemoteAddress=$RemoteAddress; Profile=@($Profile); Enabled=$Enabled }
    $rule = [pscustomobject]@{ Name=$Name; DisplayName=$DisplayName; Description=$Description; Direction=$Direction;
        Action=$Action; Enabled=$Enabled; Profile=@($Profile) }
    $global:FirewallRules = @($global:FirewallRules) + $rule
    return $rule
}
function Set-NetFirewallRule {
    param([string]$Name, [string]$Direction, [string]$Action, [bool]$Enabled, [string[]]$Profile,
          [string]$Description, [string]$ErrorAction = '')
    $global:FirewallRules = @($global:FirewallRules | ForEach-Object {
        if ($_.Name -eq $Name) {
            [pscustomobject]@{ Name=$_.Name; DisplayName=$_.DisplayName; Description=$Description; Direction=$Direction;
                Action=$Action; Enabled=$Enabled; Profile=@($Profile) }
        } else { $_ }
    })
}
function Set-NetFirewallPortFilter {
    param($AssociatedNetFirewallRule, [string]$Protocol, $LocalPort, [string]$ErrorAction = '')
    $global:PortFilters[[string]$AssociatedNetFirewallRule.Name] = [pscustomobject]@{ Protocol=$Protocol; LocalPort=[string]$LocalPort }
}
function Set-NetFirewallAddressFilter {
    param($AssociatedNetFirewallRule, [string]$RemoteAddress, [string]$ErrorAction = '')
    $global:AddressFilters[[string]$AssociatedNetFirewallRule.Name] = [pscustomobject]@{ RemoteAddress=$RemoteAddress }
}
function Start-Process {
    param([string]$FilePath, [string[]]$ArgumentList, [switch]$Verb, [switch]$Wait, [switch]$PassThru,
          [string]$RedirectStandardOutput, [string]$ErrorAction = '')
    if ($global:UacDeclined) { throw 'The operation was canceled by the user' }
    if ($RedirectStandardOutput) {
        Set-Content -LiteralPath $RedirectStandardOutput -Value $global:ChildOutput -Encoding UTF8
    }
    return [pscustomobject]@{ ExitCode = $global:ChildExitCode }
}
"""


def _ps_string(value):
    return "'" + str(value).replace("'", "''") + "'"


def _prepare_root(tmp_path: Path) -> Path:
    root = tmp_path / "hashi doctor contract"
    (root / "remote").mkdir(parents=True)
    (root / "agents.json").write_text(
        json.dumps({"global": {"instance_id": "DOCTOR-CONTRACT", "display_name": "Doctor Contract"}}),
        encoding="utf-8",
    )
    (root / "remote" / "config.yaml").write_text("port: 8767\nuse_tls: false\n", encoding="utf-8")
    shutil.copyfile(ROOT / "remote" / "supervisor_identity.py", root / "remote" / "supervisor_identity.py")
    return root


def _run_doctor(tmp_path: Path, scenario: str, extra_args: str, env_extra: dict | None = None) -> tuple[subprocess.CompletedProcess, dict]:
    """Run the real ctl script via -File with a scenario stub file, preserving exit codes."""
    root = _prepare_root(tmp_path)
    stub = tmp_path / "stubs.ps1"
    payload = json.dumps(HEALTH_PAYLOAD)
    stub.write_text(
        STUB_TEMPLATE
        + f"\n$global:HealthPayload = ({_ps_string(payload)} | ConvertFrom-Json)\n"
        + f"$global:OwnedRoot = {_ps_string(root)}\n"
        + "$global:Listeners = @([pscustomobject]@{ LocalAddress='0.0.0.0'; LocalPort=8767; OwningProcess=$global:OwnedPid })\n"
        + scenario + "\n",
        encoding="utf-8",
    )
    args = [
        "powershell.exe",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(CTL),
        "doctor",
        "-HashiRoot", str(root),
        "-CodeRoot", str(root),
        "-Python", sys.executable,
        "-DiagnosticsStubFile", str(stub),
    ]
    args.extend([part for part in extra_args.split() if part])
    env = os.environ.copy()
    env.update(env_extra or {})
    result = subprocess.run(args, capture_output=True, text=True, timeout=120, env=env)
    stdout = result.stdout.strip()
    parsed = None
    if stdout:
        try:
            parsed = json.loads(stdout)
        except json.JSONDecodeError:
            parsed = None
    return result, parsed


# ── Read-only diagnostics ─────────────────────────────────────────────

def test_doctor_json_emits_one_document_with_missing_rule(tmp_path):
    result, parsed = _run_doctor(tmp_path, "", "-Json")
    assert result.returncode == 0, result.stderr
    assert parsed is not None, result.stdout
    assert parsed["schema_version"] == 1
    assert parsed["instance_id"] == "DOCTOR-CONTRACT"
    assert parsed["firewall"]["state"] == "degraded"
    assert parsed["firewall"]["reason_code"] == "firewall_rule_missing"
    assert parsed["firewall"]["effective_policy_checked"] is True
    assert parsed["listener"]["state"] == "ok"
    assert parsed["lan_readiness"]["schema_version"] == 1
    assert "firewall_rule_missing" in parsed["lan_readiness"]["reason_codes"]
    assert "lan_reachability_unverified" in parsed["lan_readiness"]["reason_codes"]
    assert parsed["lan_readiness"]["external_reachability"]["state"] == "unknown"
    assert parsed["push"]["pushed"] is True


def test_doctor_plain_mode_still_prints_format_list(tmp_path):
    result, parsed = _run_doctor(tmp_path, "", "")
    assert result.returncode == 0, result.stderr
    assert "RemotePort" in result.stdout
    assert "HashiRoot" in result.stdout


def test_doctor_explicit_block_is_detected(tmp_path):
    scenario = (
        '$global:FirewallRules = @([pscustomobject]@{ Name="evil-block"; DisplayName="evil-block"; '
        'Direction="Inbound"; Action="Block"; Enabled=$true; Profile=@("Any") }); '
        '$global:PortFilters["evil-block"] = [pscustomobject]@{ Protocol="TCP"; LocalPort="8767" }'
    )
    result, parsed = _run_doctor(tmp_path, scenario, "-Json")
    assert parsed["firewall"]["state"] == "degraded"
    assert parsed["firewall"]["reason_code"] == "firewall_explicitly_blocked"
    assert len(parsed["firewall"]["explicit_block_evidence"]) == 1


def test_doctor_program_restricted_block_for_other_app_is_not_evidence(tmp_path):
    scenario = (
        '$global:FirewallRules = @([pscustomobject]@{ Name="chrome-block"; DisplayName="chrome-block"; '
        'Direction="Inbound"; Action="Block"; Enabled=$true; Profile=@("Private","Public") }); '
        '$global:PortFilters["chrome-block"] = [pscustomobject]@{ Protocol="TCP"; LocalPort="Any" }; '
        '$global:ApplicationFilters["chrome-block"] = [pscustomobject]@{ Program="C:\\Program Files\\chrome.exe" }'
    )
    result, parsed = _run_doctor(tmp_path, scenario, "-Json")
    assert len(parsed["firewall"]["explicit_block_evidence"]) == 0
    assert parsed["firewall"]["reason_code"] == "firewall_rule_missing"


def test_doctor_program_restricted_block_for_our_listener_is_evidence(tmp_path):
    scenario = (
        '$global:FirewallRules = @([pscustomobject]@{ Name="python-block"; DisplayName="python-block"; '
        'Direction="Inbound"; Action="Block"; Enabled=$true; Profile=@("Private","Public") }); '
        '$global:PortFilters["python-block"] = [pscustomobject]@{ Protocol="TCP"; LocalPort="Any" }; '
        '$global:ApplicationFilters["python-block"] = [pscustomobject]@{ Program="C:\\Users\\thene\\projects\\HASHI3\\.venv\\Scripts\\python.exe" }'
    )
    result, parsed = _run_doctor(tmp_path, scenario, "-Json")
    assert parsed["firewall"]["reason_code"] == "firewall_explicitly_blocked"
    assert len(parsed["firewall"]["explicit_block_evidence"]) == 1


def _owned_rule_scenario(profile, local_port, remote_address, enabled):
    return (
        '$global:FirewallRules = @([pscustomobject]@{ Name="HashiRemote_DOCTOR-CONTRACT_TCP_8767"; '
        "DisplayName='HASHI Remote MSI TCP 8767'; Description='{\"purpose\":\"hashi-remote-msi-tcp\"}'; "
        f'Direction="Inbound"; Action="Allow"; Enabled=${str(enabled).lower()}; Profile=@({profile}) }}); '
        f'$global:PortFilters["HashiRemote_DOCTOR-CONTRACT_TCP_8767"] = [pscustomobject]@{{ Protocol="TCP"; LocalPort="{local_port}" }}; '
        f'$global:AddressFilters["HashiRemote_DOCTOR-CONTRACT_TCP_8767"] = [pscustomobject]@{{ RemoteAddress="{remote_address}" }}'
    )


def test_doctor_owned_rule_profile_mismatch(tmp_path):
    scenario = _owned_rule_scenario('"Public"', "8767", "LocalSubnet", True)
    result, parsed = _run_doctor(tmp_path, scenario, "-Json")
    assert parsed["firewall"]["reason_code"] == "firewall_profile_mismatch"
    assert "profile_includes_public_or_any" in parsed["firewall"]["tcp_rule_issues"]


def test_doctor_owned_rule_port_mismatch(tmp_path):
    scenario = _owned_rule_scenario('"Domain","Private"', "9999", "LocalSubnet", True)
    result, parsed = _run_doctor(tmp_path, scenario, "-Json")
    assert parsed["firewall"]["reason_code"] == "firewall_rule_port_mismatch"


def test_doctor_owned_rule_remote_address_mismatch(tmp_path):
    scenario = _owned_rule_scenario('"Domain","Private"', "8767", "Any", True)
    result, parsed = _run_doctor(tmp_path, scenario, "-Json")
    assert parsed["firewall"]["reason_code"] == "firewall_remote_address_mismatch"


def test_doctor_owned_rule_disabled(tmp_path):
    scenario = _owned_rule_scenario('"Domain","Private"', "8767", "LocalSubnet", False)
    result, parsed = _run_doctor(tmp_path, scenario, "-Json")
    assert parsed["firewall"]["reason_code"] == "firewall_rule_disabled"


def test_doctor_owned_rule_healthy_is_ok(tmp_path):
    scenario = _owned_rule_scenario('"Domain","Private"', "8767", "LocalSubnet", True)
    result, parsed = _run_doctor(tmp_path, scenario, "-Json")
    assert parsed["firewall"]["state"] == "ok"
    assert parsed["firewall"]["reason_code"] == ""
    assert parsed["lan_readiness"]["state"] == "ok"


def test_doctor_gpo_local_merge_disabled(tmp_path):
    scenario = (
        '$global:Profiles = @('
        '  [pscustomobject]@{ Name="Domain"; Enabled=$true; DefaultInboundAction="Allow"; AllowLocalFirewallRules=$false },'
        '  [pscustomobject]@{ Name="Private"; Enabled=$true; DefaultInboundAction="Allow"; AllowLocalFirewallRules=$false },'
        '  [pscustomobject]@{ Name="Public"; Enabled=$true; DefaultInboundAction="Allow"; AllowLocalFirewallRules=$false })'
    )
    result, parsed = _run_doctor(tmp_path, scenario, "-Json")
    assert parsed["firewall"]["reason_code"] == "firewall_local_rules_ignored_by_policy"
    assert parsed["firewall"]["local_rules_ignored_by_policy"] is True


def test_doctor_ps51_rule_enabled_enum_true_is_ok(tmp_path):
    scenario = (
        '$global:FirewallRules = @([pscustomobject]@{ Name="HashiRemote_DOCTOR-CONTRACT_TCP_8767"; '
        "DisplayName='HASHI Remote MSI TCP 8767'; Description='{\"purpose\":\"hashi-remote-msi-tcp\"}'; "
        'Direction=[Microsoft.PowerShell.Cmdletization.GeneratedTypes.NetSecurity.Direction]::Inbound; '
        'Action=[Microsoft.PowerShell.Cmdletization.GeneratedTypes.NetSecurity.Action]::Allow; '
        'Enabled=[Microsoft.PowerShell.Cmdletization.GeneratedTypes.NetSecurity.Enabled]::True; '
        'Profile=@([Microsoft.PowerShell.Cmdletization.GeneratedTypes.NetSecurity.Profile]::Domain, '
        '[Microsoft.PowerShell.Cmdletization.GeneratedTypes.NetSecurity.Profile]::Private) }); '
        '$global:PortFilters["HashiRemote_DOCTOR-CONTRACT_TCP_8767"] = [pscustomobject]@{ Protocol="TCP"; LocalPort="8767" }; '
        '$global:AddressFilters["HashiRemote_DOCTOR-CONTRACT_TCP_8767"] = [pscustomobject]@{ RemoteAddress="LocalSubnet" }'
    )
    result, parsed = _run_doctor(tmp_path, scenario, "-Json")
    assert result.returncode == 0, result.stderr
    assert parsed["firewall"]["state"] == "ok"
    assert parsed["firewall"]["reason_code"] == ""
    assert parsed["firewall"]["tcp_rule"]["enabled"] is True
    assert sorted(parsed["firewall"]["tcp_rule"]["profile"]) == ["Domain", "Private"]


def test_doctor_ps51_rule_enabled_enum_false_is_disabled(tmp_path):
    # PS 5.1 exposes rule Enabled as an enum (True=1/False=2). A bare [bool]
    # cast misreads False(2) as $true, so a disabled rule would previously look
    # enabled. The canonical string comparison must report it disabled.
    scenario = (
        '$global:FirewallRules = @([pscustomobject]@{ Name="HashiRemote_DOCTOR-CONTRACT_TCP_8767"; '
        "DisplayName='HASHI Remote MSI TCP 8767'; Description='{\"purpose\":\"hashi-remote-msi-tcp\"}'; "
        'Direction=[Microsoft.PowerShell.Cmdletization.GeneratedTypes.NetSecurity.Direction]::Inbound; '
        'Action=[Microsoft.PowerShell.Cmdletization.GeneratedTypes.NetSecurity.Action]::Allow; '
        'Enabled=[Microsoft.PowerShell.Cmdletization.GeneratedTypes.NetSecurity.Enabled]::False; '
        'Profile=@([Microsoft.PowerShell.Cmdletization.GeneratedTypes.NetSecurity.Profile]::Domain, '
        '[Microsoft.PowerShell.Cmdletization.GeneratedTypes.NetSecurity.Profile]::Private) }); '
        '$global:PortFilters["HashiRemote_DOCTOR-CONTRACT_TCP_8767"] = [pscustomobject]@{ Protocol="TCP"; LocalPort="8767" }; '
        '$global:AddressFilters["HashiRemote_DOCTOR-CONTRACT_TCP_8767"] = [pscustomobject]@{ RemoteAddress="LocalSubnet" }'
    )
    result, parsed = _run_doctor(tmp_path, scenario, "-Json")
    assert parsed["firewall"]["tcp_rule"]["enabled"] is False
    assert parsed["firewall"]["reason_code"] == "firewall_rule_disabled"


def test_doctor_ps51_profile_gpoboolean_notconfigured_is_not_gpo_block(tmp_path):
    scenario = (
        '$global:Profiles = @('
        '  [pscustomobject]@{ Name="Domain"; Enabled=$true; DefaultInboundAction="Allow"; '
        'AllowLocalFirewallRules=[Microsoft.PowerShell.Cmdletization.GeneratedTypes.NetSecurity.GpoBoolean]::NotConfigured },'
        '  [pscustomobject]@{ Name="Private"; Enabled=$true; DefaultInboundAction="Allow"; '
        'AllowLocalFirewallRules=[Microsoft.PowerShell.Cmdletization.GeneratedTypes.NetSecurity.GpoBoolean]::NotConfigured },'
        '  [pscustomobject]@{ Name="Public"; Enabled=$true; DefaultInboundAction="Allow"; '
        'AllowLocalFirewallRules=[Microsoft.PowerShell.Cmdletization.GeneratedTypes.NetSecurity.GpoBoolean]::NotConfigured })'
    )
    result, parsed = _run_doctor(tmp_path, scenario, "-Json")
    assert parsed["firewall"]["local_rules_ignored_by_policy"] is False
    assert parsed["firewall"]["reason_code"] == "firewall_rule_missing"
    for profile in parsed["firewall"]["profiles"]:
        assert profile["allow_local_firewall_rules"] is None


def test_doctor_ps51_profile_gpoboolean_false_flags_gpo(tmp_path):
    scenario = (
        '$global:Profiles = @('
        '  [pscustomobject]@{ Name="Domain"; Enabled=$true; DefaultInboundAction="Allow"; '
        'AllowLocalFirewallRules=[Microsoft.PowerShell.Cmdletization.GeneratedTypes.NetSecurity.GpoBoolean]::False },'
        '  [pscustomobject]@{ Name="Private"; Enabled=$true; DefaultInboundAction="Allow"; '
        'AllowLocalFirewallRules=[Microsoft.PowerShell.Cmdletization.GeneratedTypes.NetSecurity.GpoBoolean]::False },'
        '  [pscustomobject]@{ Name="Public"; Enabled=$true; DefaultInboundAction="Allow"; '
        'AllowLocalFirewallRules=[Microsoft.PowerShell.Cmdletization.GeneratedTypes.NetSecurity.GpoBoolean]::False })'
    )
    result, parsed = _run_doctor(tmp_path, scenario, "-Json")
    assert parsed["firewall"]["local_rules_ignored_by_policy"] is True
    assert parsed["firewall"]["reason_code"] == "firewall_local_rules_ignored_by_policy"


def test_doctor_ps51_block_rule_enum_direction_action_is_evidence(tmp_path):
    scenario = (
        '$global:FirewallRules = @([pscustomobject]@{ Name="enum-block"; DisplayName="enum-block"; '
        'Direction=[Microsoft.PowerShell.Cmdletization.GeneratedTypes.NetSecurity.Direction]::Inbound; '
        'Action=[Microsoft.PowerShell.Cmdletization.GeneratedTypes.NetSecurity.Action]::Block; '
        'Enabled=[Microsoft.PowerShell.Cmdletization.GeneratedTypes.NetSecurity.Enabled]::True; '
        'Profile=@([Microsoft.PowerShell.Cmdletization.GeneratedTypes.NetSecurity.Profile]::Any) }); '
        '$global:PortFilters["enum-block"] = [pscustomobject]@{ Protocol="TCP"; LocalPort="8767" }'
    )
    result, parsed = _run_doctor(tmp_path, scenario, "-Json")
    assert parsed["firewall"]["reason_code"] == "firewall_explicitly_blocked"
    assert len(parsed["firewall"]["explicit_block_evidence"]) == 1

def test_doctor_loopback_only_bind_is_not_lan_ready(tmp_path):
    scenario = (
        '$global:Listeners = @([pscustomobject]@{ LocalAddress="127.0.0.1"; LocalPort=8767; OwningProcess=$global:OwnedPid })'
    )
    result, parsed = _run_doctor(tmp_path, scenario, "-Json")
    assert parsed["listener"]["state"] == "degraded"
    assert parsed["listener"]["reason_code"] == "loopback_only_bind"
    assert parsed["lan_readiness"]["state"] == "degraded"


def test_doctor_port_owned_by_other_process(tmp_path):
    scenario = (
        '$global:Listeners = @([pscustomobject]@{ LocalAddress="0.0.0.0"; LocalPort=8767; OwningProcess=7777 })'
    )
    result, parsed = _run_doctor(tmp_path, scenario, "-Json")
    assert parsed["listener"]["state"] == "degraded"
    assert parsed["listener"]["reason_code"] == "port_owned_by_other_process"


def test_doctor_remote_down_reports_process_down_and_exit2(tmp_path):
    scenario = '$global:HealthReachable = $false; $global:Listeners = @()'
    result, parsed = _run_doctor(tmp_path, scenario, "-Json")
    assert result.returncode == 2
    assert parsed["local_health"]["accepted"] is False
    assert "remote_process_down" in parsed["lan_readiness"]["reason_codes"]
    assert parsed["lan_readiness"]["state"] == "degraded"


def test_doctor_push_rejected_does_not_fail_diagnostics(tmp_path):
    scenario = '$global:PushResult = @{ ok = $false }'
    result, parsed = _run_doctor(tmp_path, scenario, "-Json")
    assert result.returncode == 0, result.stderr
    assert parsed["push"]["pushed"] is True
    assert parsed["push"]["ok"] is False


def test_doctor_unavailable_firewall_module_is_unknown_not_green(tmp_path):
    scenario = 'function Get-NetFirewallRule { throw "Access denied" }'
    result, parsed = _run_doctor(tmp_path, scenario, "-Json")
    assert result.returncode == 0, result.stderr
    assert parsed["firewall"]["reason_code"] == "diagnostics_unavailable"
    assert parsed["firewall"]["effective_policy_checked"] is False


# ── Repair lifecycle ──────────────────────────────────────────────────

def test_repair_creates_narrow_tcp_and_shared_mdns_rules(tmp_path):
    root = _prepare_root(tmp_path)
    stub = tmp_path / "stubs.ps1"
    stub.write_text(STUB_TEMPLATE + f"\n$global:OwnedRoot = {_ps_string(root)}\n", encoding="utf-8")
    result = _run_repair_body_file(tmp_path, stub)
    assert result.returncode == 0, result.stderr
    outcome = json.loads((tmp_path / "repair-result.json").read_text(encoding="utf-8-sig"))
    assert outcome["Outcome"]["repaired"] is True
    assert "tcp_rule_created" in outcome["Outcome"]["changes"]
    assert "mdns_rule_created" in outcome["Outcome"]["changes"]
    calls = {call["Name"]: call for call in outcome["NewRuleCalls"]}
    tcp = calls["HashiRemote_DOCTOR-CONTRACT_TCP_8767"]
    assert tcp["Protocol"] == "TCP"
    assert tcp["LocalPort"] == "8767"
    assert tcp["RemoteAddress"] == "LocalSubnet"
    assert sorted(tcp["Profile"]) == ["Domain", "Private"]
    mdns = calls["HashiRemote_mDNS_UDP_5353"]
    assert mdns["Protocol"] == "UDP"
    assert mdns["LocalPort"] == "5353"
    assert mdns["RemoteAddress"] == "LocalSubnet"
    assert sorted(mdns["Profile"]) == ["Domain", "Private"]


def _run_repair_body_file(tmp_path: Path, stub: Path) -> subprocess.CompletedProcess:
    functions = _extract_repair_functions()
    wrapper = tmp_path / "repair-wrapper.ps1"
    wrapper.write_text(
        "param([string]$StubFile)\n. ([System.IO.Path]::GetFullPath($StubFile))\n$HashiRoot = $global:OwnedRoot\n"
        + functions
        + f"\n$Outcome = Repair-HashiRemoteFirewallRules -EffectivePort 8767 -InstanceId 'DOCTOR-CONTRACT'\n"
        + "[pscustomobject]@{ Outcome=$Outcome; NewRuleCalls=@($global:NewRuleCalls); Rules=@($global:FirewallRules | ForEach-Object { [pscustomobject]@{ Name=[string]$_.Name; Description=[string]$_.Description } }) } | "
        + f"ConvertTo-Json -Depth 8 | Set-Content -LiteralPath {_ps_string(tmp_path / 'repair-result.json')} -Encoding UTF8\n",
        encoding="utf-8",
    )
    args = [
        "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
        "-File", str(wrapper),
        "-StubFile", str(stub),
    ]
    return subprocess.run(args, capture_output=True, text=True, timeout=120, env=os.environ.copy())


def _extract_repair_functions() -> str:
    ctl = CTL.read_text(encoding="utf-8")
    start = ctl.index("function Get-RemoteFirewallRuleNames {")
    end = ctl.index("function Show-RemoteDoctor {")
    return ctl[start:end]


def test_repair_updates_existing_rule_port(tmp_path):
    root = _prepare_root(tmp_path)
    scenario = (
        '$global:FirewallRules = @([pscustomobject]@{ Name="HashiRemote_DOCTOR-CONTRACT_TCP_8767"; '
        "DisplayName='HASHI Remote MSI TCP 8767'; Description='{\"purpose\":\"hashi-remote-msi-tcp\"}'; "
        'Direction="Inbound"; Action="Allow"; Enabled=$true; Profile=@("Domain") }); '
        '$global:PortFilters["HashiRemote_DOCTOR-CONTRACT_TCP_8767"] = [pscustomobject]@{ Protocol="TCP"; LocalPort="9999" }; '
        '$global:AddressFilters["HashiRemote_DOCTOR-CONTRACT_TCP_8767"] = [pscustomobject]@{ RemoteAddress="LocalSubnet" }'
    )
    stub = tmp_path / "stubs.ps1"
    stub.write_text(STUB_TEMPLATE + f"\n$global:OwnedRoot = {_ps_string(root)}\n" + scenario + "\n", encoding="utf-8")
    result = _run_repair_body_file(tmp_path, stub)
    assert result.returncode == 0, result.stderr
    outcome = json.loads((tmp_path / "repair-result.json").read_text(encoding="utf-8-sig"))
    assert outcome["Outcome"]["repaired"] is True
    assert "tcp_rule_updated" in outcome["Outcome"]["changes"]


def test_repair_mdns_refcount_keeps_other_instances(tmp_path):
    root = _prepare_root(tmp_path)
    scenario = (
        '$global:FirewallRules = @([pscustomobject]@{ Name="HashiRemote_mDNS_UDP_5353"; '
        "DisplayName='HASHI Remote mDNS UDP 5353'; "
        "Description='{\"purpose\":\"hashi-remote-mdns-udp-5353\",\"instances\":[\"HASHI6\"]}'; "
        'Direction="Inbound"; Action="Allow"; Enabled=$true; Profile=@("Domain","Private") }); '
        '$global:PortFilters["HashiRemote_mDNS_UDP_5353"] = [pscustomobject]@{ Protocol="UDP"; LocalPort="5353" }'
    )
    stub = tmp_path / "stubs.ps1"
    stub.write_text(STUB_TEMPLATE + f"\n$global:OwnedRoot = {_ps_string(root)}\n" + scenario + "\n", encoding="utf-8")
    result = _run_repair_body_file(tmp_path, stub)
    assert result.returncode == 0, result.stderr
    outcome = json.loads((tmp_path / "repair-result.json").read_text(encoding="utf-8-sig"))
    assert outcome["Outcome"]["repaired"] is True
    assert "mdns_rule_registered" in outcome["Outcome"]["changes"]


def test_repair_declined_uac_is_a_diagnostic_outcome(tmp_path):
    scenario = '$global:UacDeclined = $true'
    result, parsed = _run_doctor(
        tmp_path, scenario, "-Repair -Json",
        env_extra={"HASHI_REMOTE_REPAIR_TEST_NON_ELEVATED": "1"},
    )
    assert result.returncode == 4, result.stdout
    assert parsed["repair"]["repaired"] is False
    assert parsed["repair"]["uac_declined"] is True
    assert parsed["repair"]["elevated"] is False


def test_repair_elevated_child_success_is_relayed(tmp_path):
    child_payload = {
        "repair": {
            "repaired": True,
            "elevated": True,
            "uac_declined": False,
            "changes": ["tcp_rule_created", "mdns_rule_created"],
            "error": "",
            "health_accepted": True,
        }
    }
    scenario = (
        "$global:UacDeclined = $false; $global:ChildExitCode = 0; "
        f"$global:ChildOutput = {_ps_string(json.dumps(child_payload))}"
    )
    result, parsed = _run_doctor(
        tmp_path, scenario, "-Repair -Json",
        env_extra={"HASHI_REMOTE_REPAIR_TEST_NON_ELEVATED": "1"},
    )
    assert result.returncode == 0, result.stdout
    assert parsed["repair"]["repaired"] is True
    assert parsed["repair"]["elevated"] is True
    assert "tcp_rule_created" in parsed["repair"]["changes"]

# ── Strict Allow-rule exact-template verification ────────────────────────────────

def test_doctor_owned_tcp_rule_any_port_is_not_exact(tmp_path):
    scenario = _owned_rule_scenario('"Domain","Private"', "Any", "LocalSubnet", True)
    result, parsed = _run_doctor(tmp_path, scenario, "-Json")
    assert parsed["firewall"]["reason_code"] == "firewall_rule_port_mismatch"
    assert "port_mismatch" in parsed["firewall"]["tcp_rule_issues"]


def test_doctor_owned_tcp_rule_range_covering_port_is_not_exact(tmp_path):
    scenario = _owned_rule_scenario('"Domain","Private"', "8760-8770", "LocalSubnet", True)
    result, parsed = _run_doctor(tmp_path, scenario, "-Json")
    assert parsed["firewall"]["reason_code"] == "firewall_rule_port_mismatch"
    assert "port_mismatch" in parsed["firewall"]["tcp_rule_issues"]


def test_doctor_owned_tcp_rule_exact_port_stays_compliant(tmp_path):
    scenario = _owned_rule_scenario('"Domain","Private"', "8767", "LocalSubnet", True)
    result, parsed = _run_doctor(tmp_path, scenario, "-Json")
    assert parsed["firewall"]["state"] == "ok"
    assert "port_mismatch" not in parsed["firewall"]["tcp_rule_issues"]


def test_doctor_block_rule_range_covering_port_is_still_evidence(tmp_path):
    # Coverage semantics are preserved for Block-rule diagnosis: a broad
    # range may legitimately affect the target port and must still register.
    scenario = (
        '$global:FirewallRules = @([pscustomobject]@{ Name="range-block"; DisplayName="range-block"; '
        'Direction="Inbound"; Action="Block"; Enabled=$true; Profile=@("Any") }); '
        '$global:PortFilters["range-block"] = [pscustomobject]@{ Protocol="TCP"; LocalPort="8000-9000" }'
    )
    result, parsed = _run_doctor(tmp_path, scenario, "-Json")
    assert parsed["firewall"]["reason_code"] == "firewall_explicitly_blocked"
    assert len(parsed["firewall"]["explicit_block_evidence"]) == 1


def _owned_mdns_rule_scenario(protocol="UDP", local_port="5353", remote_address="LocalSubnet",
                              profile='"Domain","Private"', direction="Inbound", action="Allow",
                              enabled=True, purpose="hashi-remote-mdns-udp-5353"):
    description = json.dumps({"purpose": purpose})
    return (
        '$global:FirewallRules = @([pscustomobject]@{ Name="HashiRemote_mDNS_UDP_5353"; '
        "DisplayName='HASHI Remote mDNS UDP 5353'; "
        f"Description='{description}'; "
        f'Direction="{direction}"; Action="{action}"; Enabled=${str(enabled).lower()}; '
        f'Profile=@({profile}) }}); '
        f'$global:PortFilters["HashiRemote_mDNS_UDP_5353"] = [pscustomobject]@{{ Protocol="{protocol}"; LocalPort="{local_port}" }}; '
        f'$global:AddressFilters["HashiRemote_mDNS_UDP_5353"] = [pscustomobject]@{{ RemoteAddress="{remote_address}" }}'
    )


def test_doctor_owned_mdns_rule_exact_template_is_compliant(tmp_path):
    scenario = _owned_mdns_rule_scenario()
    result, parsed = _run_doctor(tmp_path, scenario, "-Json")
    assert parsed["firewall"]["mdns_rule"]["present"] is True
    assert parsed["firewall"]["mdns_rule"]["issues"] == []
    assert "mdns_rule_missing" not in parsed["lan_readiness"]["reason_codes"]


def test_doctor_owned_mdns_rule_udp_any_is_not_exact(tmp_path):
    scenario = _owned_mdns_rule_scenario(local_port="Any")
    result, parsed = _run_doctor(tmp_path, scenario, "-Json")
    assert "port_mismatch" in parsed["firewall"]["mdns_rule"]["issues"]
    assert "mdns_rule_missing" in parsed["lan_readiness"]["reason_codes"]


def test_doctor_owned_mdns_rule_wrong_port_is_not_exact(tmp_path):
    scenario = _owned_mdns_rule_scenario(local_port="5354")
    result, parsed = _run_doctor(tmp_path, scenario, "-Json")
    assert "port_mismatch" in parsed["firewall"]["mdns_rule"]["issues"]


def test_doctor_owned_mdns_rule_wrong_protocol(tmp_path):
    scenario = _owned_mdns_rule_scenario(protocol="TCP")
    result, parsed = _run_doctor(tmp_path, scenario, "-Json")
    assert "protocol_not_udp" in parsed["firewall"]["mdns_rule"]["issues"]


def test_doctor_owned_mdns_rule_public_profile(tmp_path):
    scenario = _owned_mdns_rule_scenario(profile='"Public"')
    result, parsed = _run_doctor(tmp_path, scenario, "-Json")
    assert "profile_includes_public_or_any" in parsed["firewall"]["mdns_rule"]["issues"]


def test_doctor_owned_mdns_rule_remote_address_any(tmp_path):
    scenario = _owned_mdns_rule_scenario(remote_address="Any")
    result, parsed = _run_doctor(tmp_path, scenario, "-Json")
    assert "remote_address_not_local_subnet" in parsed["firewall"]["mdns_rule"]["issues"]


def test_doctor_owned_mdns_rule_disabled(tmp_path):
    scenario = _owned_mdns_rule_scenario(enabled=False)
    result, parsed = _run_doctor(tmp_path, scenario, "-Json")
    assert "rule_disabled" in parsed["firewall"]["mdns_rule"]["issues"]


def test_doctor_owned_mdns_rule_wrong_annotation(tmp_path):
    scenario = _owned_mdns_rule_scenario(purpose="someone-elses-rule")
    result, parsed = _run_doctor(tmp_path, scenario, "-Json")
    assert "ownership_annotation_unexpected" in parsed["firewall"]["mdns_rule"]["issues"]


def test_doctor_owned_mdns_rule_wrong_direction(tmp_path):
    scenario = _owned_mdns_rule_scenario(direction="Outbound")
    result, parsed = _run_doctor(tmp_path, scenario, "-Json")
    assert "direction_not_inbound" in parsed["firewall"]["mdns_rule"]["issues"]


def test_repair_leaves_unrelated_rules_unchanged(tmp_path):
    root = _prepare_root(tmp_path)
    scenario = (
        '$global:FirewallRules = @([pscustomobject]@{ Name="other-app-rule"; '
        "DisplayName='Other App'; Description='keep-me'; "
        'Direction="Inbound"; Action="Allow"; Enabled=$true; Profile=@("Private") })'
    )
    stub = tmp_path / "stubs.ps1"
    stub.write_text(STUB_TEMPLATE + f"\n$global:OwnedRoot = {_ps_string(root)}\n" + scenario + "\n", encoding="utf-8")
    result = _run_repair_body_file(tmp_path, stub)
    assert result.returncode == 0, result.stderr
    outcome = json.loads((tmp_path / "repair-result.json").read_text(encoding="utf-8-sig"))
    others = [rule for rule in outcome["Rules"] if rule["Name"] == "other-app-rule"]
    assert len(others) == 1
    assert others[0]["Description"] == "keep-me"
    assert "tcp_rule_created" in outcome["Outcome"]["changes"]

# HASHI-Fingerprint: hfp_f1f79ab5af79464d832ec1716f44a388
