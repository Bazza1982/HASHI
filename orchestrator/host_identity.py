"""Local host proof for authenticated Functions projections; no hostname guesses."""
from __future__ import annotations

import ctypes
from functools import lru_cache
import hashlib
import os


@lru_cache(maxsize=1)
def physical_host_id() -> str:
    if os.name != "nt":
        return ""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography",
                            0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
            guid = str(winreg.QueryValueEx(key, "MachineGuid")[0]).strip().lower()
        if not guid:
            return ""
        return hashlib.sha256(("hashi-physical-host-v1\nwindows\n" + guid).encode()).hexdigest()
    except (OSError, ValueError):
        return ""


def interactive_session_id() -> str:
    """The worker's actual Windows session, never the account's display name."""
    if os.name != "nt":
        return ""
    session = ctypes.c_uint32()
    if not ctypes.windll.kernel32.ProcessIdToSessionId(os.getpid(), ctypes.byref(session)):
        return ""
    return str(session.value) if session.value else ""


def storage_identity() -> dict:
    identity = physical_host_id()
    return {"version": 1, "physical_host_id": identity,
            "path_namespace": "windows" if identity else "unknown",
            "local_path_references": bool(identity)}
