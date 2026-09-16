"""Plan A launcher: run a command as the active console session user.

Background (HASHI3, 2026-09-16)
-------------------------------
The HASHI3 nssm service runs as LocalSystem.  Google Antigravity CLI (agy)
stores its login credential in the interactive user's Windows Credential
Manager (``LegacyGeneric:target=gemini:antigravity``), which is protected by
DPAPI in that user's security context.  A LocalSystem process cannot read it
(verified: SYSTEM ``agy models`` -> "Please sign in to view available
models" while the same call as the logged-on user succeeds).

This launcher does NOT copy, decrypt, export, or re-home any credential.  It
borrows the primary token of the user logged on to the active console
session (WTSGetActiveConsoleSessionId + WTSQueryUserToken) and starts the
target executable with CreateProcessAsUser, together with that user's
environment block.  The DPAPI boundary is untouched.

Contract
--------
    python agy_user_session_launcher.py [--cwd DIR] -- EXE [ARG ...]

* EXE is started under the console-session user token with that user's
  environment, std handles connected to the launcher's own std handles.
* The launcher waits for the child and exits with the child's exit code.
* Diagnostics go to stderr only (stdout belongs to the child).

Exit codes
----------
    0   child's exit code (propagated)
    2   no active console session (user is logged off / no interactive logon)
    3   WTSQueryUserToken failed
    4   CreateEnvironmentBlock failed
    5   CreateProcessAsUser failed
    10  unsupported platform (requires Windows)
    11  usage error
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
from ctypes import wintypes
from pathlib import Path

LAUNCHER_SCRIPT_PATH = Path(__file__).resolve()

EXIT_NO_CONSOLE_SESSION = 2
EXIT_QUERY_TOKEN_FAILED = 3
EXIT_ENV_BLOCK_FAILED = 4
EXIT_CREATE_PROCESS_FAILED = 5
EXIT_NOT_WINDOWS = 10
EXIT_USAGE = 11

TOKEN_ADJUST_PRIVILEGES = 0x0020
TOKEN_QUERY = 0x0008
SE_PRIVILEGE_ENABLED = 0x00000002
CREATE_UNICODE_ENVIRONMENT = 0x00000400
STARTF_USESTDHANDLES = 0x00000100
INFINITE = 0xFFFFFFFF
STD_INPUT_HANDLE = -10
STD_OUTPUT_HANDLE = -11
STD_ERROR_HANDLE = -12
HANDLE_FLAG_INHERIT = 0x00000001
ERROR_NOT_ALL_ASSIGNED = 1300
INVALID_SESSION = 0xFFFFFFFF

DESKTOP_WINSTA_DEFAULT = "winsta0\\default"


class STARTUPINFOW(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("lpReserved", wintypes.LPWSTR),
        ("lpDesktop", wintypes.LPWSTR),
        ("lpTitle", wintypes.LPWSTR),
        ("dwX", wintypes.DWORD),
        ("dwY", wintypes.DWORD),
        ("dwXSize", wintypes.DWORD),
        ("dwYSize", wintypes.DWORD),
        ("dwXCountChars", wintypes.DWORD),
        ("dwYCountChars", wintypes.DWORD),
        ("dwFillAttribute", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("wShowWindow", wintypes.WORD),
        ("cbReserved2", wintypes.WORD),
        ("lpReserved2", ctypes.c_void_p),
        ("hStdInput", wintypes.HANDLE),
        ("hStdOutput", wintypes.HANDLE),
        ("hStdError", wintypes.HANDLE),
    ]


class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("hProcess", wintypes.HANDLE),
        ("hThread", wintypes.HANDLE),
        ("dwProcessId", wintypes.DWORD),
        ("dwThreadId", wintypes.DWORD),
    ]


class LUID(ctypes.Structure):
    _fields_ = [("LowPart", wintypes.DWORD), ("HighPart", wintypes.LONG)]


class LUID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Luid", LUID), ("Attributes", wintypes.DWORD)]


class TOKEN_PRIVILEGES(ctypes.Structure):
    _fields_ = [
        ("PrivilegeCount", wintypes.DWORD),
        ("Privileges", LUID_AND_ATTRIBUTES * 1),
    ]


def _bind():
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    wtsapi32 = ctypes.WinDLL("wtsapi32", use_last_error=True)
    userenv = ctypes.WinDLL("userenv", use_last_error=True)

    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    advapi32.OpenProcessToken.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)
    ]
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.LookupPrivilegeValueW.argtypes = [
        wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.POINTER(LUID)
    ]
    advapi32.LookupPrivilegeValueW.restype = wintypes.BOOL
    advapi32.AdjustTokenPrivileges.argtypes = [
        wintypes.HANDLE, wintypes.BOOL, ctypes.POINTER(TOKEN_PRIVILEGES),
        wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p,
    ]
    advapi32.AdjustTokenPrivileges.restype = wintypes.BOOL
    kernel32.WTSGetActiveConsoleSessionId.restype = wintypes.DWORD
    wtsapi32.WTSQueryUserToken.argtypes = [
        wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)
    ]
    wtsapi32.WTSQueryUserToken.restype = wintypes.BOOL
    userenv.CreateEnvironmentBlock.argtypes = [
        ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.BOOL
    ]
    userenv.CreateEnvironmentBlock.restype = wintypes.BOOL
    userenv.DestroyEnvironmentBlock.argtypes = [ctypes.c_void_p]
    userenv.DestroyEnvironmentBlock.restype = wintypes.BOOL
    kernel32.GetStdHandle.argtypes = [wintypes.DWORD]
    kernel32.GetStdHandle.restype = wintypes.HANDLE
    kernel32.SetHandleInformation.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD
    ]
    kernel32.SetHandleInformation.restype = wintypes.BOOL
    kernel32.CreateProcessAsUserW.argtypes = [
        wintypes.HANDLE, wintypes.LPCWSTR, wintypes.LPWSTR,
        ctypes.c_void_p, ctypes.c_void_p, wintypes.BOOL, wintypes.DWORD,
        ctypes.c_void_p, wintypes.LPCWSTR, ctypes.POINTER(STARTUPINFOW),
        ctypes.POINTER(PROCESS_INFORMATION),
    ]
    kernel32.CreateProcessAsUserW.restype = wintypes.BOOL
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.GetExitCodeProcess.argtypes = [
        wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)
    ]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    return kernel32, advapi32, wtsapi32, userenv


def _fail(message: str, exit_code: int, winerror: int | None = None) -> int:
    if winerror is None:
        winerror = ctypes.get_last_error()
    print(f"agy-launcher: {message} (winerror={winerror})", file=sys.stderr, flush=True)
    return exit_code


def _enable_tcb_privilege(advapi32, kernel32) -> bool:
    token = wintypes.HANDLE()
    ok = advapi32.OpenProcessToken(
        kernel32.GetCurrentProcess(),
        TOKEN_ADJUST_PRIVILEGES | TOKEN_QUERY,
        ctypes.byref(token),
    )
    if not ok:
        return False
    try:
        luid = LUID()
        if not advapi32.LookupPrivilegeValueW(None, "SeTcbPrivilege", ctypes.byref(luid)):
            return False
        tp = TOKEN_PRIVILEGES()
        tp.PrivilegeCount = 1
        tp.Privileges[0].Luid = luid
        tp.Privileges[0].Attributes = SE_PRIVILEGE_ENABLED
        if not advapi32.AdjustTokenPrivileges(
            token, False, ctypes.byref(tp), 0, None, None
        ):
            return False
        return ctypes.get_last_error() != ERROR_NOT_ALL_ASSIGNED
    finally:
        kernel32.CloseHandle(token)


def _spawn_as_user(kernel32, userenv, user_token, exe, cmdline, cwd):
    env_block = ctypes.c_void_p()
    if not userenv.CreateEnvironmentBlock(ctypes.byref(env_block), user_token, False):
        return None, EXIT_ENV_BLOCK_FAILED, ctypes.get_last_error()
    if not env_block.value:
        return None, EXIT_ENV_BLOCK_FAILED, ctypes.get_last_error()

    si = STARTUPINFOW()
    si.cb = ctypes.sizeof(STARTUPINFOW)
    si.dwFlags = STARTF_USESTDHANDLES
    si.lpDesktop = DESKTOP_WINSTA_DEFAULT
    si.hStdInput = kernel32.GetStdHandle(STD_INPUT_HANDLE)
    si.hStdOutput = kernel32.GetStdHandle(STD_OUTPUT_HANDLE)
    si.hStdError = kernel32.GetStdHandle(STD_ERROR_HANDLE)
    kernel32.SetHandleInformation(si.hStdInput, HANDLE_FLAG_INHERIT, HANDLE_FLAG_INHERIT)
    kernel32.SetHandleInformation(si.hStdOutput, HANDLE_FLAG_INHERIT, HANDLE_FLAG_INHERIT)
    kernel32.SetHandleInformation(si.hStdError, HANDLE_FLAG_INHERIT, HANDLE_FLAG_INHERIT)

    pi = PROCESS_INFORMATION()
    created = kernel32.CreateProcessAsUserW(
        user_token,
        exe,
        cmdline,
        None,
        None,
        True,
        CREATE_UNICODE_ENVIRONMENT,
        env_block,
        cwd,
        ctypes.byref(si),
        ctypes.byref(pi),
    )
    winerror = ctypes.get_last_error()
    if not created:
        # The interactive desktop may be unavailable; retry without an
        # explicit desktop (headless console child still gets our pipes).
        si.lpDesktop = None
        created = kernel32.CreateProcessAsUserW(
            user_token,
            exe,
            cmdline,
            None,
            None,
            True,
            CREATE_UNICODE_ENVIRONMENT,
            env_block,
            cwd,
            ctypes.byref(si),
            ctypes.byref(pi),
        )
        winerror = ctypes.get_last_error()
    userenv.DestroyEnvironmentBlock(env_block)
    if not created:
        return None, EXIT_CREATE_PROCESS_FAILED, winerror
    return pi, 0, 0


def main(argv: list[str]) -> int:
    if os.name != "nt":
        return _fail("requires native Windows", EXIT_NOT_WINDOWS)

    cwd = None
    args = list(argv)
    if args and args[0] == "--cwd":
        if len(args) < 3 or args[2] != "--":
            return _fail("usage: [--cwd DIR] -- EXE [ARG ...]", EXIT_USAGE)
        cwd = args[1]
        args = args[3:]
    elif args and args[0] == "--":
        args = args[1:]
    else:
        return _fail("usage: [--cwd DIR] -- EXE [ARG ...]", EXIT_USAGE)
    if not args:
        return _fail("usage: missing EXE after --", EXIT_USAGE)

    exe = args[0]
    cmdline = subprocess.list2cmdline(args)
    kernel32, advapi32, wtsapi32, userenv = _bind()

    if not _enable_tcb_privilege(advapi32, kernel32):
        return _fail("could not enable SeTcbPrivilege", EXIT_QUERY_TOKEN_FAILED)

    session_id = kernel32.WTSGetActiveConsoleSessionId()
    if session_id == INVALID_SESSION:
        return _fail("no active console session", EXIT_NO_CONSOLE_SESSION)
    print(f"agy-launcher: active console session={session_id}", file=sys.stderr, flush=True)

    user_token = wintypes.HANDLE()
    if not wtsapi32.WTSQueryUserToken(session_id, ctypes.byref(user_token)):
        return _fail(
            f"WTSQueryUserToken failed for session {session_id}",
            EXIT_QUERY_TOKEN_FAILED,
        )

    pi, exit_code, winerror = _spawn_as_user(
        kernel32, userenv, user_token, exe, cmdline, cwd
    )
    kernel32.CloseHandle(user_token)
    if pi is None:
        return _fail(f"CreateProcessAsUserW failed for {exe}", exit_code, winerror)

    print(
        f"agy-launcher: spawned pid={pi.dwProcessId} under console session user token",
        file=sys.stderr,
        flush=True,
    )
    kernel32.CloseHandle(pi.hThread)
    kernel32.WaitForSingleObject(pi.hProcess, INFINITE)
    child_exit = wintypes.DWORD()
    if not kernel32.GetExitCodeProcess(pi.hProcess, ctypes.byref(child_exit)):
        child_exit.value = EXIT_CREATE_PROCESS_FAILED
    kernel32.CloseHandle(pi.hProcess)
    return int(child_exit.value)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))