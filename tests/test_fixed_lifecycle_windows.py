from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from tests.test_remote_windows_supervisor import _powershell, _ps_string


ROOT = Path(__file__).resolve().parents[1]
pytestmark = [
    pytest.mark.platform,
    pytest.mark.skipif(sys.platform != "win32", reason="Native PowerShell task contract"),
]


@pytest.mark.parametrize("foreign", [False, True])
def test_split_home_fixed_tasks_upgrade_without_changing_owner_or_access(tmp_path, foreign):
    home = tmp_path / "private home with spaces"
    home.mkdir()
    (home / "agents.json").write_text(
        json.dumps({"global": {"instance_id": "FIXED-ROOTS"}}), encoding="utf-8",
    )
    result_path = tmp_path / "tasks.json"
    ctl = ROOT / "bin/hashi_restart_ctl.ps1"
    result = _powershell(f"""
$ErrorActionPreference = 'Stop'
$global:RegisteredTasks = @{{}}
function New-ScheduledTaskAction {{
    param($Execute, $Argument, $WorkingDirectory)
    [pscustomobject]@{{Execute=$Execute; Arguments=$Argument; WorkingDirectory=$WorkingDirectory}}
}}
function New-ScheduledTaskSettingsSet {{
    param($ExecutionTimeLimit)
    [pscustomobject]@{{ExecutionTimeLimit=[Xml.XmlConvert]::ToString([TimeSpan]$ExecutionTimeLimit)}}
}}
function New-ScheduledTaskPrincipal {{
    param($UserId, $LogonType, $RunLevel)
    [pscustomobject]@{{UserId=$UserId; LogonType=$LogonType; RunLevel=[string]$RunLevel}}
}}
function New-ScheduledTask {{
    param($Action, $Settings, $Principal)
    [pscustomobject]@{{Actions=@($Action); Principal=$Principal; Settings=$Settings}}
}}
function Register-ScheduledTask {{
    param($TaskName, $InputObject, [switch]$Force, $ErrorAction)
    $global:RegisteredTasks[$TaskName] = $InputObject
    # Scheduler registration may reset the task DACL.
    $global:TaskSecurity[$TaskName] = 'O:BAG:SYD:(A;;FA;;;SY)(A;;FA;;;BA)'
}}
function Get-ScheduledTask {{ param($TaskName, $ErrorAction) $global:RegisteredTasks[$TaskName] }}
function Start-ScheduledTask {{ throw 'A definition upgrade must not restart an instance' }}
& {_ps_string(ctl)} register -HashiRoot {_ps_string(home)} -CodeRoot {_ps_string(ROOT)} -Python {_ps_string(sys.executable)}
$Before = @{{}}
foreach ($Name in @($global:RegisteredTasks.Keys)) {{
    $Task = $global:RegisteredTasks[$Name]
    $Task.Principal.UserId = 'S-1-5-19'
    $Task.Principal.LogonType = 'ServiceAccount'
    $Task.Actions[0].Arguments = 'old definition'
    $global:TaskSecurity[$Name] = 'O:BAG:SYD:(A;;FA;;;SY)(A;;FA;;;BA)(A;;FR;;;NS)(A;;FR;;;LS)'
    if ('{foreign}' -eq 'True') {{
        $Task.Actions[0].WorkingDirectory = {_ps_string(tmp_path / 'foreign home')}
    }}
    $Before[$Name] = $Task | ConvertTo-Json -Depth 8 -Compress
}}
$Rejected = $false
try {{
    & {_ps_string(ctl)} ensure -HashiRoot {_ps_string(home)} -CodeRoot {_ps_string(ROOT)} -Python {_ps_string(sys.executable)} -TaskUserId 'S-1-5-18'
}} catch {{
    if ('{foreign}' -ne 'True' -or $_.Exception.Message -notmatch 'another instance home') {{ throw }}
    $Rejected = $true
}}
$Status = (& {_ps_string(ctl)} status -HashiRoot {_ps_string(home)} -CodeRoot {_ps_string(ROOT)} -Python {_ps_string(sys.executable)} | Out-String)
[pscustomobject]@{{
    Rejected=$Rejected
    Status=$Status
    Tasks=@($global:RegisteredTasks.Keys | ForEach-Object {{
        $Name = $_
        $Task = $global:RegisteredTasks[$Name]
        [pscustomobject]@{{
            Name=$Name; Task=$Task; Security=$global:TaskSecurity[$Name]
            Unchanged=($Before[$Name] -eq ($Task | ConvertTo-Json -Depth 8 -Compress))
        }}
    }})
}} | ConvertTo-Json -Depth 12 | Set-Content -Encoding UTF8 -LiteralPath {_ps_string(result_path)}
""")
    assert result.returncode == 0, result.stderr
    actual = json.loads(result_path.read_text(encoding="utf-8-sig"))
    assert actual["Rejected"] is foreign
    for item in actual["Tasks"]:
        assert item["Task"]["Principal"]["UserId"] == "S-1-5-19"
        assert item["Task"]["Principal"]["LogonType"] == "ServiceAccount"
        assert "(A;;FR;;;NS)" in item["Security"]
        if foreign:
            assert item["Unchanged"] is True
        else:
            action = item["Task"]["Actions"][0]
            assert action["WorkingDirectory"] == str(home)
            assert str(ROOT / "bin") in action["Arguments"]
            assert str(home) in action["Arguments"]
            assert item["Task"]["Principal"]["RunLevel"] == "Highest"
    if not foreign:
        assert "RuntimeDefinitionMatches : True" in actual["Status"]
        assert "DefinitionMatches        : True" in actual["Status"]
# HASHI-Fingerprint: hfp_5c344768e43b4e0282002c02a26567ed
