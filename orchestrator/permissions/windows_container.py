"""Task AppContainer profiles and private scratch ACLs; no privilege changes."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
from pathlib import Path
from uuid import uuid4


def protect_task_scratch(path, *, runner_sid, appcontainer_sid):
    if os.name != "nt" or not Path(path).is_dir():
        raise OSError("a native owned task scratch directory is required")
    security = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    pointer = wintypes.LPVOID
    security.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(pointer), ctypes.POINTER(wintypes.ULONG),
    ]
    security.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
    security.GetSecurityDescriptorDacl.argtypes = [
        pointer, ctypes.POINTER(wintypes.BOOL), ctypes.POINTER(pointer), ctypes.POINTER(wintypes.BOOL),
    ]
    security.GetSecurityDescriptorDacl.restype = wintypes.BOOL
    security.SetNamedSecurityInfoW.argtypes = [
        wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD, pointer, pointer, pointer, pointer,
    ]
    security.SetNamedSecurityInfoW.restype = wintypes.DWORD
    kernel.LocalFree.argtypes, kernel.LocalFree.restype = [pointer], pointer
    # The task receives Modify on its own generated data, not WRITE_DAC,
    # WRITE_OWNER, another task's scratch, or any service private directory.
    sddl = (f"D:P(A;OICI;FA;;;{runner_sid})(A;OICI;0x1301bf;;;{appcontainer_sid})"
            "(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)")
    descriptor = pointer()
    if not security.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, ctypes.byref(descriptor), None):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        present, defaulted, acl = wintypes.BOOL(), wintypes.BOOL(), pointer()
        if not security.GetSecurityDescriptorDacl(
            descriptor, ctypes.byref(present), ctypes.byref(acl), ctypes.byref(defaulted),
        ) or not present or not acl:
            raise PermissionError("task scratch DACL is incomplete")
        result = security.SetNamedSecurityInfoW(str(path), 1, 0x80000004, None, None, acl, None)
        if result:
            raise ctypes.WinError(result)
    finally:
        kernel.LocalFree(descriptor)


class TaskContainerProfile:
    def __init__(self):
        if os.name != "nt":
            raise OSError("native AppContainer profiles are required")
        userenv = ctypes.WinDLL("userenv", use_last_error=True)
        security = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        userenv.CreateAppContainerProfile.argtypes = [
            wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPVOID,
            wintypes.DWORD, ctypes.POINTER(wintypes.LPVOID),
        ]
        userenv.CreateAppContainerProfile.restype = ctypes.c_long
        userenv.DeleteAppContainerProfile.argtypes = [wintypes.LPCWSTR]
        userenv.DeleteAppContainerProfile.restype = ctypes.c_long
        security.ConvertSidToStringSidW.argtypes = [wintypes.LPVOID, ctypes.POINTER(wintypes.LPWSTR)]
        security.ConvertSidToStringSidW.restype = wintypes.BOOL
        kernel.LocalFree.argtypes, kernel.LocalFree.restype = [wintypes.LPVOID], wintypes.LPVOID
        security.FreeSid.argtypes, security.FreeSid.restype = [wintypes.LPVOID], wintypes.LPVOID
        self.name, self._api, self._closed = "HASHI.Task." + uuid4().hex, userenv, False
        pointer = wintypes.LPVOID()
        status = userenv.CreateAppContainerProfile(self.name, self.name, self.name, None, 0, ctypes.byref(pointer))
        if status < 0 or not pointer:
            raise OSError(status & 0xFFFFFFFF, "task AppContainer profile could not be created")
        value = wintypes.LPWSTR()
        try:
            if not security.ConvertSidToStringSidW(pointer, ctypes.byref(value)):
                raise ctypes.WinError(ctypes.get_last_error())
            self.sid = value.value
        except BaseException:
            userenv.DeleteAppContainerProfile(self.name)
            raise
        finally:
            if value:
                kernel.LocalFree(value)
            security.FreeSid(pointer)

    def close(self):
        if not self._closed:
            status = self._api.DeleteAppContainerProfile(self.name)
            if status < 0:
                raise OSError(status & 0xFFFFFFFF, "task AppContainer profile cleanup is unverified")
            self._closed = True
