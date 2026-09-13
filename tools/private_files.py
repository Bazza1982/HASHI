"""Platform adaptation for owner-only local credential files."""
from __future__ import annotations
import argparse
import os
import csv
import ctypes
import re
from collections.abc import Iterable, Sequence
from pathlib import Path
import subprocess


_WINDOWS_SID = re.compile(r"S-1(?:-\d+)+", re.IGNORECASE)


def _normalize_windows_sid(value: str) -> str:
    sid = str(value or "").strip().upper()
    if not _WINDOWS_SID.fullmatch(sid):
        raise ValueError(f"Invalid Windows SID: {value!r}")
    return sid


def protect_private_file(
    path: Path,
    *,
    additional_full_control_sids: Iterable[str] = (),
) -> None:
    """Replace broad access with the writer and explicit runtime principals.

    A deployment may create an instance as an elevated service account while
    its long-running process uses a Limited user token.  Such setup code must
    pass that runtime user's SID explicitly; otherwise the writer remains the
    sole non-system principal, as before.
    """

    if os.name != 'nt':
        if tuple(additional_full_control_sids):
            raise ValueError("Additional Windows principals require Windows")
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
    try:
        sid = _normalize_windows_sid(sid)
    except ValueError:
        raise OSError('Unable to identify the credential file owner')
    permitted_sids = [sid]
    for candidate in additional_full_control_sids:
        normalized = _normalize_windows_sid(candidate)
        if normalized not in permitted_sids:
            permitted_sids.append(normalized)
    inheritance = 'OICI' if path.is_dir() else ''
    sddl = 'D:P' + ''.join(f'(A;{inheritance};FA;;;{account})'
                          for account in (*permitted_sids, 'SY', 'BA'))
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


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Apply HASHI's private-file ACL to one exact path.",
    )
    parser.add_argument(
        "--allow-full-control-sid",
        action="append",
        default=[],
        help="Additional intended Windows runtime principal SID.",
    )
    parser.add_argument("path", type=Path)
    args = parser.parse_args(argv)
    protect_private_file(
        args.path,
        additional_full_control_sids=args.allow_full_control_sid,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
