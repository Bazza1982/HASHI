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

        try:
            with winreg.OpenKey(winreg.HKEY_USERS, sid):
                return True
        except FileNotFoundError:
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
