"""Private instance credentials prepared during personal Functions startup."""
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
    """Resolve the exact registered Remote task without machine-wide enumeration."""

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
# The installer registers one instance task in the scheduler root. Accessing
# that exact task through COM avoids loading ScheduledTasks/CIM and enumerating
# every task during cold startup.
$service = New-Object -ComObject 'Schedule.Service'
$service.Connect()
$folder = $service.GetFolder('\')
try {
    $task = $folder.GetTask($env:HASHI_REMOTE_PRINCIPAL_QUERY_TASK)
} catch {
    $failure = $_.Exception
    while ($failure.InnerException) { $failure = $failure.InnerException }
    # Only ERROR_FILE_NOT_FOUND means the exact task is not installed. Access,
    # scheduler, and other failures must abort before publishing credentials.
    if ($failure.HResult -ne -2147024894) { throw }
    [Console]::Out.WriteLine('ABSENT')
    exit 0
}
if ($task.Path -ne ('\' + $env:HASHI_REMOTE_PRINCIPAL_QUERY_TASK)) {
    throw 'Remote task registration path does not match this instance'
}
$principal = [string]$task.Definition.Principal.UserId
if ([string]::IsNullOrWhiteSpace($principal)) {
    throw 'Remote task has no principal'
}
try {
    $sid = [System.Security.Principal.SecurityIdentifier]::new($principal).Value
} catch {
    $account = [System.Security.Principal.NTAccount]::new($principal)
    $sid = [string]$account.Translate(
        [System.Security.Principal.SecurityIdentifier]
    ).Value
}
[Console]::Out.WriteLine("SID:$sid")
"""
    environment = dict(os.environ)
    environment["HASHI_REMOTE_PRINCIPAL_QUERY_TASK"] = task_name
    try:
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
    except subprocess.TimeoutExpired as error:
        raise RuntimeError(
            "Timed out resolving the registered Remote runtime principal (10 seconds)"
        ) from error
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


def resolve_remote_runtime_sids(root: Path) -> tuple[str, ...]:
    """Return the registered Remote consumers for private config publication.

    Credential writers must carry these principals onto atomic candidates;
    otherwise a writer running under a different account can strand Remote.
    Resolution failures deliberately propagate before publication.
    """
    return _windows_remote_runtime_sids(root)


def ensure_local_api_credential(
    document: ConfigDocument,
    *,
    profile: str,
    remote_runtime_sids: Iterable[str] | None = None,
) -> ConfigDocument:
    """Prepare missing personal instance keys without replacing existing secrets.

    The local API and authenticated Remote lifecycle hop are installed together.
    Publish both keys privately in one revision-checked transaction so a normal
    installation never needs a separate feature-enablement step. Enterprise
    credentials remain owned by the configured authentication provider.
    """
    if profile != "personal":
        return document
    current = document.get("workbench_admin_token")
    if current is not None and not isinstance(current, str):
        raise ValueError("workbench_admin_token must be a string")
    network = document.get("hashi_remote_shared_token")
    if network is not None and not isinstance(network, str):
        raise ValueError("hashi_remote_shared_token must be a string")
    needs_network = not (
        (isinstance(network, str) and network.strip())
        or os.environ.get("HASHI_REMOTE_SHARED_TOKEN", "").strip()
    )
    needs_local = not (isinstance(current, str) and current.strip())
    if isinstance(remote_runtime_sids, (str, bytes)):
        raise TypeError("remote_runtime_sids must be an iterable of SIDs")
    runtime_sids = (
        tuple(remote_runtime_sids)
        if remote_runtime_sids is not None
        else resolve_remote_runtime_sids(document.source.parent)
    )
    if not needs_local and not needs_network:
        if runtime_sids:
            from tools.private_files import protect_private_file

            protect_private_file(
                document.source,
                additional_full_control_sids=runtime_sids,
            )
        return document
    updated = document.copy()
    if needs_network:
        updated["hashi_remote_shared_token"] = secrets.token_urlsafe(48)
    if needs_local:
        updated["workbench_admin_token"] = secrets.token_urlsafe(48)
    updated.revision = write_config_json(
        updated.source,
        updated,
        private_full_control_sids=runtime_sids,
    )
    return updated


def issue_scheduler_tool_token(secret, *, instance_id, agent_id, request_id, audience, tools,
                               run_id=None, fencing_token=None, now=None):
    """Delegate Scheduler calls to one signed Run attempt, never API administration."""
    import base64
    import hashlib
    import hmac
    import json
    import time

    if not all(isinstance(value, str) and value.strip()
               for value in (secret, instance_id, agent_id, request_id, audience)):
        raise ValueError("Scheduler tool credential requires a complete Run identity")
    claims = {"purpose": "hashi-scheduler-tool-v1", "instance": instance_id,
              "agent": agent_id, "request": request_id, "audience": audience,
              "tools": sorted(set(tools)), "expires": int(time.time() if now is None else now) + 3600}
    if run_id is not None or fencing_token is not None:
        if not isinstance(run_id, str) or not run_id.strip() or type(fencing_token) is not int or fencing_token <= 0:
            raise ValueError("Scheduler tool credential requires a valid Run attempt")
        claims.update(run_id=run_id, fencing_token=fencing_token)
    body = base64.urlsafe_b64encode(json.dumps(claims, sort_keys=True, separators=(",", ":")).encode()).decode().rstrip("=")
    signature = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()
    return body + "." + signature


def scheduler_tool_token_claims(secret, token, *, instance_id, agent_id, audience, tool, now=None):
    """Return authenticated claims only; PAO must separately fence the live effect."""
    import base64
    import hashlib
    import hmac
    import json
    import time

    if not secret or not isinstance(token, str) or len(token) > 8192:
        return None
    try:
        body, signature = token.split(".")
        expected = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            return None
        claims = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        if not isinstance(claims, dict):
            return None
        current = time.time() if now is None else now
        valid = (
            claims.get("purpose") == "hashi-scheduler-tool-v1"
            and claims.get("instance") == instance_id
            and claims.get("agent") == agent_id
            and claims.get("audience") == audience
            and isinstance(claims.get("request"), str) and claims["request"]
            and isinstance(claims.get("tools"), list) and tool in claims["tools"]
            and type(claims.get("expires")) is int
            and current < claims["expires"] <= current + 3600
        )
        return claims if valid else None
    except (ValueError, TypeError, KeyError, UnicodeError):
        return None


def verify_scheduler_tool_token(secret, token, **bindings):
    """Compatibility predicate; destructive callers must also validate PAO liveness."""
    return scheduler_tool_token_claims(secret, token, **bindings) is not None
# HASHI-Fingerprint: hfp_639484c6271d473aa7a60c75fdeb69b3
