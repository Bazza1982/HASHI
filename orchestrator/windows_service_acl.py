"""Narrow Windows service-control ACL support for Hashi Remote."""
from __future__ import annotations

import os
import re
import subprocess


_WINDOWS_SID = re.compile(r"S-1(?:-\d+)+", re.IGNORECASE)
_SERVICE_NAME = re.compile(r"[A-Za-z0-9_.-]{1,128}")


def windows_service_restart_ace(runtime_sid: str) -> str:
    """Return the exact allow ACE for service start and stop only."""

    return f"(A;;RPWP;;;{_validated_sid(runtime_sid)})"


def windows_service_has_restart_access(sddl: str, runtime_sid: str) -> bool:
    return windows_service_restart_ace(runtime_sid) in str(sddl or "")


def insert_windows_service_restart_ace(sddl: str, runtime_sid: str) -> str:
    """Insert the narrow ACE into the DACL while preserving any SACL."""

    value = str(sddl or "").strip()
    if not value.startswith("D:"):
        raise ValueError("service security descriptor has no DACL")
    ace = windows_service_restart_ace(runtime_sid)
    if ace in value:
        return value
    sacl_at = value.find("S:", 2)
    return value + ace if sacl_at < 0 else value[:sacl_at] + ace + value[sacl_at:]


def remove_windows_service_restart_ace(sddl: str, runtime_sid: str) -> str:
    value = str(sddl or "").strip()
    ace = windows_service_restart_ace(runtime_sid)
    return value.replace(ace, "")


def read_windows_service_sddl(service_name: str) -> str:
    _require_windows()
    output = _run_sc("sdshow", _validated_service_name(service_name))
    for line in reversed(output.splitlines()):
        candidate = line.strip()
        if candidate.startswith("D:"):
            return candidate
    raise OSError("Windows service security descriptor was not returned")


def grant_windows_service_restart(
    service_name: str,
    *,
    runtime_sid: str,
) -> dict[str, object]:
    """Grant one principal only SERVICE_START and SERVICE_STOP."""

    name = _validated_service_name(service_name)
    before = read_windows_service_sddl(name)
    after = insert_windows_service_restart_ace(before, runtime_sid)
    changed = after != before
    if changed:
        _run_sc("sdset", name, after)
    observed = read_windows_service_sddl(name)
    if not windows_service_has_restart_access(observed, runtime_sid):
        raise OSError("Windows service restart ACE was not applied")
    return {"service_name": name, "configured": True, "changed": changed}


def revoke_windows_service_restart(
    service_name: str,
    *,
    runtime_sid: str,
) -> dict[str, object]:
    """Remove only the exact ACE installed by this module."""

    name = _validated_service_name(service_name)
    before = read_windows_service_sddl(name)
    after = remove_windows_service_restart_ace(before, runtime_sid)
    changed = after != before
    if changed:
        _run_sc("sdset", name, after)
    observed = read_windows_service_sddl(name)
    if windows_service_has_restart_access(observed, runtime_sid):
        raise OSError("Windows service restart ACE was not removed")
    return {"service_name": name, "configured": False, "changed": changed}


def _validated_sid(value: str) -> str:
    sid = str(value or "").strip().upper()
    if not _WINDOWS_SID.fullmatch(sid):
        raise ValueError("runtime principal must be a Windows SID")
    return sid


def _validated_service_name(value: str) -> str:
    name = str(value or "").strip()
    if not _SERVICE_NAME.fullmatch(name):
        raise ValueError("invalid Windows service name")
    return name


def _require_windows() -> None:
    if os.name != "nt":
        raise OSError("Windows service ACL operations require Windows")


def _run_sc(*arguments: str) -> str:
    result = subprocess.run(
        ["sc.exe", *arguments],
        capture_output=True,
        text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    output = "\n".join(
        value.strip() for value in (result.stdout, result.stderr) if value.strip()
    )
    if result.returncode != 0:
        raise OSError(
            f"Windows service ACL command failed ({result.returncode}): "
            + " ".join(["sc.exe", *arguments[:2]])
            + (f"\n{output}" if output else "")
        )
    return output
