"""Read-only native qualification of service identities and protected files."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
from pathlib import Path

from orchestrator.permissions.windows_ipc import current_process_user_sid


def _api():
    if os.name != "nt":
        raise OSError("native Windows security qualification is required")
    security = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    pointer = wintypes.LPVOID
    security.GetNamedSecurityInfoW.argtypes = [
        wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD,
        ctypes.POINTER(pointer), ctypes.POINTER(pointer), ctypes.POINTER(pointer),
        ctypes.POINTER(pointer), ctypes.POINTER(pointer),
    ]
    security.GetNamedSecurityInfoW.restype = wintypes.DWORD
    security.GetAce.argtypes, security.GetAce.restype = [pointer, wintypes.DWORD, ctypes.POINTER(pointer)], wintypes.BOOL
    security.ConvertSidToStringSidW.argtypes = [pointer, ctypes.POINTER(wintypes.LPWSTR)]
    security.ConvertSidToStringSidW.restype = wintypes.BOOL
    security.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    security.OpenProcessToken.restype = wintypes.BOOL
    security.GetTokenInformation.argtypes = [
        wintypes.HANDLE, ctypes.c_int, pointer, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
    ]
    security.GetTokenInformation.restype = wintypes.BOOL
    kernel.GetCurrentProcess.argtypes, kernel.GetCurrentProcess.restype = [], wintypes.HANDLE
    kernel.CloseHandle.argtypes, kernel.CloseHandle.restype = [wintypes.HANDLE], wintypes.BOOL
    kernel.LocalFree.argtypes, kernel.LocalFree.restype = [pointer], pointer
    return kernel, security


def _sid(pointer, kernel, security):
    value = wintypes.LPWSTR()
    if not security.ConvertSidToStringSidW(pointer, ctypes.byref(value)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return value.value
    finally:
        kernel.LocalFree(value)


def require_private_path(path, *, trusted_sids, read_only_public=False):
    """No ACL is changed, and an untrusted file owner is never accepted."""
    path = Path(path)
    if (not path.is_absolute() or len(path.drive) != 2 or path.drive[1] != ":"
            or not path.exists() or any(parent.is_symlink() or parent.is_junction()
                                        for parent in (path, *path.parents))):
        raise PermissionError("a local protected installation path is required")
    kernel, security = _api()
    owner, dacl, descriptor = wintypes.LPVOID(), wintypes.LPVOID(), wintypes.LPVOID()
    error = security.GetNamedSecurityInfoW(
        str(path), 1, 0x1 | 0x4, ctypes.byref(owner), None, ctypes.byref(dacl),
        None, ctypes.byref(descriptor),
    )
    if error:
        raise ctypes.WinError(error)
    allowed = frozenset(trusted_sids) | {"S-1-5-18", "S-1-5-32-544"}
    try:
        if not dacl or not owner or _sid(owner, kernel, security) not in allowed:
            raise PermissionError("installation owner or DACL is not protected")
        # ACL header: revision, reserved, size, ACE count, reserved.
        count = ctypes.cast(dacl, ctypes.POINTER(ctypes.c_ushort))[2]
        if count > 4096:
            raise PermissionError("installation ACL is invalid")
        dangerous = 0x10000000 | 0x40000000 | 0x000D0000 | 0x00000156
        if not read_only_public:
            dangerous |= 0x80000000 | 0x000000A9
        for index in range(count):
            pointer = wintypes.LPVOID()
            if not security.GetAce(dacl, index, ctypes.byref(pointer)):
                raise ctypes.WinError(ctypes.get_last_error())
            header = ctypes.cast(pointer, ctypes.POINTER(ctypes.c_ubyte))
            kind, flags = header[0], header[1]
            if flags & 0x08:  # INHERIT_ONLY does not grant this object access.
                continue
            if kind == 1:  # A deny cannot increase authority.
                continue
            if kind != 0:
                raise PermissionError("unqualified object/callback access entry")
            size = ctypes.cast(pointer, ctypes.POINTER(ctypes.c_ushort))[1]
            if size < 12:
                raise PermissionError("invalid installation access entry")
            mask = ctypes.cast(pointer.value + 4, ctypes.POINTER(wintypes.DWORD))[0]
            trustee = _sid(pointer.value + 8, kernel, security)
            if trustee not in allowed and mask & dangerous:
                raise PermissionError("installation grants untrusted data/control access")
        return True
    finally:
        kernel.LocalFree(descriptor)


def _require_service_identity(expected_sid, *, excluded_sids=(), nonadmin=True):
    """Observe actual account groups, including filtered administrator tokens."""
    kernel, security = _api()
    sid = current_process_user_sid()
    if sid != expected_sid or sid in set(excluded_sids) | {"S-1-5-18", "S-1-5-19", "S-1-5-20"}:
        raise PermissionError("the configured independent service account is not active")
    token = wintypes.HANDLE()
    if not security.OpenProcessToken(kernel.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        def info(kind):
            size = wintypes.DWORD()
            security.GetTokenInformation(token, kind, None, 0, ctypes.byref(size))
            if not 0 < size.value <= 1024 * 1024:
                raise PermissionError("service token information is unavailable")
            buffer = ctypes.create_string_buffer(size.value)
            if not security.GetTokenInformation(token, kind, buffer, size.value, ctypes.byref(size)):
                raise ctypes.WinError(ctypes.get_last_error())
            return buffer
        elevation = info(20)
        if ctypes.cast(elevation, ctypes.POINTER(wintypes.DWORD))[0]:
            raise PermissionError("elevated service identities are not qualified")
        groups = info(2)
        count = ctypes.cast(groups, ctypes.POINTER(wintypes.DWORD))[0]
        if count > 4096:
            raise PermissionError("service token group layout is invalid")

        class Entry(ctypes.Structure):
            _fields_ = [("sid", wintypes.LPVOID), ("attributes", wintypes.DWORD)]

        class Layout(ctypes.Structure):
            _fields_ = [("count", wintypes.DWORD), ("entries", Entry * 1)]

        offset = Layout.entries.offset
        if offset + count * ctypes.sizeof(Entry) > ctypes.sizeof(groups):
            raise PermissionError("service token group layout is incomplete")
        entries = ctypes.cast(ctypes.addressof(groups) + offset, ctypes.POINTER(Entry))
        administrator = any(_sid(entries[i].sid, kernel, security) == "S-1-5-32-544" for i in range(count))
        if nonadmin and administrator:
            raise PermissionError("an administrator account is not a nonadmin service identity")
        return {"user_sid": sid, "elevated": False, "administrator_member": administrator}
    finally:
        kernel.CloseHandle(token)



def require_nonadmin_identity(expected_sid, *, excluded_sids=()):
    return _require_service_identity(expected_sid, excluded_sids=excluded_sids, nonadmin=True)


def require_l1_identity(expected_sid):
    """L1 may share a normal filtered user account; elevated execution is refused."""
    return _require_service_identity(expected_sid, nonadmin=False)


class InstallationLock:
    """Kernel-exclusive service ownership, acquired before opening the ledger."""

    def __init__(self, private_root):
        kernel, _ = _api()
        kernel.CreateFileW.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
            wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
        ]
        kernel.CreateFileW.restype = wintypes.HANDLE
        self._kernel = kernel
        self._handle = kernel.CreateFileW(
            str(Path(private_root) / "service.lock"), 0x80000000 | 0x40000000,
            0, None, 4, 0x80, None,
        )
        if self._handle == ctypes.c_void_p(-1).value:
            self._handle = None
            raise PermissionError("another service already owns this permission installation")

    def close(self):
        if self._handle is not None:
            if not self._kernel.CloseHandle(self._handle):
                raise ctypes.WinError(ctypes.get_last_error())
            self._handle = None
