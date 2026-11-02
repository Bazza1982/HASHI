"""Execute only actual reporting helpers, never UAC/install/service actions."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

SOURCE = Path(__file__).resolve().parents[1] / "packaging" / "portable_windows" / "templates" / "launcher" / "Bootstrap-Elevated.ps1"
PS = shutil.which("powershell.exe") if os.name == "nt" else None


def _report(tmp_path, *, stage, attempted, exception, blocked_storage=False):
    output = tmp_path / "diagnostics"
    if blocked_storage:
        output.write_text("occupied", encoding="utf-8")
    harness = tmp_path / "report-only.ps1"
    # The AST supplies the real function. No outer startup statement is invoked.
    harness.write_text("""
param([string]$Source, [string]$Destination)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$tokens = $null
$parseErrors = $null
$tree = [Management.Automation.Language.Parser]::ParseFile($Source, [ref]$tokens, [ref]$parseErrors)
if ($parseErrors.Count) { throw 'PowerShell syntax errors' }
$definitions = @($tree.FindAll({param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Get-BootstrapFailureReport'}, $true))
if ($definitions.Count -ne 1) { throw 'Missing unique report helper' }
. ([ScriptBlock]::Create($definitions[0].Extent.Text))
$failure = [Management.Automation.ErrorRecord]::new(EXCEPTION, 'fixture', [Management.Automation.ErrorCategory]::NotSpecified, $null)
$report = Get-BootstrapFailureReport -FailureRecord $failure -Stage 'STAGE' -LaunchAttempted $ATTEMPTED -DiagnosticRoot $Destination
$report | ConvertTo-Json -Depth 6 -Compress
""".replace("EXCEPTION", exception).replace("STAGE", stage).replace("ATTEMPTED", "true" if attempted else "false"),
        encoding="utf-8-sig")
    result = subprocess.run([PS, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(harness), str(SOURCE), str(output)],
        capture_output=True, text=True, encoding="utf-8", timeout=20, check=True)
    return json.loads(result.stdout.strip())


@pytest.mark.skipif(not PS, reason="Actual Windows PowerShell reporting-helper validation")
@pytest.mark.parametrize("stage,attempted,exception,category,effect", [
    ("entry", False, "[IO.FileNotFoundException]::new('PRIVATE path')", "missing_file", "not_started"),
    ("install_root", False, "[ArgumentException]::new('PRIVATE path')", "invalid_path", "not_started"),
    ("desktop", False, "[IO.DirectoryNotFoundException]::new('PRIVATE path')", "desktop_unavailable", "not_started"),
    ("elevation", True, "[ComponentModel.Win32Exception]::new(1223, 'PRIVATE token')", "elevation_cancelled", "not_started"),
    ("elevation", True, "[UnauthorizedAccessException]::new('PRIVATE path')", "access_denied", "unconfirmed"),
    ("elevation", True, "[InvalidOperationException]::new('PRIVATE token')", "unexpected_error", "unconfirmed"),
])
def test_actual_helper_writes_safe_stage_and_truthful_startup_evidence(tmp_path, stage, attempted, exception, category, effect):
    report = _report(tmp_path, stage=stage, attempted=attempted, exception=exception)
    record = report["Record"]
    assert record["stage"] == stage and record["category"] == category and record["effect_state"] == effect
    assert report["Reference"] in report["Message"] and report["Reference"].startswith("booterr_")
    path = Path(report["DiagnosticPath"])
    assert path.parent == tmp_path / "diagnostics" and path.is_file()
    assert json.loads(path.read_text(encoding="utf-8")) == record
    assert "PRIVATE" not in json.dumps(report) + path.read_text(encoding="utf-8")


@pytest.mark.skipif(not PS, reason="Actual Windows PowerShell reporting-helper validation")
def test_failed_diagnostic_storage_never_claims_a_file_was_saved(tmp_path):
    report = _report(tmp_path, stage="entry", attempted=False,
        exception="[IO.FileNotFoundException]::new('PRIVATE path')", blocked_storage=True)
    assert report["DiagnosticPath"] is None
    assert "could not be saved" in report["Message"]
    assert report["Reference"] in report["Message"]


def test_launcher_records_each_phase_without_retrying_or_waiting_for_elevation():
    source = SOURCE.read_text(encoding="utf-8-sig")
    assert source.count("    Start-Process") == 1 and "-Verb RunAs" in source
    assert "-Wait" not in source
    assert source.index("$bootstrapLaunchAttempted = $true") < source.index("    Start-Process")
    assert "Get-BootstrapFailureReport -FailureRecord $_ -Stage $bootstrapStage -LaunchAttempted $bootstrapLaunchAttempted" in source
    assert "$report.Message," in source
    assert "Exception.Message" not in source
