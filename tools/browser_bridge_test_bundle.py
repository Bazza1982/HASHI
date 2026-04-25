from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.browser_bridge_handoff import write_handoff_markdown
from tools.browser_bridge_live_bundle import write_live_bundle
from tools.browser_bridge_maturity import write_maturity_report

HOST_NAME = "com.hashi.browser_bridge"
EXTENSION_ID = "jdeaedmoejdapldleofeggedgenogpka"


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _state_files(root_dir: Path) -> list[Path]:
    names = [
        "acceptance_summary.json",
        "handoff_summary.md",
        "live_acceptance_runbook.json",
        "live_bundle.json",
        "live_probe_plan.json",
        "live_probe_report.json",
        "live_readiness_report.json",
        "maturity_report.json",
        "smoke_commands.json",
        "smoke_plan.json",
        "smoke_results.json",
    ]
    return [root_dir / "state" / name for name in names]


def _copytree_clean(src: Path, dst: Path) -> None:
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)


def _write_install_ps1(path: Path, *, distro_name: str, linux_repo_root: Path, bundle_dir: Path) -> None:
    content = f"""param()

$ErrorActionPreference = "Stop"

$HostName = "{HOST_NAME}"
$ExtensionId = "{EXTENSION_ID}"
$InstallRoot = Join-Path $env:LOCALAPPDATA "HASHI\\browser_bridge_test"
$ExtensionInstallDir = Join-Path $InstallRoot "extension"
$WrapperPath = Join-Path $InstallRoot "hashi_browser_bridge_host.cmd"
$ManifestPath = Join-Path $InstallRoot "$HostName.json"
$RegistryPath = "HKCU:\\Software\\Google\\Chrome\\NativeMessagingHosts\\$HostName"
$PreviousManifestPath = Join-Path $InstallRoot "previous_manifest_path.txt"
$BundleRoot = Split-Path -Parent $PSScriptRoot
$BundleExtensionDir = Join-Path $BundleRoot "extension"

function Write-Log {{
    param([string]$Message)
    Write-Host "[HASHI Test Bundle] $Message"
}}

New-Item -ItemType Directory -Path $InstallRoot -Force | Out-Null

$existingManifest = $null
try {{
    $existingManifest = (Get-ItemProperty -Path $RegistryPath -Name "(default)" -ErrorAction Stop)."(default)"
}} catch {{
}}
if ($existingManifest) {{
    Set-Content -Path $PreviousManifestPath -Value $existingManifest -Encoding ASCII
    Write-Log "Saved previous host manifest path to $PreviousManifestPath"
}}

if (Test-Path $ExtensionInstallDir) {{
    Remove-Item -Recurse -Force $ExtensionInstallDir
}}
Copy-Item -Recurse -Force $BundleExtensionDir $ExtensionInstallDir
Write-Log "Copied extension to $ExtensionInstallDir"

$WrapperContent = @"
@echo off
cd /d %LOCALAPPDATA%
C:\\Windows\\System32\\wsl.exe -d {distro_name} bash -lc "cd '{linux_repo_root}' && /usr/bin/env python3 -m tools.browser_native_host --stdio --socket /tmp/hashi-browser-bridge.sock --log-file '{linux_repo_root}/logs/browser_native_host.log'"
"@
Set-Content -Path $WrapperPath -Value $WrapperContent -Encoding ASCII
Write-Log "Wrote native host wrapper: $WrapperPath"

$Manifest = @{{
    name = $HostName
    description = "HASHI Browser Bridge native host (test bundle)"
    path = $WrapperPath
    type = "stdio"
    allowed_origins = @("chrome-extension://$ExtensionId/")
}}
$Manifest | ConvertTo-Json -Depth 4 | Set-Content -Path $ManifestPath -Encoding ASCII
Write-Log "Wrote native host manifest: $ManifestPath"

New-Item -Path $RegistryPath -Force | Out-Null
Set-ItemProperty -Path $RegistryPath -Name "(default)" -Value $ManifestPath
Write-Log "Registered native host manifest"

Write-Host ""
Write-Host "Next:"
Write-Host "1. Open chrome://extensions"
Write-Host "2. Enable Developer mode"
Write-Host "3. Click Load unpacked"
Write-Host "4. Select: $ExtensionInstallDir"
Write-Host ""
Write-Host "Expected extension ID: $ExtensionId"
Write-Host "This is a TEST install only."
"""
    path.write_text(content, encoding="utf-8")


def _write_uninstall_ps1(path: Path) -> None:
    content = f"""param()

$ErrorActionPreference = "Stop"

$HostName = "{HOST_NAME}"
$InstallRoot = Join-Path $env:LOCALAPPDATA "HASHI\\browser_bridge_test"
$RegistryPath = "HKCU:\\Software\\Google\\Chrome\\NativeMessagingHosts\\$HostName"
$PreviousManifestPath = Join-Path $InstallRoot "previous_manifest_path.txt"

function Write-Log {{
    param([string]$Message)
    Write-Host "[HASHI Test Bundle] $Message"
}}

if (Test-Path $PreviousManifestPath) {{
    $previous = (Get-Content -Raw $PreviousManifestPath).Trim()
    if ($previous) {{
        New-Item -Path $RegistryPath -Force | Out-Null
        Set-ItemProperty -Path $RegistryPath -Name "(default)" -Value $previous
        Write-Log "Restored previous native host manifest"
    }}
}} else {{
    Remove-Item -Path $RegistryPath -Recurse -Force -ErrorAction SilentlyContinue
    Write-Log "Removed test native host registration"
}}

Remove-Item -Path $InstallRoot -Recurse -Force -ErrorAction SilentlyContinue
Write-Log "Removed test install root"
Write-Host ""
Write-Host "You can now remove the unpacked Chrome extension manually if desired."
"""
    path.write_text(content, encoding="utf-8")


def _write_cmd_launcher(path: Path, target_ps1: str) -> None:
    content = (
        "@echo off\r\n"
        'PowerShell -NoProfile -ExecutionPolicy Bypass -File "%~dp0\\'
        f"{target_ps1}"
        '"\r\n'
    )
    path.write_text(content, encoding="ascii")


def _write_readme(
    path: Path,
    *,
    bundle_dir: Path,
    root_dir: Path,
    rollback_commit: str,
) -> None:
    maturity = json.loads((root_dir / "state" / "maturity_report.json").read_text(encoding="utf-8"))
    lines = [
        "HASHI Browser Bridge Test Bundle",
        "",
        f"Generated: {datetime.now(timezone.utc).isoformat()}",
        f"Repo root: {_repo_root()}",
        f"Bundle dir: {bundle_dir}",
        f"Rollback commit: {rollback_commit}",
        f"Stage: {maturity['stage']}",
        f"Fully working: {maturity['fully_working']}",
        f"Ready for live probe: {maturity['ready_for_live_probe']}",
        "",
        "This bundle is for controlled testing only.",
        "Do not treat it as a production-ready browser bridge.",
        "",
        "Install:",
        "1. Run install\\INSTALL_HASHI_BROWSER_BRIDGE_TEST.cmd",
        "2. Open chrome://extensions",
        "3. Enable Developer mode",
        "4. Click Load unpacked",
        "5. Select the installed extension folder shown by the installer",
        "",
        "Uninstall / rollback:",
        "1. Run install\\UNINSTALL_HASHI_BROWSER_BRIDGE_TEST.cmd",
        "2. Remove the unpacked extension from Chrome if needed",
        "",
        "Key files:",
        "- extension\\",
        "- state\\handoff_summary.md",
        "- state\\maturity_report.json",
        "- state\\live_readiness_report.json",
        "- state\\live_probe_plan.json",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def build_test_bundle(
    root_dir: Path,
    *,
    bundle_dir: Path,
    rollback_commit: str,
    repo_root: Path | None = None,
    distro_name: str | None = None,
) -> dict[str, Any]:
    repo_root = repo_root or _repo_root()
    distro_name = distro_name or os.environ.get("WSL_DISTRO_NAME", "Ubuntu-22.04")

    write_live_bundle(root_dir, repo_root=repo_root, rollback_commit=rollback_commit)
    write_maturity_report(root_dir)
    write_handoff_markdown(root_dir)

    bundle_dir.mkdir(parents=True, exist_ok=True)
    extension_src = repo_root / "tools" / "chrome_extension" / "hashi_browser_bridge"
    extension_dst = bundle_dir / "extension"
    state_dst = bundle_dir / "state"
    install_dst = bundle_dir / "install"

    _copytree_clean(extension_src, extension_dst)
    if state_dst.exists():
        shutil.rmtree(state_dst)
    state_dst.mkdir(parents=True, exist_ok=True)
    for src in _state_files(root_dir):
        if src.exists():
            shutil.copy2(src, state_dst / src.name)

    install_dst.mkdir(parents=True, exist_ok=True)
    _write_install_ps1(
        install_dst / "INSTALL_HASHI_BROWSER_BRIDGE_TEST.ps1",
        distro_name=distro_name,
        linux_repo_root=repo_root,
        bundle_dir=bundle_dir,
    )
    _write_uninstall_ps1(install_dst / "UNINSTALL_HASHI_BROWSER_BRIDGE_TEST.ps1")
    _write_cmd_launcher(install_dst / "INSTALL_HASHI_BROWSER_BRIDGE_TEST.cmd", "INSTALL_HASHI_BROWSER_BRIDGE_TEST.ps1")
    _write_cmd_launcher(install_dst / "UNINSTALL_HASHI_BROWSER_BRIDGE_TEST.cmd", "UNINSTALL_HASHI_BROWSER_BRIDGE_TEST.ps1")
    _write_readme(bundle_dir / "README.txt", bundle_dir=bundle_dir, root_dir=root_dir, rollback_commit=rollback_commit)

    result = {
        "bundle_dir": str(bundle_dir),
        "extension_dir": str(extension_dst),
        "state_dir": str(state_dst),
        "install_dir": str(install_dst),
        "rollback_commit": rollback_commit,
        "distro_name": distro_name,
        "repo_root": str(repo_root),
        "expected_extension_id": EXTENSION_ID,
    }
    (bundle_dir / "bundle_meta.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a Windows-visible HASHI Browser Bridge test bundle")
    parser.add_argument("command", choices=["build"])
    parser.add_argument("--root", required=True)
    parser.add_argument("--bundle-dir", required=True)
    parser.add_argument("--rollback-commit", required=True)
    parser.add_argument("--repo-root", default=str(_repo_root()))
    parser.add_argument("--distro-name", default=os.environ.get("WSL_DISTRO_NAME", "Ubuntu-22.04"))
    args = parser.parse_args()

    result = build_test_bundle(
        Path(args.root),
        bundle_dir=Path(args.bundle_dir),
        rollback_commit=args.rollback_commit,
        repo_root=Path(args.repo_root),
        distro_name=args.distro_name,
    )
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
