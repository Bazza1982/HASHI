"""Private recoverable Job names; host/AppContainer identities receive no handle.

Names are fixed-prefix random reservations in a protected service journal.
Recovery verifies the kernel owner before it may terminate a named object.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import re

_PREFIX = "Global\\HASHI.Permissions.Job."


def private_job_name(nonce):
    if not isinstance(nonce, str) or re.fullmatch(r"[0-9a-f]{32}", nonce) is None:
        raise ValueError("a private random Job reservation is required")
    return _PREFIX + nonce


def _validate(name, owner_sid):
    if (not isinstance(name, str) or not name.startswith(_PREFIX)
            or re.fullmatch(r"[0-9a-f]{32}", name[len(_PREFIX):]) is None
            or not isinstance(owner_sid, str) or re.fullmatch(r"S-1-(?:\d+-)*\d+", owner_sid) is None):
        raise ValueError("an exact private Job name and installed owner SID are required")


def _security_api():
    security = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    pointer = wintypes.LPVOID
    security.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(pointer), ctypes.POINTER(wintypes.ULONG),
    ]
    security.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
    security.GetSecurityInfo.argtypes = [
        wintypes.HANDLE, ctypes.c_int, wintypes.DWORD, ctypes.POINTER(pointer),
        ctypes.POINTER(pointer), ctypes.POINTER(pointer), ctypes.POINTER(pointer), ctypes.POINTER(pointer),
    ]
    security.GetSecurityInfo.restype = wintypes.DWORD
    security.ConvertSidToStringSidW.argtypes = [pointer, ctypes.POINTER(wintypes.LPWSTR)]
    security.ConvertSidToStringSidW.restype = wintypes.BOOL
    kernel.LocalFree.argtypes, kernel.LocalFree.restype = [pointer], pointer
    return kernel, security


def create_private_job(kernel, name, owner_sid):
    _validate(name, owner_sid)
    memory, security = _security_api()
    descriptor = wintypes.LPVOID()
    # Explicit owner and protected DACL; no broad application-package/user
    # trustee, no inherited handle and no new Windows privilege is requested.
    sddl = f"O:{owner_sid}D:P(A;;GA;;;{owner_sid})(A;;GA;;;SY)(A;;GA;;;BA)"
    if not security.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, ctypes.byref(descriptor), None):
        raise ctypes.WinError(ctypes.get_last_error())

    class Attributes(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("descriptor", wintypes.LPVOID), ("inherit", wintypes.BOOL)]

    try:
        attributes = Attributes(ctypes.sizeof(Attributes), descriptor, False)
        ctypes.set_last_error(0)
        handle = kernel.CreateJobObjectW(ctypes.byref(attributes), name)
        error = ctypes.get_last_error()
        if not handle:
            raise ctypes.WinError(error)
        if error == 183:
            if not kernel.CloseHandle(handle):
                raise OSError("colliding Job handle cleanup is unverified")
            raise PermissionError("the private Job name was already reserved")
        return handle
    finally:
        memory.LocalFree(descriptor)


def open_private_job(kernel, name, owner_sid):
    _validate(name, owner_sid)
    kernel.OpenJobObjectW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.OpenJobObjectW.restype = wintypes.HANDLE
    handle = kernel.OpenJobObjectW(0x0004 | 0x0008 | 0x00020000, False, name)
    if not handle:
        error = ctypes.get_last_error()
        if error == 2:
            # A Job is destroyed only after its last handle and every associated
            # process are gone. Its reservation existed before CreateProcess.
            return None
        raise ctypes.WinError(error)
    memory, security = _security_api()
    owner, descriptor = wintypes.LPVOID(), wintypes.LPVOID()
    value = wintypes.LPWSTR()
    try:
        error = security.GetSecurityInfo(handle, 6, 1, ctypes.byref(owner), None, None, None,
                                         ctypes.byref(descriptor))
        if error:
            raise ctypes.WinError(error)
        if not owner or not security.ConvertSidToStringSidW(owner, ctypes.byref(value)):
            raise ctypes.WinError(ctypes.get_last_error())
        if value.value != owner_sid:
            raise PermissionError("the named Job belongs to another kernel owner")
        return handle
    except BaseException:
        if not kernel.CloseHandle(handle):
            raise OSError("unqualified Job handle cleanup is unverified") from None
        raise
    finally:
        if value:
            memory.LocalFree(value)
        if descriptor:
            memory.LocalFree(descriptor)
