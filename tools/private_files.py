"""Platform adaptation for owner-only local credential files."""
from __future__ import annotations
import os
import csv
import ctypes
from pathlib import Path
import subprocess


def protect_private_file(path: Path) -> None:
    if os.name != 'nt':
        path.chmod(0o700 if path.is_dir() else 0o600)
        return
    # Replace the DACL atomically: removing inheritance alone leaves any
    # pre-existing explicit readers in place. chmod only changes read-only flags.
    from ctypes import wintypes
    system = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32'
    result = subprocess.run([str(system/'whoami.exe'), '/user', '/fo', 'csv', '/nh'],capture_output=True,text=True,
                            check=True,creationflags=subprocess.CREATE_NO_WINDOW)
    rows = list(csv.reader(result.stdout.strip().splitlines()))
    sid = rows[0][-1] if len(rows) == 1 and len(rows[0]) == 2 else ''
    if not sid.startswith('S-1-') or any(c not in 'S-0123456789' for c in sid):
        raise OSError('Unable to identify the credential file owner')
    inheritance = 'OICI' if path.is_dir() else ''
    sddl = 'D:P' + ''.join(f'(A;{inheritance};FA;;;{account})'
                          for account in (sid, 'SY', 'BA'))
    security = ctypes.WinDLL('advapi32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    pointer = ctypes.c_void_p
    security.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(pointer), ctypes.POINTER(wintypes.ULONG)]
    security.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
    security.GetSecurityDescriptorDacl.argtypes = [pointer, ctypes.POINTER(wintypes.BOOL),
                                                   ctypes.POINTER(pointer), ctypes.POINTER(wintypes.BOOL)]
    security.GetSecurityDescriptorDacl.restype = wintypes.BOOL
    security.SetNamedSecurityInfoW.argtypes = [wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD,
                                               pointer, pointer, pointer, pointer]
    security.SetNamedSecurityInfoW.restype = wintypes.DWORD
    kernel.LocalFree.argtypes = [pointer]
    kernel.LocalFree.restype = pointer
    descriptor = pointer()
    if not security.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, ctypes.byref(descriptor), None):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        present, defaulted, acl = wintypes.BOOL(), wintypes.BOOL(), pointer()
        if not security.GetSecurityDescriptorDacl(descriptor, ctypes.byref(present), ctypes.byref(acl), ctypes.byref(defaulted)):
            raise ctypes.WinError(ctypes.get_last_error())
        # SE_FILE_OBJECT, DACL_SECURITY_INFORMATION | PROTECTED_DACL_SECURITY_INFORMATION.
        error = security.SetNamedSecurityInfoW(str(path), 1, 0x80000004, None, None, acl, None)
        if error:
            raise ctypes.WinError(error)
    finally:
        kernel.LocalFree(descriptor)
