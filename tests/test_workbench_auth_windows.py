"""Windows registered-principal lookup without ScheduledTasks/CIM enumeration."""
from __future__ import annotations

import os
import subprocess
from types import SimpleNamespace

import pytest

from orchestrator import workbench_auth
from orchestrator.config_json import read_config_json
from remote import supervisor_identity

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows task principal contract")

# Replace only the OS scheduler transport. Execute the real lookup and SID
# conversion in PowerShell, without creating, modifying or running any task.
_SCHEDULER = r"""
function Get-ScheduledTask { throw 'Machine-wide CIM task enumeration is unavailable' }
$script:fixtureFolder = [pscustomobject]@{}
$script:fixtureFolder | Add-Member ScriptMethod GetTask {
    param([string]$name)
    if ($name -cne $env:HASHI_REMOTE_PRINCIPAL_QUERY_TASK) {
        throw 'Wrong instance task'
    }
    if ($env:HASHI_TEST_PRINCIPAL_CASE -eq 'absent') {
        throw [System.IO.FileNotFoundException]::new('Task does not exist')
    }
    if ($env:HASHI_TEST_PRINCIPAL_CASE -eq 'denied') {
        throw [System.UnauthorizedAccessException]::new('Task access denied')
    }
    $principal = 'S-1-5-19'
    if ($env:HASHI_TEST_PRINCIPAL_CASE -eq 'account') { $principal = 'NT AUTHORITY\SYSTEM' }
    if ($env:HASHI_TEST_PRINCIPAL_CASE -eq 'empty') { $principal = '' }
    if ($env:HASHI_TEST_PRINCIPAL_CASE -eq 'invalid') { $principal = 'S-1-invalid' }
    $path = '\' + $name
    if ($env:HASHI_TEST_PRINCIPAL_CASE -eq 'foreign') { $path = '\Other\' + $name }
    return [pscustomobject]@{
        Path = $path
        Definition = [pscustomobject]@{
            Principal = [pscustomobject]@{ UserId = $principal }
        }
    }
}
$script:fixtureService = [pscustomobject]@{}
$script:fixtureService | Add-Member ScriptMethod Connect {}
$script:fixtureService | Add-Member ScriptMethod GetFolder {
    param([string]$path)
    if ($path -cne '\') { throw 'Wrong registration folder' }
    return $script:fixtureFolder
}
function New-Object {
    param([string]$ComObject)
    if ($ComObject -cne 'Schedule.Service') { throw 'Unexpected scheduler transport' }
    return $script:fixtureService
}
"""


@pytest.fixture
def registered_scheduler(tmp_path, monkeypatch):
    # The spelling is data, not PowerShell code, including quotes/metacharacters.
    name = "HashiRemote-fixture'$([Console]::Out.WriteLine('INJECTED'))"
    monkeypatch.setattr(
        supervisor_identity, "resolve_supervisor_identity",
        lambda _root: SimpleNamespace(windows_task_name=name),
    )
    real_run = subprocess.run

    def query(argv, **kwargs):
        command = list(argv)
        index = command.index("-Command") + 1
        command[index] = _SCHEDULER + "\n" + command[index]
        return real_run(command, **kwargs)

    monkeypatch.setattr(workbench_auth.subprocess, "run", query)
    return tmp_path


@pytest.mark.parametrize(
    ("case", "expected"),
    [("sid", ("S-1-5-19",)), ("account", ("S-1-5-18",)), ("absent", ())],
)
def test_exact_instance_lookup_works_without_cim_enumeration(registered_scheduler, monkeypatch, case, expected):
    monkeypatch.setenv("HASHI_TEST_PRINCIPAL_CASE", case)
    assert workbench_auth.resolve_remote_runtime_sids(registered_scheduler) == expected


@pytest.mark.parametrize("case", ["denied", "empty", "invalid", "foreign"])
def test_invalid_registration_cannot_publish_credentials(registered_scheduler, monkeypatch, case):
    monkeypatch.setenv("HASHI_TEST_PRINCIPAL_CASE", case)
    path = registered_scheduler / "secrets.json"
    original = b'{"workbench_admin_token":"existing-fixture","unrelated":"keep"}\n'
    path.write_bytes(original)
    document = read_config_json(path)

    with pytest.raises(RuntimeError, match="Could not resolve"):
        workbench_auth.ensure_local_api_credential(document, profile="personal")

    assert path.read_bytes() == original
    assert sorted(p.name for p in registered_scheduler.iterdir()) == ["secrets.json"]


@pytest.mark.parametrize(
    ("stderr", "phase"),
    [
        (b"", "powershell_startup"),
        (b"HASHI_REMOTE_QUERY_PHASE:script_ready:0\n"
         b"HASHI_REMOTE_QUERY_PHASE:connected:25\n", "connected"),
    ],
)
def test_query_timeout_fails_before_credential_publication(tmp_path, monkeypatch, stderr, phase):
    path = tmp_path / "secrets.json"
    original = b'{"workbench_admin_token":"existing-fixture","unrelated":"keep"}\n'
    path.write_bytes(original)
    document = read_config_json(path)

    def timeout(argv, **kwargs):
        assert 0 < kwargs["timeout"] <= 30
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"], stderr=stderr)

    monkeypatch.setattr(workbench_auth.subprocess, "run", timeout)
    with pytest.raises(RuntimeError, match="Timed out.*Remote runtime principal") as error:
        workbench_auth.ensure_local_api_credential(document, profile="personal")
    assert f"stage={phase}" in str(error.value)
    assert path.read_bytes() == original


@pytest.mark.parametrize(
    "output", ["SID:S-1-invalid", "SID:S-1-5-19\nABSENT", "untrusted-output"],
)
def test_malformed_query_result_fails_before_credential_publication(tmp_path, monkeypatch, output):
    path = tmp_path / "secrets.json"
    original = b'{"workbench_admin_token":"existing-fixture","unrelated":"keep"}\n'
    path.write_bytes(original)
    monkeypatch.setattr(
        workbench_auth.subprocess, "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout=output, stderr=""),
    )
    with pytest.raises(RuntimeError, match="invalid SID"):
        workbench_auth.ensure_local_api_credential(read_config_json(path), profile="personal")
    assert path.read_bytes() == original
# HASHI-Fingerprint: hfp_2e994ff6fd124df0aa95cff7985f66f4
