"""FC loopback credential bootstrap for an already authenticated Remote bridge."""
from __future__ import annotations

import os
import re
import secrets
import subprocess
from collections.abc import Iterable
from pathlib import Path

from orchestrator.config_json import ConfigDocument, write_config_json


_WINDOWS_SID = re.compile(r"S-1(?:-\d+)+", re.IGNORECASE)


def _windows_remote_runtime_sids(root: Path) -> tuple[str, ...]:
    """Resolve the registered instance Remote principal without guessing identity."""

    if os.name != "nt":
        return ()
    from remote.supervisor_identity import resolve_supervisor_identity

    task_name = resolve_supervisor_identity(root).windows_task_name
    powershell = (
        Path(os.environ.get("SystemRoot", r"C:\Windows"))
        / "System32"
        / "WindowsPowerShell"
        / "v1.0"
        / "powershell.exe"
    )
    script = r"""
$ErrorActionPreference = 'Stop'
$tasks = @(
    Get-ScheduledTask -ErrorAction Stop |
        Where-Object { $_.TaskName -eq $env:HASHI_REMOTE_PRINCIPAL_QUERY_TASK }
)
if ($tasks.Count -eq 0) {
    [Console]::Out.WriteLine('ABSENT')
    exit 0
}
if ($tasks.Count -ne 1) {
    throw 'Remote task identity is ambiguous'
}
$principal = [string]$tasks[0].Principal.UserId
if ([string]::IsNullOrWhiteSpace($principal)) {
    throw 'Remote task has no principal'
}
try {
    $account = New-Object System.Security.Principal.NTAccount($principal)
    $sid = [string]$account.Translate(
        [System.Security.Principal.SecurityIdentifier]
    ).Value
} catch {
    $sid = [string](New-Object System.Security.Principal.SecurityIdentifier($principal)).Value
}
[Console]::Out.WriteLine("SID:$sid")
"""
    environment = dict(os.environ)
    environment["HASHI_REMOTE_PRINCIPAL_QUERY_TASK"] = task_name
    result = subprocess.run(
        [
            str(powershell),
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            script,
        ],
        capture_output=True,
        text=True,
        timeout=10,
        env=environment,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    output = result.stdout.strip()
    if result.returncode != 0:
        details = (result.stderr or result.stdout or "").strip().splitlines()
        suffix = f": {details[-1][:500]}" if details else ""
        raise RuntimeError(
            "Could not resolve the registered Remote runtime principal "
            f"(exit {result.returncode}){suffix}"
        )
    if output == "ABSENT":
        return ()
    sid = output.removeprefix("SID:").strip()
    if not _WINDOWS_SID.fullmatch(sid):
        raise RuntimeError("Registered Remote runtime principal returned an invalid SID")
    return (sid.upper(),)


def ensure_local_api_credential(
    document: ConfigDocument,
    *,
    profile: str,
    remote_runtime_sids: Iterable[str] | None = None,
) -> ConfigDocument:
    """Retain existing credentials; publish a missing personal loopback key once.

    Remote already reads this canonical key before each loopback request.  A
    private, revision-checked publication lets that existing hop authenticate
    without accepting anonymous question reads or exposing the network key.
    """
    if profile != "personal":
        return document
    current = document.get("workbench_admin_token")
    if current is not None and not isinstance(current, str):
        raise ValueError("workbench_admin_token must be a string")
    network = document.get("hashi_remote_shared_token")
    if not ((isinstance(network, str) and network.strip())
            or os.environ.get("HASHI_REMOTE_SHARED_TOKEN", "").strip()):
        return document
    if isinstance(remote_runtime_sids, (str, bytes)):
        raise TypeError("remote_runtime_sids must be an iterable of SIDs")
    runtime_sids = (
        tuple(remote_runtime_sids)
        if remote_runtime_sids is not None
        else _windows_remote_runtime_sids(document.source.parent)
    )
    if isinstance(current, str) and current.strip():
        if runtime_sids:
            from tools.private_files import protect_private_file

            protect_private_file(
                document.source,
                additional_full_control_sids=runtime_sids,
            )
        return document
    updated = document.copy()
    updated["workbench_admin_token"] = secrets.token_urlsafe(48)
    updated.revision = write_config_json(
        updated.source,
        updated,
        private_full_control_sids=runtime_sids,
    )
    return updated
