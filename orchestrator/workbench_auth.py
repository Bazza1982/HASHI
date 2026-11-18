"""Private instance credentials prepared during personal Functions startup."""
from __future__ import annotations

import logging
import os
import re
import secrets
import subprocess
import time
from collections.abc import Iterable
from pathlib import Path

from orchestrator.config_json import ConfigConflictError, ConfigDocument, read_config_json, write_config_json


_WINDOWS_SID = re.compile(r"S-1(?:-\d+)+", re.IGNORECASE)
_WINDOWS_REMOTE_QUERY_TIMEOUT = 30
_WINDOWS_QUERY_PHASE = re.compile(r"HASHI_REMOTE_QUERY_PHASE:([a-z_]{1,40}):(\d{1,12})")


def _windows_query_phase(stderr: str | bytes | None) -> tuple[str, int]:
    text = stderr.decode("utf-8", errors="replace") if isinstance(stderr, bytes) else (stderr or "")
    phase, elapsed_ms = "powershell_startup", 0
    for line in text.splitlines():
        match = _WINDOWS_QUERY_PHASE.fullmatch(line.strip())
        if match:
            phase, elapsed_ms = match.group(1), int(match.group(2))
    return phase, elapsed_ms


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
$queryClock = [System.Diagnostics.Stopwatch]::StartNew()
function Write-QueryPhase([string]$Phase) {
    [Console]::Error.WriteLine(
        'HASHI_REMOTE_QUERY_PHASE:' + $Phase + ':' + $queryClock.ElapsedMilliseconds
    )
}
Write-QueryPhase 'script_ready'
# The installer registers one instance task in the scheduler root. Accessing
# that exact task through COM avoids loading ScheduledTasks/CIM and enumerating
# every task during cold startup.
$service = New-Object -ComObject 'Schedule.Service'
Write-QueryPhase 'service_created'
$service.Connect()
Write-QueryPhase 'connected'
$folder = $service.GetFolder('\')
Write-QueryPhase 'folder_opened'
try {
    $task = $folder.GetTask($env:HASHI_REMOTE_PRINCIPAL_QUERY_TASK)
} catch {
    $failure = $_.Exception
    while ($failure.InnerException) { $failure = $failure.InnerException }
    # Only ERROR_FILE_NOT_FOUND means the exact task is not installed. Access,
    # scheduler, and other failures must abort before publishing credentials.
    if ($failure.HResult -ne -2147024894) { throw }
    Write-QueryPhase 'task_missing'
    [Console]::Out.WriteLine('ABSENT')
    exit 0
}
Write-QueryPhase 'task_found'
if ($task.Path -ne ('\' + $env:HASHI_REMOTE_PRINCIPAL_QUERY_TASK)) {
    throw 'Remote task registration path does not match this instance'
}
$principal = [string]$task.Definition.Principal.UserId
if ([string]::IsNullOrWhiteSpace($principal)) {
    throw 'Remote task has no principal'
}
Write-QueryPhase 'principal_read'
try {
    $sid = [System.Security.Principal.SecurityIdentifier]::new($principal).Value
} catch {
    $account = [System.Security.Principal.NTAccount]::new($principal)
    $sid = [string]$account.Translate(
        [System.Security.Principal.SecurityIdentifier]
    ).Value
}
Write-QueryPhase 'sid_resolved'
[Console]::Out.WriteLine("SID:$sid")
"""
    environment = dict(os.environ)
    environment["HASHI_REMOTE_PRINCIPAL_QUERY_TASK"] = task_name
    started = time.monotonic()
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
            timeout=_WINDOWS_REMOTE_QUERY_TIMEOUT,
            env=environment,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except subprocess.TimeoutExpired as error:
        phase, elapsed_ms = _windows_query_phase(error.stderr)
        raise RuntimeError(
            "Timed out resolving the registered Remote runtime principal "
            f"({_WINDOWS_REMOTE_QUERY_TIMEOUT} seconds; stage={phase}, "
            f"script_elapsed_ms={elapsed_ms})"
        ) from error
    elapsed = time.monotonic() - started
    if result.returncode == 0 and elapsed >= 10:
        _phase, script_elapsed_ms = _windows_query_phase(result.stderr)
        script_elapsed = script_elapsed_ms / 1000
        logging.getLogger(__name__).warning(
            "Windows Remote principal query took %.3fs "
            "(PowerShell startup/exit %.3fs, scheduler query %.3fs)",
            elapsed, max(0, elapsed - script_elapsed), script_elapsed,
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


def resolve_remote_runtime_sids(root: Path) -> tuple[str, ...]:
    """Return the registered Remote consumers for private config publication.

    Credential writers must carry these principals onto atomic candidates;
    otherwise a writer running under a different account can strand Remote.
    Resolution failures deliberately propagate before publication.
    """
    if os.name == "nt":
        # A deliberately disabled connector has no runtime principal to serve.
        # Reuse the authoritative lifecycle configuration and avoid making an
        # unrelated Task Scheduler query part of Core/Functions readiness.
        from orchestrator.remote_lifecycle import load_settings

        if not load_settings(root).enabled:
            return ()
    return _windows_remote_runtime_sids(root)


def ensure_remote_runtime_access(secrets_path: Path, root: Path) -> None:
    """Prepare credentials only when the configured optional Remote needs them."""
    from orchestrator.remote_lifecycle import load_settings

    root = Path(root).expanduser().resolve()
    path = Path(secrets_path).expanduser().resolve()
    if not load_settings(root).enabled:
        return
    runtime_sids = resolve_remote_runtime_sids(root)
    document = read_config_json(path)
    network = document.get("hashi_remote_shared_token")
    if network is not None and not isinstance(network, str):
        raise ValueError("hashi_remote_shared_token must be a string")
    local = document.get("workbench_admin_token")
    if (
        path == root / "secrets.json"
        and isinstance(local, str) and local.strip()
        and not (isinstance(network, str) and network.strip())
        and not os.environ.get("HASHI_REMOTE_SHARED_TOKEN", "").strip()
    ):
        # Publish only after resolving the precise Remote consumer. The
        # revision-checked private candidate preserves concurrent edits.
        updated = document.copy()
        updated["hashi_remote_shared_token"] = secrets.token_urlsafe(48)
        write_config_json(path, updated, private_full_control_sids=runtime_sids)
        return
    if runtime_sids:
        from tools.private_files import protect_private_file

        protect_private_file(path, additional_full_control_sids=runtime_sids)


def scheduler_delegation_path(root: Path) -> Path:
    return Path(root) / "state" / "scheduler_delegation.json"


def scheduler_delegation_key(root: Path, *, create: bool = False) -> str:
    """Read an instance-private signing key; bootstrap creates it atomically once."""
    path = scheduler_delegation_path(root)
    try:
        document = read_config_json(path)
    except FileNotFoundError:
        if not create:
            return ""
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            write_config_json(path, {"key": secrets.token_urlsafe(48)}, expected_revision=None)
        except ConfigConflictError:
            # Another Function bootstrap won the absent-file publication race.
            pass
        document = read_config_json(path)
    key = document.get("key")
    if not isinstance(key, str) or not key.strip():
        raise ValueError("invalid scheduler delegation key")
    return key


def ensure_local_api_credential(
    document: ConfigDocument,
    *,
    profile: str,
    remote_runtime_sids: Iterable[str] | None = None,
    scheduler_root: Path | None = None,
    defer_missing_remote: bool = False,
) -> ConfigDocument:
    """Prepare missing local keys without replacing existing secrets.

    Missing keys are published privately in a revision-checked transaction.
    When an existing local key is sufficient, or the secrets document is empty,
    the configured Remote lifecycle may prepare its network key asynchronously.
    Enterprise credentials
    remain owned by the configured authentication provider. A
    governed instance gets a separate local Scheduler delegation key; it does
    not authenticate users or grant administrator access.
    """
    if profile != "personal":
        try:
            scheduler_delegation_key(scheduler_root or document.source.parent, create=True)
        except (OSError, ValueError, ConfigConflictError) as exc:
            # A local optional tool must not prevent Functions from starting.
            logging.getLogger(__name__).warning("Scheduler delegation key unavailable: %s", type(exc).__name__)
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
    if (
        defer_missing_remote and remote_runtime_sids is None
        and needs_local and not document
    ):
        # An empty document has no existing consumer data or credentials to
        # strand. Bootstrap only the private local key; optional Remote can
        # create its network key and reconcile its exact ACL when available.
        updated = document.copy()
        updated["workbench_admin_token"] = secrets.token_urlsafe(48)
        updated.revision = write_config_json(updated.source, updated)
        return updated
    if (
        defer_missing_remote and remote_runtime_sids is None
        and not needs_local and needs_network
    ):
        # The optional Remote lifecycle can publish its own missing key later.
        return document
    if not needs_local and not needs_network and remote_runtime_sids is None:
        # No publication is needed. Remote can reconcile its own consumer ACL
        # asynchronously without delaying the local Backend API.
        return document
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
                               run_id=None, fencing_token=None, owner_id=None, now=None):
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
    if owner_id is not None:
        if not isinstance(owner_id, str) or not owner_id.strip():
            raise ValueError("Scheduler tool credential requires a valid owner")
        claims["owner_id"] = owner_id
    body = base64.urlsafe_b64encode(json.dumps(claims, sort_keys=True, separators=(",", ":")).encode()).decode().rstrip("=")
    signature = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()
    return body + "." + signature


def scheduler_tool_token_claims(secret, token, *, instance_id, agent_id, audience, tool, now=None,
                                allow_expired_for_renewal=False):
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
            and claims["expires"] > 0
            and (allow_expired_for_renewal or current < claims["expires"])
            and claims["expires"] <= current + 3660
        )
        return claims if valid else None
    except (ValueError, TypeError, KeyError, UnicodeError):
        return None


def verify_scheduler_tool_token(secret, token, **bindings):
    """Compatibility predicate; destructive callers must also validate PAO liveness."""
    return scheduler_tool_token_claims(secret, token, **bindings) is not None
# HASHI-Fingerprint: hfp_639484c6271d473aa7a60c75fdeb69b3
