"""Exercise the actual Windows runner against isolated task and health adapters."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows task runner")
RUNNER = Path(__file__).resolve().parents[1] / "bin" / "hashi_restart_task_runner.ps1"


def _ps(value: Path | str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _run(tmp_path: Path, case: str):
    shell = shutil.which("powershell.exe")
    if not shell:
        pytest.skip("Windows PowerShell is unavailable")
    code = tmp_path / "code"
    (code / "bin").mkdir(parents=True)
    stopped = tmp_path / "stopped"
    (code / "bin" / "bridge_ctl.ps1").write_text(
        "[IO.File]::WriteAllText(" + _ps(stopped) + ",'stopped')\nexit 0\n",
        encoding="utf-8",
    )
    (tmp_path / "agents.json").write_text(
        json.dumps({"global": {"instance_id": "HASHI3", "workbench_port": 65530}}),
        encoding="utf-8",
    )
    trace = tmp_path / "calls.json"
    log = tmp_path / "restart.log"
    wrapper = tmp_path / "exercise.ps1"
    script = r"""
$ErrorActionPreference = 'Stop'
$global:Trace = TRACE_PATH
$global:Case = CASE_NAME
$global:Calls = @{open=0; state=0; health=0; start=0; stop=0; cim=0}
$global:Running = $global:Case -eq 'stubborn'
$global:Result = 0
function Save-Calls {
    [IO.File]::WriteAllText($global:Trace, ($global:Calls | ConvertTo-Json -Compress))
}
$global:Task = [PSCustomObject]@{}
$global:Task | Add-Member ScriptProperty State {
    $global:Calls.state++; Save-Calls
    if ($global:Running) { return 4 }; return 3
}
$global:Task | Add-Member ScriptProperty LastTaskResult { return $global:Result }
$global:Folder = [PSCustomObject]@{}
$global:Folder | Add-Member ScriptMethod GetTask {
    param($name)
    if ($name -ne 'Isolated.Test.Runtime') { throw 'wrong task' }
    $global:Calls.open++; Save-Calls
    return $global:Task
}
$global:Scheduler = [PSCustomObject]@{}
$global:Scheduler | Add-Member ScriptMethod Connect {}
$global:Scheduler | Add-Member ScriptMethod GetFolder {
    param($path)
    if ($path -ne '\') { throw 'wrong task folder' }
    return $global:Folder
}
function global:New-Object {
    param([string]$ComObject)
    if ($ComObject -ne 'Schedule.Service') { throw 'unexpected COM class' }
    if ($global:Case -eq 'open_failure') { throw 'task read access denied' }
    return $global:Scheduler
}
function global:Get-ScheduledTask {
    $global:Calls.cim++; Save-Calls
    throw 'per-poll CIM task enumeration is forbidden in this scenario'
}
function global:Get-ScheduledTaskInfo {
    throw 'detail enumeration must not be required to observe task failure'
}
function global:Start-ScheduledTask {
    param([string]$TaskName)
    $global:Calls.start++; $global:Running=$true; Save-Calls
}
function global:Stop-ScheduledTask {
    param([string]$TaskName)
    $global:Calls.stop++
    if ($global:Case -ne 'stubborn') { $global:Running=$false }
    Save-Calls
}
function global:Start-Sleep { param([int]$Seconds,[int]$Milliseconds) }
function global:Invoke-RestMethod {
    param([string]$Uri,[string]$Method,[int]$TimeoutSec)
    $global:Calls.health++
    if ($global:Case -eq 'crash' -and $global:Calls.health -eq 2) {
        $global:Running=$false; $global:Result=7
    }
    Save-Calls
    return [PSCustomObject]@{
        ok=$true
        instance_id=$(if ($global:Case -eq 'wrong_instance') {'FOREIGN'} else {'HASHI3'})
        ready=($global:Case -ne 'crash' -and $global:Calls.health -ge 3)
        startup=@{services_ready=($global:Calls.health -ge 3);failed_agents=0}
    }
}
Save-Calls
& RUNNER_PATH -HashiRoot ROOT_PATH -CodeRoot CODE_PATH -LogPath LOG_PATH -RuntimeTaskName 'Isolated.Test.Runtime'
exit $LASTEXITCODE
"""
    for key, value in {
        "TRACE_PATH": trace, "CASE_NAME": case, "RUNNER_PATH": RUNNER,
        "ROOT_PATH": tmp_path, "CODE_PATH": code, "LOG_PATH": log,
    }.items():
        script = script.replace(key, _ps(value))
    wrapper.write_text(script, encoding="utf-8")
    p = subprocess.run(
        [shell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-File", str(wrapper)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        encoding="utf-8", errors="replace", timeout=30,
    )
    calls = json.loads(trace.read_text("utf-8"))
    return p, calls, log.read_text("utf-8-sig") if log.exists() else "", stopped.exists()


def test_runner_reuses_live_task_handle_and_finishes_when_healthy(tmp_path):
    p, calls, log, stopped = _run(tmp_path, "healthy")
    assert p.returncode == 0, p.stdout
    assert stopped
    assert calls["open"] == 1 and calls["cim"] == 0
    assert calls["health"] == 3 and calls["start"] == 1
    assert "restart completed" in log


def test_runner_still_detects_task_crash_before_readiness(tmp_path):
    p, calls, log, _ = _run(tmp_path, "crash")
    assert p.returncode != 0
    assert calls["health"] == 2 and calls["start"] == 1
    assert "failed before readiness; result=7" in log


def test_runner_never_accepts_a_foreign_instance_health(tmp_path):
    p, calls, log, _ = _run(tmp_path, "wrong_instance")
    assert p.returncode != 0
    assert calls["health"] == 300 and calls["start"] == 1
    assert "timed out waiting" in log


def test_task_read_failure_does_not_stop_the_running_instance(tmp_path):
    p, calls, _log, stopped = _run(tmp_path, "open_failure")
    assert p.returncode != 0
    assert not stopped and calls["start"] == 0


def test_old_task_that_cannot_retire_never_starts_a_second_owner(tmp_path):
    p, calls, log, _ = _run(tmp_path, "stubborn")
    assert p.returncode != 0
    assert calls["stop"] == 1 and calls["start"] == 0
    assert "did not stop cleanly" in log
# HASHI-Fingerprint: hfp_4c703d1e89624be0b2eb0c41dc7ec015
