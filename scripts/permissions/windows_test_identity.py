"""Create and verify a disposable Windows test identity, then remove only it.

No account is created on import. The CLI is explicitly opt-in. A durable
manifest contains ownership metadata but never the generated password.
This is test infrastructure, not an installed security service.
"""
from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import re
import secrets
import uuid


_NAME = re.compile(r"HashiProbe_[0-9a-f]{8}\Z")
_ID = re.compile(r"[0-9a-f]{32}\Z")
_SID = re.compile(r"S-1-5-21-(?:[0-9]+-){3}[0-9]+\Z")
_ADMINISTRATORS = "S-1-5-32-544"


class CleanupUnverified(RuntimeError):
    """An account could not safely be identified or removed."""


def _write_manifest(path: Path, value: dict, *, exclusive: bool = False) -> None:
    text = json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n"
    if exclusive:
        with path.open("x", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        return
    candidate = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with candidate.open("x", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(candidate, path)
    finally:
        if candidate.exists():
            candidate.unlink()


def _validate_manifest(value: object) -> dict:
    if not isinstance(value, dict) or set(value) != {"version", "id", "name", "marker", "sid", "state"}:
        raise CleanupUnverified("invalid test identity manifest")
    identifier, name, marker, sid, state = (
        value["id"], value["name"], value["marker"], value["sid"], value["state"]
    )
    if (
        value["version"] != 1 or not isinstance(identifier, str) or not _ID.fullmatch(identifier)
        or not isinstance(name, str) or not _NAME.fullmatch(name)
        or name != "HashiProbe_" + identifier[:8]
        or marker != "HASHI disposable permission probe " + identifier
        or state not in {"intent", "created", "cleaned"}
        or (sid is not None and (not isinstance(sid, str) or not _SID.fullmatch(sid)))
    ):
        raise CleanupUnverified("invalid test identity ownership")
    return dict(value)


def _remove_matching(api, name: str, marker: str, sid: str) -> None:
    info = api.info(name)
    if info is None:
        return
    if info.get("comment") != marker or info.get("sid") != sid:
        raise CleanupUnverified("test account identity is not proven")
    api.delete(name)
    if api.info(name) is not None:
        raise CleanupUnverified("test account deletion is not confirmed")


def recover_identity(api, manifest: Path) -> None:
    """Remove only a successfully recorded account whose identity still matches."""
    path = Path(manifest)
    try:
        value = _validate_manifest(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, UnicodeError) as exc:
        raise CleanupUnverified("test identity manifest is unavailable") from exc
    info = api.info(value["name"])
    if value["state"] == "cleaned":
        if info is not None:
            raise CleanupUnverified("a cleaned account name has reappeared")
        return
    if info is None:
        value["state"] = "cleaned"
        _write_manifest(path, value)
        return
    if value["state"] != "created" or value["sid"] is None:
        raise CleanupUnverified("test account identity is not proven")
    _remove_matching(api, value["name"], value["marker"], value["sid"])
    value["state"] = "cleaned"
    _write_manifest(path, value)


class DisposableTestIdentity:
    """Own a uniquely named test account and its durable cleanup record."""

    def __init__(self, api, manifest: Path) -> None:
        self._api = api
        self._manifest = Path(manifest)
        identifier = uuid.uuid4().hex
        self.name = "HashiProbe_" + identifier[:8]
        self.sid: str | None = None
        self.password: str | None = None
        self._value = {
            "version": 1, "id": identifier, "name": self.name,
            "marker": "HASHI disposable permission probe " + identifier,
            "sid": None, "state": "intent",
        }
        self._created = False

    def __enter__(self):
        _write_manifest(self._manifest, self._value, exclusive=True)
        self.password = "Aa1!" + secrets.token_urlsafe(24)
        try:
            self._api.create(self.name, self.password, self._value["marker"])
            self._created = True
            info = self._api.info(self.name)
            if (not isinstance(info, dict) or info.get("comment") != self._value["marker"]
                    or not isinstance(info.get("sid"), str) or not _SID.fullmatch(info["sid"])):
                raise CleanupUnverified("created test identity could not be verified")
            self.sid = info["sid"]
            self._value["sid"] = self.sid
            self._value["state"] = "created"
            _write_manifest(self._manifest, self._value)
            if _ADMINISTRATORS in self._api.group_sids(self.name):
                raise PermissionError("test account is an administrator")
            return self
        except BaseException:
            try:
                if self._created:
                    if self.sid is None:
                        info = self._api.info(self.name)
                        if (not isinstance(info, dict) or info.get("comment") != self._value["marker"]
                                or not isinstance(info.get("sid"), str) or not _SID.fullmatch(info["sid"])):
                            raise CleanupUnverified("created account could not be safely identified")
                        self.sid = info["sid"]
                    _remove_matching(self._api, self.name, self._value["marker"], self.sid)
                    self._value["sid"] = self.sid
                    self._value["state"] = "cleaned"
                    _write_manifest(self._manifest, self._value)
                    self._created = False
                else:
                    existing = self._api.info(self.name)
                    if existing is not None and existing.get("comment") == self._value["marker"]:
                        raise CleanupUnverified("test account creation outcome is unknown")
            except CleanupUnverified:
                raise
            except Exception as cleanup_error:
                raise CleanupUnverified("test account cleanup could not be confirmed") from cleanup_error
            finally:
                self.password = None
            raise

    def __exit__(self, exc_type, exc, traceback):
        try:
            if self._created:
                recover_identity(self._api, self._manifest)
        finally:
            self.password = None


class _UserInfo1(ctypes.Structure):
    _fields_ = [
        ("name", wintypes.LPWSTR), ("password", wintypes.LPWSTR),
        ("password_age", wintypes.DWORD), ("privilege", wintypes.DWORD),
        ("home", wintypes.LPWSTR), ("comment", wintypes.LPWSTR),
        ("flags", wintypes.DWORD), ("script", wintypes.LPWSTR),
    ]


class _LocalGroup(ctypes.Structure):
    _fields_ = [("name", wintypes.LPWSTR)]


class WindowsAccounts:
    """Minimal NetAPI operations used only for the disposable test account."""

    def __init__(self) -> None:
        if os.name != "nt":
            raise RuntimeError("native Windows is required")
        self.net = ctypes.WinDLL("netapi32", use_last_error=True)
        self.adv = ctypes.WinDLL("advapi32", use_last_error=True)
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.net.NetUserAdd.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p,
                                       ctypes.POINTER(wintypes.DWORD)]
        self.net.NetUserAdd.restype = wintypes.DWORD
        self.net.NetUserGetInfo.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                                           ctypes.POINTER(ctypes.c_void_p)]
        self.net.NetUserGetInfo.restype = wintypes.DWORD
        self.net.NetUserGetLocalGroups.argtypes = [
            wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
            ctypes.POINTER(ctypes.c_void_p), wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD),
        ]
        self.net.NetUserGetLocalGroups.restype = wintypes.DWORD
        self.net.NetUserDel.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
        self.net.NetUserDel.restype = wintypes.DWORD
        self.net.NetApiBufferFree.argtypes = [ctypes.c_void_p]
        self.net.NetApiBufferFree.restype = wintypes.DWORD
        self.adv.LookupAccountNameW.argtypes = [
            wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD),
            wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD),
        ]
        self.adv.LookupAccountNameW.restype = wintypes.BOOL
        self.adv.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
        self.adv.ConvertSidToStringSidW.restype = wintypes.BOOL
        self.kernel.LocalFree.argtypes = [ctypes.c_void_p]
        self.kernel.LocalFree.restype = ctypes.c_void_p
        self.kernel.GetComputerNameW.argtypes = [
            wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD),
        ]
        self.kernel.GetComputerNameW.restype = wintypes.BOOL
        size = wintypes.DWORD(256)
        computer = ctypes.create_unicode_buffer(size.value)
        if not self.kernel.GetComputerNameW(computer, ctypes.byref(size)):
            raise OSError(ctypes.get_last_error(), "cannot determine local computer name")
        self._computer = computer.value

    def _sid(self, account: str) -> str:
        size, domain_size, kind = wintypes.DWORD(), wintypes.DWORD(), wintypes.DWORD()
        self.adv.LookupAccountNameW(None, account, None, ctypes.byref(size),
                                    None, ctypes.byref(domain_size), ctypes.byref(kind))
        if ctypes.get_last_error() != 122 or not size.value:
            raise OSError(ctypes.get_last_error(), "cannot size account SID")
        sid = ctypes.create_string_buffer(size.value)
        domain = ctypes.create_unicode_buffer(domain_size.value or 1)
        if not self.adv.LookupAccountNameW(
            None, account, sid, ctypes.byref(size), domain, ctypes.byref(domain_size), ctypes.byref(kind)
        ):
            raise OSError(ctypes.get_last_error(), "cannot resolve account SID")
        text = wintypes.LPWSTR()
        if not self.adv.ConvertSidToStringSidW(sid, ctypes.byref(text)):
            raise OSError(ctypes.get_last_error(), "cannot render account SID")
        try:
            return text.value
        finally:
            self.kernel.LocalFree(ctypes.cast(text, ctypes.c_void_p))

    def create(self, name: str, password: str, comment: str) -> None:
        user = _UserInfo1(name, password, 0, 1, None, comment, 0x201, None)
        error_field = wintypes.DWORD()
        status = self.net.NetUserAdd(None, 1, ctypes.byref(user), ctypes.byref(error_field))
        if status:
            raise OSError(status, "cannot create disposable account")

    def info(self, name: str) -> dict | None:
        buffer = ctypes.c_void_p()
        status = self.net.NetUserGetInfo(None, name, 1, ctypes.byref(buffer))
        if status == 2221:  # NERR_UserNotFound
            return None
        if status:
            raise OSError(status, "cannot inspect disposable account")
        try:
            user = ctypes.cast(buffer, ctypes.POINTER(_UserInfo1)).contents
            if user.name.casefold() != name.casefold():
                raise CleanupUnverified("account name changed")
            return {"comment": str(user.comment or ""), "sid": self._sid(self._computer + "\\" + name)}
        finally:
            self.net.NetApiBufferFree(buffer)

    def _local_group_sids(self, group: str) -> set[str]:
        # Local aliases may live under either the machine or BUILTIN authority.
        # Resolve both: a same-named local alias must not conceal Administrators.
        resolved = set()
        for authority in (self._computer, "BUILTIN"):
            try:
                resolved.add(self._sid(authority + "\\" + group))
            except OSError as exc:
                if exc.errno != 1332:  # ERROR_NONE_MAPPED
                    raise
        if not resolved:
            raise CleanupUnverified("local group SID could not be verified")
        return resolved

    def group_sids(self, name: str) -> set[str]:
        buffer = ctypes.c_void_p()
        read, total = wintypes.DWORD(), wintypes.DWORD()
        status = self.net.NetUserGetLocalGroups(
            None, name, 0, 1, ctypes.byref(buffer), 0xFFFFFFFF, ctypes.byref(read), ctypes.byref(total)
        )
        if status:
            raise OSError(status, "cannot inspect disposable account groups")
        try:
            groups = ctypes.cast(buffer, ctypes.POINTER(_LocalGroup))
            result = set()
            for index in range(read.value):
                result.update(self._local_group_sids(groups[index].name))
            return result
        finally:
            if buffer:
                self.net.NetApiBufferFree(buffer)

    def delete(self, name: str) -> None:
        status = self.net.NetUserDel(None, name)
        if status:
            raise OSError(status, "cannot delete disposable account")


def main() -> None:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--exercise-disposable-identity", action="store_true")
    mode.add_argument("--recover-manifest", action="store_true")
    parser.add_argument("--manifest", required=True, type=Path)
    args = parser.parse_args()
    if not args.manifest.is_absolute() or not args.manifest.parent.is_dir():
        raise SystemExit("an absolute manifest path in an existing directory is required")
    api = WindowsAccounts()
    if args.recover_manifest:
        recover_identity(api, args.manifest)
        print(json.dumps({"cleanup_confirmed": True}))
        return
    with DisposableTestIdentity(api, args.manifest) as identity:
        observed_sid = identity.sid
    print(json.dumps({"non_administrator": True, "sid_verified": bool(observed_sid),
                      "cleanup_confirmed": True}))


if __name__ == "__main__":
    main()
