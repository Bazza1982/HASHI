"""Own only the Windows user profile created for a disposable test identity."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import time

from scripts.permissions.windows_test_identity import CleanupUnverified, _write_manifest


class WindowsProfiles:
    def __init__(self) -> None:
        if os.name != "nt":
            raise OSError("native Windows is required")
        self._userenv = ctypes.WinDLL("userenv", use_last_error=True)
        self._userenv.DeleteProfileW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR]
        self._userenv.DeleteProfileW.restype = wintypes.BOOL

    def path(self, sid: str) -> str | None:
        import winreg

        key = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\ProfileList" + "\\" + sid
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key) as profile:
                value, _ = winreg.QueryValueEx(profile, "ProfileImagePath")
        except FileNotFoundError:
            return None
        if not isinstance(value, str) or not value:
            raise CleanupUnverified("profile registration is unreadable")
        return os.path.expandvars(value)

    def loaded(self, sid: str) -> bool:
        import winreg

        for hive in (sid, sid + "_Classes"):
            try:
                with winreg.OpenKey(winreg.HKEY_USERS, hive):
                    return True
            except FileNotFoundError:
                pass
        return False

    def delete(self, sid: str) -> None:
        if not self._userenv.DeleteProfileW(sid, None, None):
            raise CleanupUnverified(f"owned profile deletion failed (Windows error {ctypes.get_last_error()})")


class DisposableWindowsProfile:
    """No pre-existing profile can be adopted, deleted, unloaded or modified."""

    def __init__(self, accounts, identity, profiles, manifest: Path) -> None:
        self.accounts = accounts
        self.identity = identity
        self.profiles = profiles
        self.manifest = Path(manifest)
        self.sid = identity.sid
        self._active = False
        self._value = {
            "version": 1, "type": "hashi.disposable-user-profile",
            "sid": self.sid, "name": identity.name,
            "marker": identity._value["marker"], "path": None, "state": "intent",
        }

    def _check_owner(self) -> None:
        current = self.accounts.info(self.identity.name)
        if (not self.sid or current is None or current.get("sid") != self.sid
                or current.get("comment") != self._value["marker"]):
            raise CleanupUnverified("the disposable profile owner changed")

    def __enter__(self):
        self._check_owner()
        if self.profiles.path(self.sid) is not None or self.profiles.loaded(self.sid):
            raise CleanupUnverified("a pre-existing Windows profile must be preserved")
        _write_manifest(self.manifest, self._value, exclusive=True)
        self._active = True
        return self

    def before_logon(self, sid: str) -> None:
        if not self._active or sid != self.sid:
            raise CleanupUnverified("the profile preparation does not match the login")
        self._check_owner()

    def __exit__(self, exc_type, exc, traceback):
        try:
            self._check_owner()
            path = self.profiles.path(self.sid)
            self._value.update(path=path, state="created" if path is not None else "intent")
            _write_manifest(self.manifest, self._value)
            deadline = time.monotonic() + 10
            while self.profiles.loaded(self.sid):
                if time.monotonic() >= deadline:
                    raise CleanupUnverified("the owned profile is still loaded")
                time.sleep(0.05)
            if path is not None:
                self.profiles.delete(self.sid)
                if self.profiles.path(self.sid) is not None or Path(path).exists():
                    raise CleanupUnverified("owned Windows profile cleanup is unverified")
            self._value["state"] = "cleaned"
            _write_manifest(self.manifest, self._value)
        finally:
            self._active = False
        return False

def recover_profile(accounts, profiles, identity_manifest: Path, profile_manifest: Path) -> None:
    """Resume only recorded cleanup after Windows has released the owned hive.

    This never unloads a hive, closes another process's handles, restarts a
    service, or removes an account. The SCM caller must first verify that all
    recorded services are absent and the temporary logon right is gone.
    """
    from orchestrator.config_json import read_config_json, write_config_json
    from scripts.permissions.windows_test_identity import _validate_manifest

    identity = _validate_manifest(dict(read_config_json(identity_manifest)))
    value = read_config_json(profile_manifest)
    if (set(value) != {"version", "type", "sid", "name", "marker", "path", "state"}
            or type(value["version"]) is not int or value["version"] != 1
            or value["type"] != "hashi.disposable-user-profile"
            or value["state"] not in {"intent", "created", "cleaned"}
            or any(value[field] != identity[field] for field in ("sid", "name", "marker"))
            or identity["sid"] is None or identity["state"] not in {"created", "cleaned"}):
        raise CleanupUnverified("invalid retained profile ownership")
    recorded = value["path"]
    if recorded is not None and (not isinstance(recorded, str) or not Path(recorded).is_absolute()):
        raise CleanupUnverified("invalid retained profile path")
    info = accounts.info(identity["name"])
    if info is None:
        if identity["state"] != "cleaned" or value["state"] != "cleaned":
            raise CleanupUnverified("the retained profile owner is unavailable")
    elif (identity["state"] != "created" or info.get("sid") != identity["sid"]
          or info.get("comment") != identity["marker"]):
        raise CleanupUnverified("the retained profile owner changed")
    sid = identity["sid"]
    if profiles.loaded(sid):
        raise CleanupUnverified("the owned profile is still loaded")
    current = profiles.path(sid)
    if current is not None and (value["state"] != "created" or current != recorded):
        raise CleanupUnverified("the retained profile path changed or was not recorded")
    if current is None:
        if recorded is not None and Path(recorded).exists():
            raise CleanupUnverified("unregistered profile files remain; reconciliation required")
        if value["state"] == "cleaned":
            return
    else:
        if any(part.is_symlink() or getattr(part, "is_junction", lambda: False)()
               for part in (Path(recorded), *Path(recorded).parents)):
            raise CleanupUnverified("the retained profile path was redirected")
        # Windows owns deletion by SID; never recursively remove a path taken
        # from the journal. A durability/conflict error propagates without retry.
        profiles.delete(sid)
        if profiles.loaded(sid) or profiles.path(sid) is not None or Path(recorded).exists():
            raise CleanupUnverified("owned Windows profile cleanup is unverified")
    value["state"] = "cleaned"
    write_config_json(profile_manifest, value)

# HASHI-Fingerprint: hfp_308817e97d1a40df8e5e6c994aa77e36
