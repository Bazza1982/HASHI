"""Plan A launcher: run a command as the active console session user.

Background (HASHI3, updated 2026-10-03)
--------------------------------------
HASHI can run either as LocalSystem or already inside the interactive user's
console session.  Google Antigravity CLI (agy) stores its login credential in
that interactive user's Windows Credential Manager, protected by DPAPI.  A
LocalSystem process cannot use that credential.

This launcher does NOT copy, decrypt, export, or re-home any credential.  It
borrows the primary token of the user logged on to the active console
session and starts the target executable in that user's context, by:

  1. using the current identity only when its session and SID exactly match
     the active console user's session and SID,
  2. otherwise using ``WTSGetActiveConsoleSessionId`` + ``WTSQueryUserToken``
     + ``CreateProcessAsUser`` from the privileged service context, or
  3. when CreateProcessAsUser is rejected by the hosting context, via a
     triggerless, manually-run Windows scheduled task with the
     interactive-token logon type.  The Task Scheduler service starts the
     child with the user's live logon token; no password is ever involved.

Contract
--------
    python agy_user_session_launcher.py [--cwd DIR] -- EXE [ARG ...]

* The launcher waits for the child and exits with the child's exit code.
* Diagnostics go to stderr only (stdout belongs to the child).

Implementation notes
--------------------
* Worker hosts may lack valid standard handles (multiprocessing spawn
  inherits only an explicit handle list); invalid handles are replaced with
  inheritable NUL handles before CreateProcessAsUserW.
* The scheduled-task fallback stores argv and, only for the explicit
  ``--input-format stream-json`` contract, stdin in one ACL-restricted UUID
  directory.  The per-invocation runner owns a Windows Job for its child,
  observes a launcher heartbeat, and kills that Job plus its own artifacts if
  the launcher disappears.  The task XML has no time/event trigger and can
  only run through the launcher's explicit ``schtasks /Run`` call.

Exit codes
----------
    0   child's exit code (propagated)
    2   no active console session (user is logged off / no interactive logon)
    3   WTSQueryUserToken failed
    4   CreateEnvironmentBlock failed
    5   CreateProcessAsUser failed (and interactive-task fallback failed)
    6   interactive-task fallback timed out
    7   interactive-task fallback failed to start
    10  unsupported platform (requires Windows)
    11  usage error
"""

from __future__ import annotations

import ctypes
import html
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from ctypes import wintypes
from pathlib import Path

LAUNCHER_SCRIPT_PATH = Path(__file__).resolve()

EXIT_NO_CONSOLE_SESSION = 2
EXIT_QUERY_TOKEN_FAILED = 3
EXIT_ENV_BLOCK_FAILED = 4
EXIT_CREATE_PROCESS_FAILED = 5
EXIT_TASK_TIMEOUT = 6
EXIT_TASK_START_FAILED = 7
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
WTS_USER_NAME = 5
WTS_DOMAIN_NAME = 7
GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
OPEN_EXISTING = 3
FILE_ATTRIBUTE_NORMAL = 0x00000080

DESKTOP_WINSTA_DEFAULT = "winsta0\\default"

TASK_POLL_TIMEOUT_SEC = 7200.0
TASK_POLL_INTERVAL_SEC = 1.0
TASK_NAME_PREFIX = "RikaAgy-"
TASK_NAME_PATTERN = re.compile(r"^RikaAgy-[0-9a-f]{16}$")
SID_PATTERN = re.compile(r"^S-\d+(?:-\d+)+$")
RUNNER_POLL_INTERVAL_SEC = 0.25
RUNNER_HEARTBEAT_TIMEOUT_SEC = 10.0
RUNNER_ACK_TIMEOUT_SEC = 30.0
MAX_STDIN_BYTES = 64 * 1024 * 1024
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000


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


class TOKEN_USER(ctypes.Structure):
    _fields_ = [("User", ctypes.c_void_p)]  # SID_AND_ATTRIBUTES*


def _bind():
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    wtsapi32 = ctypes.WinDLL("wtsapi32", use_last_error=True)
    userenv = ctypes.WinDLL("userenv", use_last_error=True)

    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.GetCurrentProcessId.restype = wintypes.DWORD
    kernel32.ProcessIdToSessionId.argtypes = [
        wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)
    ]
    kernel32.ProcessIdToSessionId.restype = wintypes.BOOL
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
    wtsapi32.WTSQuerySessionInformationW.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.c_int,
        ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(wintypes.DWORD),
    ]
    wtsapi32.WTSQuerySessionInformationW.restype = wintypes.BOOL
    wtsapi32.WTSFreeMemory.argtypes = [ctypes.c_void_p]
    wtsapi32.WTSFreeMemory.restype = None
    userenv.CreateEnvironmentBlock.argtypes = [
        ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.BOOL
    ]
    userenv.CreateEnvironmentBlock.restype = wintypes.BOOL
    userenv.DestroyEnvironmentBlock.argtypes = [ctypes.c_void_p]
    userenv.DestroyEnvironmentBlock.restype = wintypes.BOOL
    kernel32.GetStdHandle.argtypes = [wintypes.DWORD]
    kernel32.GetStdHandle.restype = wintypes.HANDLE
    kernel32.GetHandleInformation.argtypes = [
        wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)
    ]
    kernel32.GetHandleInformation.restype = wintypes.BOOL
    kernel32.SetHandleInformation.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD
    ]
    kernel32.SetHandleInformation.restype = wintypes.BOOL
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    kernel32.CreateFileW.restype = wintypes.HANDLE
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
    advapi32.GetTokenInformation.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.GetTokenInformation.restype = wintypes.BOOL
    advapi32.LookupAccountSidW.argtypes = [
        wintypes.LPCWSTR, ctypes.c_void_p, wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD), wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.LookupAccountSidW.restype = wintypes.BOOL
    advapi32.LookupAccountNameW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.DWORD),
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.LookupAccountNameW.restype = wintypes.BOOL
    advapi32.ConvertSidToStringSidW.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)
    ]
    advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    return kernel32, advapi32, wtsapi32, userenv


def _fail(message: str, exit_code: int, winerror: int | None = None) -> int:
    if winerror is None:
        winerror = ctypes.get_last_error()
    print(f"agy-launcher: {message} (winerror={winerror})", file=sys.stderr, flush=True)
    return exit_code


def _adjust_privileges(advapi32, kernel32, names: tuple) -> bool:
    token = wintypes.HANDLE()
    ok = advapi32.OpenProcessToken(
        kernel32.GetCurrentProcess(),
        TOKEN_ADJUST_PRIVILEGES | TOKEN_QUERY,
        ctypes.byref(token),
    )
    if not ok:
        return False
    try:
        for name in names:
            luid = LUID()
            if not advapi32.LookupPrivilegeValueW(None, name, ctypes.byref(luid)):
                return False
            tp = TOKEN_PRIVILEGES()
            tp.PrivilegeCount = 1
            tp.Privileges[0].Luid = luid
            tp.Privileges[0].Attributes = SE_PRIVILEGE_ENABLED
            if not advapi32.AdjustTokenPrivileges(
                token, False, ctypes.byref(tp), 0, None, None
            ):
                return False
            if ctypes.get_last_error() == ERROR_NOT_ALL_ASSIGNED:
                print(
                    f"agy-launcher: privilege {name} not held by caller token",
                    file=sys.stderr,
                    flush=True,
                )
                return False
        return True
    finally:
        kernel32.CloseHandle(token)


def _token_user_name(advapi32, kernel32, user_token) -> str:
    """Resolve the user token's account name as 'domain\\user'."""
    need = wintypes.DWORD()
    advapi32.GetTokenInformation(user_token, 1, None, 0, ctypes.byref(need))
    if not need.value:
        return ""
    buf = ctypes.create_string_buffer(need.value)
    if not advapi32.GetTokenInformation(user_token, 1, buf, need.value, ctypes.byref(need)):
        return ""
    tu = ctypes.cast(buf, ctypes.POINTER(TOKEN_USER)).contents
    name_len = wintypes.DWORD(256)
    domain_len = wintypes.DWORD(256)
    name_buf = ctypes.create_unicode_buffer(name_len.value)
    domain_buf = ctypes.create_unicode_buffer(domain_len.value)
    use = wintypes.DWORD()
    if not advapi32.LookupAccountSidW(
        None, tu.User, name_buf, ctypes.byref(name_len),
        domain_buf, ctypes.byref(domain_len), ctypes.byref(use),
    ):
        return ""
    if domain_buf.value:
        return f"{domain_buf.value}\\{name_buf.value}"
    return name_buf.value


def _sid_to_string(advapi32, kernel32, sid) -> str:
    value = wintypes.LPWSTR()
    if not advapi32.ConvertSidToStringSidW(sid, ctypes.byref(value)):
        return ""
    try:
        return str(value.value or "")
    finally:
        kernel32.LocalFree(ctypes.cast(value, ctypes.c_void_p))


def _token_user_sid(advapi32, kernel32, user_token) -> str:
    need = wintypes.DWORD()
    advapi32.GetTokenInformation(user_token, 1, None, 0, ctypes.byref(need))
    if not need.value:
        return ""
    buf = ctypes.create_string_buffer(need.value)
    if not advapi32.GetTokenInformation(
        user_token, 1, buf, need.value, ctypes.byref(need)
    ):
        return ""
    token_user = ctypes.cast(buf, ctypes.POINTER(TOKEN_USER)).contents
    return _sid_to_string(advapi32, kernel32, token_user.User)


def _account_sid(advapi32, kernel32, account_name: str) -> str:
    if not account_name:
        return ""
    sid_size = wintypes.DWORD()
    domain_size = wintypes.DWORD()
    sid_type = wintypes.DWORD()
    advapi32.LookupAccountNameW(
        None,
        account_name,
        None,
        ctypes.byref(sid_size),
        None,
        ctypes.byref(domain_size),
        ctypes.byref(sid_type),
    )
    if not sid_size.value:
        return ""
    sid_buffer = ctypes.create_string_buffer(sid_size.value)
    domain_buffer = ctypes.create_unicode_buffer(max(domain_size.value, 1))
    if not advapi32.LookupAccountNameW(
        None,
        account_name,
        sid_buffer,
        ctypes.byref(sid_size),
        domain_buffer,
        ctypes.byref(domain_size),
        ctypes.byref(sid_type),
    ):
        return ""
    return _sid_to_string(advapi32, kernel32, sid_buffer)


def _wts_session_text(wtsapi32, session_id: int, info_class: int) -> str:
    value = wintypes.LPWSTR()
    byte_count = wintypes.DWORD()
    if not wtsapi32.WTSQuerySessionInformationW(
        None,
        session_id,
        info_class,
        ctypes.byref(value),
        ctypes.byref(byte_count),
    ):
        return ""
    try:
        return str(value.value or "")
    finally:
        wtsapi32.WTSFreeMemory(ctypes.cast(value, ctypes.c_void_p))


def _current_console_identity(kernel32, advapi32, wtsapi32, session_id: int):
    """Return ``(account, sid)`` only for the exact active console identity."""

    process_session = wintypes.DWORD()
    if not kernel32.ProcessIdToSessionId(
        kernel32.GetCurrentProcessId(), ctypes.byref(process_session)
    ):
        return None
    if process_session.value != session_id:
        return None

    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(
        kernel32.GetCurrentProcess(), TOKEN_QUERY, ctypes.byref(token)
    ):
        return None
    try:
        current_sid = _token_user_sid(advapi32, kernel32, token)
    finally:
        kernel32.CloseHandle(token)
    if not current_sid:
        return None

    user_name = _wts_session_text(wtsapi32, session_id, WTS_USER_NAME)
    domain_name = _wts_session_text(wtsapi32, session_id, WTS_DOMAIN_NAME)
    if not user_name:
        return None
    account_name = f"{domain_name}\\{user_name}" if domain_name else user_name
    console_sid = _account_sid(advapi32, kernel32, account_name)
    if not console_sid or console_sid != current_sid:
        return None
    return account_name, console_sid


def _sanitize_std_handles(kernel32) -> tuple:
    handles = []
    extras = []
    for std_id in (STD_INPUT_HANDLE, STD_OUTPUT_HANDLE, STD_ERROR_HANDLE):
        handle = kernel32.GetStdHandle(std_id)
        flags = wintypes.DWORD()
        valid = bool(handle) and bool(
            kernel32.GetHandleInformation(handle, ctypes.byref(flags))
        )
        if not valid:
            nul = kernel32.CreateFileW(
                "NUL",
                GENERIC_READ | GENERIC_WRITE,
                FILE_SHARE_READ | FILE_SHARE_WRITE,
                None,
                OPEN_EXISTING,
                FILE_ATTRIBUTE_NORMAL,
                None,
            )
            if not nul:
                nul = None
            else:
                kernel32.SetHandleInformation(
                    nul, HANDLE_FLAG_INHERIT, HANDLE_FLAG_INHERIT
                )
                extras.append(nul)
            handle = nul
        else:
            kernel32.SetHandleInformation(
                handle, HANDLE_FLAG_INHERIT, HANDLE_FLAG_INHERIT
            )
        handles.append(handle)
    return handles[0], handles[1], handles[2], extras


def _spawn_as_user(kernel32, userenv, user_token, exe, cmdline, cwd):
    env_block = ctypes.c_void_p()
    if not userenv.CreateEnvironmentBlock(ctypes.byref(env_block), user_token, False):
        return None, EXIT_ENV_BLOCK_FAILED, ctypes.get_last_error()
    if not env_block.value:
        return None, EXIT_ENV_BLOCK_FAILED, ctypes.get_last_error()

    h_stdin, h_stdout, h_stderr, extras = _sanitize_std_handles(kernel32)

    si = STARTUPINFOW()
    si.cb = ctypes.sizeof(STARTUPINFOW)
    si.dwFlags = STARTF_USESTDHANDLES
    si.lpDesktop = DESKTOP_WINSTA_DEFAULT
    si.hStdInput = h_stdin or None
    si.hStdOutput = h_stdout or None
    si.hStdError = h_stderr or None

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
    for extra in extras:
        kernel32.CloseHandle(extra)
    if not created:
        return None, EXIT_CREATE_PROCESS_FAILED, winerror
    return pi, 0, 0


class _WindowsChildJob:
    """Own one Popen process tree by process handle, never by a reused PID."""

    def __init__(self, process: subprocess.Popen):
        self._handle = None
        if os.name != "nt":
            return
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        class _BasicLimitInformation(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class _IoCounters(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong),
            ]

        class _ExtendedLimitInformation(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", _BasicLimitInformation),
                ("IoInfo", _IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            return
        limits = _ExtendedLimitInformation()
        limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        configured = kernel32.SetInformationJobObject(
            handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)
        )
        assigned = configured and kernel32.AssignProcessToJobObject(
            handle, wintypes.HANDLE(process._handle)
        )
        if not assigned:
            kernel32.CloseHandle(handle)
            return
        self._kernel32 = kernel32
        self._handle = handle

    def close(self) -> None:
        if self._handle is not None:
            self._kernel32.CloseHandle(self._handle)
            self._handle = None

    @property
    def active(self) -> bool:
        return self._handle is not None


def _spawn_as_current_identity(exe: str, args: list[str], cwd: str | None) -> int:
    """Spawn under the already-verified console identity and own its process tree."""

    try:
        child = subprocess.Popen([exe, *args], cwd=cwd)
    except OSError as exc:
        return _fail(
            f"could not start {exe} as the current console identity",
            EXIT_CREATE_PROCESS_FAILED,
            getattr(exc, "winerror", None),
        )
    job = _WindowsChildJob(child)
    if os.name == "nt" and not job.active:
        if child.poll() is None:
            child.terminate()
        try:
            child.wait(timeout=10)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=10)
        return _fail(
            "could not bind current-identity child to its Job",
            EXIT_CREATE_PROCESS_FAILED,
        )
    try:
        return int(child.wait())
    finally:
        job.close()


def _write_private_bytes(path: Path, data: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(descriptor)
    if os.name != "nt":
        path.chmod(0o600)


def _write_private_text(path: Path, value: str, *, encoding: str = "utf-8") -> None:
    _write_private_bytes(path, value.encode(encoding))


def _restrict_task_directory(path: Path, user_name: str) -> bool:
    if os.name != "nt":
        path.chmod(0o700)
        return True
    try:
        result = subprocess.run(
            [
                "icacls",
                str(path),
                "/inheritance:r",
                "/grant:r",
                "*S-1-5-18:(OI)(CI)F",
                f"{user_name}:(OI)(CI)F",
            ],
            capture_output=True,
            timeout=120,
        )
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _stream_json_stdin_requested(args: list[str]) -> bool:
    for index, value in enumerate(args):
        if value == "--input-format" and index + 1 < len(args):
            return args[index + 1].strip().casefold() == "stream-json"
        if value.strip().casefold() == "--input-format=stream-json":
            return True
    return False


def _read_scheduled_stdin(args: list[str]) -> bytes | None:
    if not _stream_json_stdin_requested(args):
        return None
    if getattr(sys.stdin, "isatty", lambda: False)():
        return None
    data = sys.stdin.buffer.read(MAX_STDIN_BYTES + 1)
    if len(data) > MAX_STDIN_BYTES:
        raise ValueError("stream-json stdin exceeds the launcher limit")
    return data


def _manual_task_xml(
    user_name: str, user_sid: str, command: str, arguments: str
) -> str:
    if not SID_PATTERN.fullmatch(user_sid):
        raise ValueError("invalid console user SID")
    escaped_user = html.escape(user_name, quote=True)
    escaped_command = html.escape(command, quote=True)
    escaped_arguments = html.escape(arguments, quote=True)
    security_descriptor = html.escape(
        f"D:P(A;;FA;;;SY)(A;;FA;;;BA)(A;;FA;;;{user_sid})", quote=True
    )
    return f'''<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Author>HASHI</Author>
    <SecurityDescriptor>{security_descriptor}</SecurityDescriptor>
  </RegistrationInfo>
  <Triggers />
  <Principals>
    <Principal id="Author">
      <UserId>{escaped_user}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>false</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>true</Hidden>
    <ExecutionTimeLimit>PT2H</ExecutionTimeLimit>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{escaped_command}</Command>
      <Arguments>{escaped_arguments}</Arguments>
    </Exec>
  </Actions>
</Task>
'''


def _task_temp_root() -> Path:
    if os.name == "nt":
        return Path(os.environ.get("WINDIR", r"C:\Windows")) / "Temp"
    return Path(os.environ.get("TMPDIR", "/tmp"))


def _valid_task_identity(work_dir: Path, task_name: str) -> bool:
    if not TASK_NAME_PATTERN.fullmatch(str(task_name or "")):
        return False
    try:
        return (
            Path(work_dir).name == task_name
            and Path(work_dir).resolve().parent == _task_temp_root().resolve()
        )
    except OSError:
        return False


def _delete_scheduled_task(task_name: str) -> bool:
    if not TASK_NAME_PATTERN.fullmatch(str(task_name or "")):
        return False
    for attempt in range(8):
        try:
            deleted = subprocess.run(
                ["schtasks", "/Delete", "/F", "/TN", task_name],
                capture_output=True,
                timeout=120,
            )
            if deleted.returncode == 0:
                return True
            queried = subprocess.run(
                ["schtasks", "/Query", "/TN", task_name],
                capture_output=True,
                timeout=120,
            )
            if queried.returncode != 0:
                return True
        except (OSError, subprocess.SubprocessError):
            pass
        if attempt < 7:
            time.sleep(0.1)
    return False


def _remove_task_directory(path: Path) -> None:
    """Remove only this invocation directory, tolerating handle-release lag."""

    for _attempt in range(40):
        shutil.rmtree(path, ignore_errors=True)
        if not path.exists():
            return
        time.sleep(0.1)
    shutil.rmtree(path, ignore_errors=True)


def _heartbeat_fresh(path: Path) -> bool:
    try:
        return time.time() - path.stat().st_mtime <= RUNNER_HEARTBEAT_TIMEOUT_SEC
    except OSError:
        return False


def _run_task_payload(work_dir: Path, task_name: str) -> int:
    """Run exactly one task payload and self-clean if its launcher disappears."""

    work_dir = Path(work_dir)
    if not _valid_task_identity(work_dir, task_name):
        return EXIT_TASK_START_FAILED
    child = None
    job = None
    orphaned = False
    input_handle = None
    try:
        payload = json.loads((work_dir / "args.json").read_text(encoding="utf-8"))
        args = payload.get("args") or []
        cwd = payload.get("cwd") or None
        if not isinstance(args, list) or not args or not all(
            isinstance(value, str) for value in args
        ):
            return EXIT_TASK_START_FAILED
        input_path = work_dir / "input.bin"
        input_handle = input_path.open("rb") if input_path.is_file() else subprocess.DEVNULL
        output_path = work_dir / "out.txt"
        output_descriptor = os.open(
            output_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
        )
        error_path = work_dir / "err.txt"
        error_descriptor = os.open(
            error_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
        )
        with (
            os.fdopen(output_descriptor, "wb") as output,
            os.fdopen(error_descriptor, "wb") as error,
        ):
            child = subprocess.Popen(
                args,
                stdin=input_handle,
                stdout=output,
                stderr=error,
                cwd=cwd,
            )
            job = _WindowsChildJob(child)
            if os.name == "nt" and not job.active:
                child.terminate()
                child.wait(timeout=10)
                return EXIT_TASK_START_FAILED
            while child.poll() is None:
                if not _heartbeat_fresh(work_dir / "heartbeat"):
                    orphaned = True
                    job.close()
                    job = None
                    if child.poll() is None:
                        child.terminate()
                    try:
                        child.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        child.kill()
                        child.wait(timeout=10)
                    break
                time.sleep(RUNNER_POLL_INTERVAL_SEC)
        return_code = int(child.returncode if child.returncode is not None else 1)
        if orphaned:
            return return_code
        _write_private_text(work_dir / "code.txt", str(return_code))
        deadline = time.monotonic() + RUNNER_ACK_TIMEOUT_SEC
        while time.monotonic() < deadline:
            if (work_dir / "ack").exists():
                break
            if not _heartbeat_fresh(work_dir / "heartbeat"):
                break
            time.sleep(RUNNER_POLL_INTERVAL_SEC)
        return return_code
    except Exception:
        return EXIT_TASK_START_FAILED
    finally:
        if job is not None:
            job.close()
        if input_handle not in (None, subprocess.DEVNULL):
            input_handle.close()
        _delete_scheduled_task(task_name)
        _remove_task_directory(work_dir)


def _spawn_via_interactive_task(
    user_name: str,
    user_sid: str,
    exe: str,
    args: list,
    cwd: str | None,
) -> int:
    """Fallback: run through one triggerless, manually-started interactive task."""
    task_name = TASK_NAME_PREFIX + uuid.uuid4().hex[:16]
    tmp = _task_temp_root() / task_name
    tmp.mkdir(parents=True, exist_ok=True)
    try:
        if not _restrict_task_directory(tmp, user_name):
            print(
                "agy-launcher: could not restrict scheduled-task artifacts",
                file=sys.stderr,
                flush=True,
            )
            return EXIT_TASK_START_FAILED
        try:
            stdin_bytes = _read_scheduled_stdin(args)
        except ValueError as exc:
            print(f"agy-launcher: {exc}", file=sys.stderr, flush=True)
            return EXIT_TASK_START_FAILED
        _write_private_text(
            tmp / "args.json",
            json.dumps({"args": [exe, *args], "cwd": str(cwd) if cwd else None}),
        )
        if stdin_bytes is not None:
            _write_private_bytes(tmp / "input.bin", stdin_bytes)
        _write_private_text(tmp / "heartbeat", str(time.time()))
        runner_arguments = subprocess.list2cmdline(
            [str(LAUNCHER_SCRIPT_PATH), "--task-runner", str(tmp), task_name]
        )
        task_xml = _manual_task_xml(
            user_name, user_sid, sys.executable, runner_arguments
        )
        task_xml_path = tmp / "task.xml"
        _write_private_text(task_xml_path, task_xml, encoding="utf-16")
        create = subprocess.run(
            [
                "schtasks", "/Create", "/F", "/TN", task_name,
                "/XML", str(task_xml_path),
            ],
            capture_output=True,
            timeout=120,
        )
        if create.returncode != 0:
            print(
                "agy-launcher: schtasks /Create failed: "
                + create.stderr.decode(errors="replace")[:400],
                file=sys.stderr,
                flush=True,
            )
            return EXIT_TASK_START_FAILED
        run = subprocess.run(
            ["schtasks", "/Run", "/TN", task_name],
            capture_output=True,
            timeout=120,
        )
        if run.returncode != 0:
            print(
                "agy-launcher: schtasks /Run failed: "
                + run.stderr.decode(errors="replace")[:400],
                file=sys.stderr,
                flush=True,
            )
            _delete_scheduled_task(task_name)
            return EXIT_TASK_START_FAILED
        print(
            f"agy-launcher: interactive-token task {task_name} started "
            f"for user {user_name}",
            file=sys.stderr,
            flush=True,
        )
        deadline = time.monotonic() + TASK_POLL_TIMEOUT_SEC
        while time.monotonic() < deadline:
            code_file = tmp / "code.txt"
            if code_file.exists():
                time.sleep(0.2)  # let the output file flush settle
                break
            try:
                (tmp / "heartbeat").touch()
            except OSError:
                break
            time.sleep(TASK_POLL_INTERVAL_SEC)
        if not code_file.exists():
            print(
                "agy-launcher: interactive-token task did not finish in time",
                file=sys.stderr,
                flush=True,
            )
            return EXIT_TASK_TIMEOUT
        out_file = tmp / "out.txt"
        if out_file.exists():
            data = out_file.read_bytes()
            if data:
                sys.stdout.buffer.write(data)
                sys.stdout.buffer.flush()
        err_file = tmp / "err.txt"
        if err_file.exists():
            data = err_file.read_bytes()
            if data:
                sys.stderr.buffer.write(data)
                sys.stderr.buffer.flush()
        try:
            exit_code = int(code_file.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            exit_code = 0
        try:
            _write_private_text(tmp / "ack", "read")
        except OSError:
            pass
        if exit_code != 0:
            print(
                f"agy-launcher: task child failed (exit_code={exit_code})",
                file=sys.stderr,
                flush=True,
            )
        return exit_code
    finally:
        _delete_scheduled_task(task_name)
        _remove_task_directory(tmp)


def main(argv: list[str]) -> int:
    if len(argv) == 3 and argv[0] == "--task-runner":
        return _run_task_payload(Path(argv[1]), argv[2])
    if os.name != "nt":
        return _fail("requires native Windows", EXIT_NOT_WINDOWS)

    cwd = None
    force_task = False
    args = list(argv)
    if args and args[0] == "--force-task":
        force_task = True
        args = args[1:]
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

    session_id = kernel32.WTSGetActiveConsoleSessionId()
    if session_id == INVALID_SESSION:
        return _fail("no active console session", EXIT_NO_CONSOLE_SESSION)
    print(f"agy-launcher: active console session={session_id}", file=sys.stderr, flush=True)

    current_identity = _current_console_identity(
        kernel32, advapi32, wtsapi32, session_id
    )
    if current_identity is not None:
        user_name, user_sid = current_identity
        print(
            "agy-launcher: current process identity exactly matches the "
            "active console session",
            file=sys.stderr,
            flush=True,
        )
        if force_task:
            print(
                "agy-launcher: --force-task requested; using a manual "
                "interactive-token task",
                file=sys.stderr,
                flush=True,
            )
            return _spawn_via_interactive_task(
                user_name, user_sid, exe, args[1:], cwd
            )
        return _spawn_as_current_identity(exe, args[1:], cwd)

    print(
        "agy-launcher: current identity is not the active console identity; "
        "using the privileged WTS token path",
        file=sys.stderr,
        flush=True,
    )
    if not _adjust_privileges(advapi32, kernel32, ("SeTcbPrivilege",)):
        return _fail("could not enable SeTcbPrivilege", EXIT_QUERY_TOKEN_FAILED)
    _adjust_privileges(
        advapi32,
        kernel32,
        ("SeAssignPrimaryTokenPrivilege", "SeIncreaseQuotaPrivilege"),
    )

    user_token = wintypes.HANDLE()
    if not wtsapi32.WTSQueryUserToken(session_id, ctypes.byref(user_token)):
        return _fail(
            f"WTSQueryUserToken failed for session {session_id}",
            EXIT_QUERY_TOKEN_FAILED,
    )
    user_name = _token_user_name(advapi32, kernel32, user_token)
    user_sid = _token_user_sid(advapi32, kernel32, user_token)

    pi, exit_code, winerror = (None, 0, 0)
    if force_task:
        print(
            "agy-launcher: --force-task requested; skipping CreateProcessAsUser",
            file=sys.stderr,
            flush=True,
        )
    else:
        pi, exit_code, winerror = _spawn_as_user(
            kernel32, userenv, user_token, exe, cmdline, cwd
        )
    kernel32.CloseHandle(user_token)
    if pi is not None:
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

    # Fallback: interactive-token scheduled task (Plan A equivalent).
    if force_task:
        print(
            "agy-launcher: falling back to interactive-token scheduled task "
            "(--force-task)",
            file=sys.stderr,
            flush=True,
        )
    else:
        print(
            f"agy-launcher: CreateProcessAsUserW failed (winerror={winerror}); "
            "falling back to interactive-token scheduled task",
            file=sys.stderr,
            flush=True,
        )
    if not user_name or not user_sid:
        print(
            "agy-launcher: could not resolve the session user identity; "
            "interactive-token fallback unavailable",
            file=sys.stderr,
            flush=True,
        )
        return _fail(f"CreateProcessAsUserW failed for {exe}", exit_code, winerror)
    return _spawn_via_interactive_task(user_name, user_sid, exe, args[1:], cwd)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
