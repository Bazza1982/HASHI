"""Opt-in, disposable Windows AppContainer capability probe.

This is a local platform experiment, not the HASHI safety service or an L2
acceptance test. It requires a specifically identified non-administrator test
account, runs a trusted Windows binary without secrets, checks its actual token,
and deletes only the randomly named AppContainer profile that it created.
"""
from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import uuid
from ctypes import wintypes

if os.name != "nt":
    raise SystemExit("This opt-in probe requires native Windows.")

DWORD = wintypes.DWORD
HANDLE = wintypes.HANDLE
LPVOID = ctypes.c_void_p
SIZE_T = ctypes.c_size_t
BOOL = wintypes.BOOL
KERNEL = ctypes.WinDLL("kernel32", use_last_error=True)
USERENV = ctypes.WinDLL("userenv", use_last_error=True)
ADVAPI = ctypes.WinDLL("advapi32", use_last_error=True)


class SECURITY_ATTRIBUTES(ctypes.Structure):
    _fields_ = [
        ("nLength", DWORD),
        ("lpSecurityDescriptor", LPVOID),
        ("bInheritHandle", BOOL),
    ]


class SECURITY_CAPABILITIES(ctypes.Structure):
    _fields_ = [
        ("AppContainerSid", LPVOID),
        ("Capabilities", LPVOID),
        ("CapabilityCount", DWORD),
        ("Reserved", DWORD),
    ]


class STARTUPINFOW(ctypes.Structure):
    _fields_ = [
        ("cb", DWORD), ("lpReserved", wintypes.LPWSTR),
        ("lpDesktop", wintypes.LPWSTR), ("lpTitle", wintypes.LPWSTR),
        ("dwX", DWORD), ("dwY", DWORD), ("dwXSize", DWORD), ("dwYSize", DWORD),
        ("dwXCountChars", DWORD), ("dwYCountChars", DWORD),
        ("dwFillAttribute", DWORD), ("dwFlags", DWORD),
        ("wShowWindow", wintypes.WORD), ("cbReserved2", wintypes.WORD),
        ("lpReserved2", ctypes.POINTER(ctypes.c_ubyte)),
        ("hStdInput", HANDLE), ("hStdOutput", HANDLE), ("hStdError", HANDLE),
    ]


class STARTUPINFOEXW(ctypes.Structure):
    _fields_ = [("StartupInfo", STARTUPINFOW), ("lpAttributeList", LPVOID)]


class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("hProcess", HANDLE), ("hThread", HANDLE),
        ("dwProcessId", DWORD), ("dwThreadId", DWORD),
    ]


class TOKEN_APPCONTAINER_INFORMATION(ctypes.Structure):
    _fields_ = [("TokenAppContainer", LPVOID)]


class SID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Sid", LPVOID), ("Attributes", DWORD)]


class TOKEN_GROUPS(ctypes.Structure):
    _fields_ = [("GroupCount", DWORD), ("Groups", SID_AND_ATTRIBUTES * 1)]


USERENV.CreateAppContainerProfile.argtypes = [
    wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR,
    LPVOID, DWORD, ctypes.POINTER(LPVOID),
]
USERENV.CreateAppContainerProfile.restype = ctypes.c_long
USERENV.DeleteAppContainerProfile.argtypes = [wintypes.LPCWSTR]
USERENV.DeleteAppContainerProfile.restype = ctypes.c_long
ADVAPI.FreeSid.argtypes = [LPVOID]
ADVAPI.FreeSid.restype = LPVOID
ADVAPI.OpenProcessToken.argtypes = [HANDLE, DWORD, ctypes.POINTER(HANDLE)]
ADVAPI.OpenProcessToken.restype = BOOL
ADVAPI.GetTokenInformation.argtypes = [
    HANDLE, DWORD, LPVOID, DWORD, ctypes.POINTER(DWORD),
]
ADVAPI.GetTokenInformation.restype = BOOL
ADVAPI.EqualSid.argtypes = [LPVOID, LPVOID]
ADVAPI.EqualSid.restype = BOOL
ADVAPI.ConvertSidToStringSidW.argtypes = [LPVOID, ctypes.POINTER(wintypes.LPWSTR)]
ADVAPI.ConvertSidToStringSidW.restype = BOOL
ADVAPI.ConvertStringSidToSidW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(LPVOID)]
ADVAPI.ConvertStringSidToSidW.restype = BOOL
ADVAPI.GetUserNameW.argtypes = [wintypes.LPWSTR, ctypes.POINTER(DWORD)]
ADVAPI.GetUserNameW.restype = BOOL
KERNEL.GetCurrentProcess.argtypes = []
KERNEL.GetCurrentProcess.restype = HANDLE
KERNEL.LocalFree.argtypes = [LPVOID]
KERNEL.LocalFree.restype = LPVOID
KERNEL.InitializeProcThreadAttributeList.argtypes = [
    LPVOID, DWORD, DWORD, ctypes.POINTER(SIZE_T),
]
KERNEL.InitializeProcThreadAttributeList.restype = BOOL
KERNEL.UpdateProcThreadAttribute.argtypes = [
    LPVOID, DWORD, SIZE_T, LPVOID, SIZE_T, LPVOID, ctypes.POINTER(SIZE_T),
]
KERNEL.UpdateProcThreadAttribute.restype = BOOL
KERNEL.DeleteProcThreadAttributeList.argtypes = [LPVOID]
KERNEL.DeleteProcThreadAttributeList.restype = None
KERNEL.CreateFileW.argtypes = [
    wintypes.LPCWSTR, DWORD, DWORD, ctypes.POINTER(SECURITY_ATTRIBUTES),
    DWORD, DWORD, HANDLE,
]
KERNEL.CreateFileW.restype = HANDLE
KERNEL.CreateProcessW.argtypes = [
    wintypes.LPCWSTR, wintypes.LPWSTR, LPVOID, LPVOID, BOOL, DWORD,
    LPVOID, wintypes.LPCWSTR, ctypes.POINTER(STARTUPINFOW),
    ctypes.POINTER(PROCESS_INFORMATION),
]
KERNEL.CreateProcessW.restype = BOOL
KERNEL.ResumeThread.argtypes = [HANDLE]
KERNEL.ResumeThread.restype = DWORD
KERNEL.WaitForSingleObject.argtypes = [HANDLE, DWORD]
KERNEL.WaitForSingleObject.restype = DWORD
KERNEL.GetExitCodeProcess.argtypes = [HANDLE, ctypes.POINTER(DWORD)]
KERNEL.GetExitCodeProcess.restype = BOOL
KERNEL.TerminateProcess.argtypes = [HANDLE, wintypes.UINT]
KERNEL.TerminateProcess.restype = BOOL
KERNEL.CloseHandle.argtypes = [HANDLE]
KERNEL.CloseHandle.restype = BOOL


def _checked(ok: object, operation: str) -> None:
    if not ok:
        raise OSError(ctypes.get_last_error(), operation)


def _token_data(token: HANDLE, category: int) -> ctypes.Array:
    size = DWORD()
    KERNEL.SetLastError(0)
    ADVAPI.GetTokenInformation(token, category, None, 0, ctypes.byref(size))
    if not size.value:
        raise OSError(ctypes.get_last_error(), "GetTokenInformation size")
    buffer = ctypes.create_string_buffer(size.value)
    _checked(
        ADVAPI.GetTokenInformation(token, category, buffer, size, ctypes.byref(size)),
        "GetTokenInformation",
    )
    return buffer


def _sid_text(sid: LPVOID) -> str:
    value = wintypes.LPWSTR()
    _checked(ADVAPI.ConvertSidToStringSidW(sid, ctypes.byref(value)), "ConvertSidToStringSidW")
    try:
        return value.value
    finally:
        KERNEL.LocalFree(ctypes.cast(value, LPVOID))


def _require_disposable_account(expected_sid: str) -> None:
    if not isinstance(expected_sid, str) or not expected_sid.startswith("S-1-5-21-"):
        raise PermissionError("a local disposable test account SID is required")
    token = HANDLE()
    admin_sid = LPVOID()
    _checked(ADVAPI.OpenProcessToken(KERNEL.GetCurrentProcess(), 0x0008, ctypes.byref(token)),
             "OpenProcessToken current process")
    try:
        user_info = _token_data(token, 1)
        actual_sid = ctypes.cast(user_info, ctypes.POINTER(SID_AND_ATTRIBUTES)).contents.Sid
        if _sid_text(actual_sid) != expected_sid:
            raise PermissionError("current account differs from the registered test SID")
        elevation = _token_data(token, 20)
        if ctypes.cast(elevation, ctypes.POINTER(DWORD)).contents.value:
            raise PermissionError("the disposable test account must not be elevated")
        _checked(
            ADVAPI.ConvertStringSidToSidW("S-1-5-32-544", ctypes.byref(admin_sid)),
            "ConvertStringSidToSidW administrators",
        )
        groups = _token_data(token, 2)
        count = ctypes.cast(groups, ctypes.POINTER(TOKEN_GROUPS)).contents.GroupCount
        entries = ctypes.cast(
            ctypes.addressof(groups) + TOKEN_GROUPS.Groups.offset,
            ctypes.POINTER(SID_AND_ATTRIBUTES),
        )
        if any(ADVAPI.EqualSid(entries[index].Sid, admin_sid) for index in range(count)):
            raise PermissionError("the disposable test account must not belong to Administrators")
        username = ctypes.create_unicode_buffer(257)
        size = DWORD(len(username))
        _checked(ADVAPI.GetUserNameW(username, ctypes.byref(size)), "GetUserNameW")
        if not re.fullmatch(r"HashiProbe_[0-9a-f]{8}", username.value):
            raise PermissionError("a uniquely named disposable test account is required")
    finally:
        if admin_sid:
            KERNEL.LocalFree(admin_sid)
        if token:
            KERNEL.CloseHandle(token)


def _environment() -> dict[str, str]:
    # These path values are needed by Windows profile creation. No credential,
    # application configuration, agent environment, or unrelated handles pass.
    return {
        key: os.environ[key]
        for key in ("SystemRoot", "WINDIR", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "TEMP", "TMP")
        if key in os.environ
    }


def _run_restricted(sid: LPVOID, executable: Path, arguments: list[str]) -> dict:
    process = PROCESS_INFORMATION()
    token = HANDLE()
    null_handle = None
    attributes = None
    attributes_initialized = False
    process_done = False
    try:
        size = SIZE_T()
        KERNEL.InitializeProcThreadAttributeList(None, 2, 0, ctypes.byref(size))
        if not size.value:
            raise OSError(ctypes.get_last_error(), "attribute-list size")
        attributes = ctypes.create_string_buffer(size.value)
        _checked(
            KERNEL.InitializeProcThreadAttributeList(attributes, 2, 0, ctypes.byref(size)),
            "InitializeProcThreadAttributeList",
        )
        attributes_initialized = True
        caps = SECURITY_CAPABILITIES(sid, None, 0, 0)
        _checked(
            KERNEL.UpdateProcThreadAttribute(
                attributes, 0, 0x00020009, ctypes.byref(caps),
                ctypes.sizeof(caps), None, None,
            ),
            "set AppContainer security capabilities",
        )
        sec = SECURITY_ATTRIBUTES(ctypes.sizeof(SECURITY_ATTRIBUTES), None, True)
        null_handle = KERNEL.CreateFileW(
            "NUL", 0xC0000000, 3, ctypes.byref(sec), 3, 0, None
        )
        if null_handle == ctypes.c_void_p(-1).value:
            raise OSError(ctypes.get_last_error(), "open NUL")
        handles = (HANDLE * 1)(null_handle)
        _checked(
            KERNEL.UpdateProcThreadAttribute(
                attributes, 0, 0x00020002, ctypes.byref(handles),
                ctypes.sizeof(handles), None, None,
            ),
            "allow only NUL inherited handle",
        )
        startup = STARTUPINFOEXW()
        startup.StartupInfo.cb = ctypes.sizeof(startup)
        startup.StartupInfo.dwFlags = 0x00000100  # STARTF_USESTDHANDLES
        startup.StartupInfo.hStdInput = null_handle
        startup.StartupInfo.hStdOutput = null_handle
        startup.StartupInfo.hStdError = null_handle
        startup.lpAttributeList = ctypes.cast(attributes, LPVOID)
        command = ctypes.create_unicode_buffer(
            subprocess.list2cmdline([str(executable), *arguments])
        )
        values = [f"{key}={value}" for key, value in _environment().items()]
        environment = ctypes.create_unicode_buffer("\0".join(values) + "\0\0")
        flags = 0x00080000 | 0x00000004 | 0x00000400 | 0x08000000
        _checked(
            KERNEL.CreateProcessW(
                str(executable), command, None, None, True, flags, environment,
                str(executable.parent), ctypes.byref(startup.StartupInfo), ctypes.byref(process),
            ),
            "CreateProcessW suspended",
        )
        _checked(ADVAPI.OpenProcessToken(process.hProcess, 0x0008, ctypes.byref(token)), "OpenProcessToken")
        appcontainer = _token_data(token, 29)
        is_container = bool(ctypes.cast(appcontainer, ctypes.POINTER(DWORD)).contents.value)
        app_sid = _token_data(token, 31)
        actual = ctypes.cast(app_sid, ctypes.POINTER(TOKEN_APPCONTAINER_INFORMATION)).contents.TokenAppContainer
        matches = bool(ADVAPI.EqualSid(sid, actual))
        cap_buffer = _token_data(token, 30)
        capability_count = ctypes.cast(cap_buffer, ctypes.POINTER(DWORD)).contents.value
        if not is_container or not matches or capability_count:
            raise RuntimeError("suspended process token differs from the restricted test profile")
        if KERNEL.ResumeThread(process.hThread) == 0xFFFFFFFF:
            raise OSError(ctypes.get_last_error(), "ResumeThread")
        wait = KERNEL.WaitForSingleObject(process.hProcess, 15_000)
        if wait != 0:
            raise RuntimeError(f"probe process did not exit normally (wait={wait})")
        process_done = True
        exit_code = DWORD()
        _checked(KERNEL.GetExitCodeProcess(process.hProcess, ctypes.byref(exit_code)), "GetExitCodeProcess")
        return {
            "token_is_appcontainer": is_container,
            "token_sid_matches_profile": matches,
            "capability_count": capability_count,
            "process_exit_code": exit_code.value,
        }
    finally:
        cleanup_error = None
        if process.hProcess and not process_done:
            if KERNEL.WaitForSingleObject(process.hProcess, 0) != 0:
                if not KERNEL.TerminateProcess(process.hProcess, 1):
                    cleanup_error = "could not terminate the disposable test process"
                if KERNEL.WaitForSingleObject(process.hProcess, 5_000) != 0:
                    cleanup_error = "disposable test process did not stop"
        if token:
            KERNEL.CloseHandle(token)
        if process.hThread:
            KERNEL.CloseHandle(process.hThread)
        if process.hProcess:
            KERNEL.CloseHandle(process.hProcess)
        if attributes_initialized:
            KERNEL.DeleteProcThreadAttributeList(attributes)
        if null_handle and null_handle != ctypes.c_void_p(-1).value:
            KERNEL.CloseHandle(null_handle)
        if cleanup_error:
            raise RuntimeError(cleanup_error)


def probe(expected_test_sid: str) -> dict:
    _require_disposable_account(expected_test_sid)
    profile = "HashiPermProbe_" + uuid.uuid4().hex
    sid = LPVOID()
    result = {"profile_created": False, "profile_deleted": False}
    status = USERENV.CreateAppContainerProfile(
        profile, "HASHI temporary platform probe", "Disposable capability test",
        None, 0, ctypes.byref(sid),
    )
    if status != 0:
        raise OSError(status & 0xFFFFFFFF, "CreateAppContainerProfile")
    result["profile_created"] = True
    try:
        system_root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
        icacls = system_root / "System32" / "icacls.exe"
        findstr = system_root / "System32" / "findstr.exe"
        with tempfile.TemporaryDirectory(prefix="hashi-permission-probe-") as directory:
            root = Path(directory)
            allowed = root / "allowed"
            protected = root / "protected"
            allowed.mkdir()
            protected.mkdir()
            good_file = allowed / "test.txt"
            private_file = protected / "test.txt"
            good_file.write_text("probe-sentinel\n", encoding="ascii")
            private_file.write_text("probe-sentinel\n", encoding="ascii")
            subject = "*" + _sid_text(sid)
            for target, rights in ((root, "(RX)"), (allowed, "(RX)"), (good_file, "(R)")):
                subprocess.run(
                    [str(icacls), str(target), "/grant", subject + ":" + rights],
                    check=True, timeout=10, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    env=_environment(),
                )
            # Verify that the trusted parent and this exact Windows binary can
            # read both fixtures before checking the restricted token.
            for target in (good_file, private_file):
                subprocess.run(
                    [str(findstr), "/L", "probe-sentinel", str(target)],
                    check=True, timeout=10, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    env=_environment(),
                )
            result["parent_read_both_fixtures"] = True
            result["allowed"] = _run_restricted(
                sid, findstr, ["/L", "probe-sentinel", str(good_file)]
            )
            result["protected"] = _run_restricted(
                sid, findstr, ["/L", "probe-sentinel", str(private_file)]
            )
            # Change only the two fixture ACLs, then retry the identical file
            # with the same AppContainer identity to confirm the control.
            for target, rights in ((protected, "(RX)"), (private_file, "(R)")):
                subprocess.run(
                    [str(icacls), str(target), "/grant", subject + ":" + rights],
                    check=True, timeout=10, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    env=_environment(),
                )
            result["protected_after_grant"] = _run_restricted(
                sid, findstr, ["/L", "probe-sentinel", str(private_file)]
            )
    finally:
        cleanup = USERENV.DeleteAppContainerProfile(profile)
        result["profile_deleted"] = cleanup == 0
        ADVAPI.FreeSid(sid)
        if cleanup != 0:
            raise OSError(cleanup & 0xFFFFFFFF, "DeleteAppContainerProfile")
    return result


if __name__ == "__main__":
    if len(sys.argv) != 4 or sys.argv[1:3] != [
        "--run-disposable-probe", "--expected-test-user-sid"
    ]:
        raise SystemExit("The opt-in probe requires an explicitly registered disposable account SID.")
    observation = probe(sys.argv[3])
    print(json.dumps(observation, sort_keys=True))
    if (observation.get("allowed", {}).get("process_exit_code") != 0
            or observation.get("protected", {}).get("process_exit_code") not in {1, 2}
            or observation.get("protected_after_grant", {}).get("process_exit_code") != 0):
        raise SystemExit(1)
