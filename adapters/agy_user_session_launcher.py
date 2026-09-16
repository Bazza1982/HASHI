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
session and starts the target executable in that user's context, either:

  1. directly with ``WTSGetActiveConsoleSessionId`` + ``WTSQueryUserToken``
     + ``CreateProcessAsUser`` (primary path), or
  2. when CreateProcessAsUser is rejected by the hosting context, via a
     one-shot Windows scheduled task with the interactive-token logon type
     (``schtasks /RU <user> /IT``).  The Task Scheduler service starts the
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
* The scheduled-task fallback writes argv to an args.json file and executes
  a small runner.py under the user token, relaying stdout/stderr through
  files back to this process.  If this launcher is killed before the task
  finishes, the task keeps running until agy's own --print-timeout.

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
import json
import os
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


RUNNER_TEMPLATE = '''\
import json
import subprocess
import sys
from pathlib import Path

work_dir = Path(sys.argv[1])
payload = json.loads((work_dir / "args.json").read_text(encoding="utf-8"))
if isinstance(payload, dict):
    args = payload.get("args") or []
    cwd = payload.get("cwd") or None
else:
    args = payload
    cwd = None
with open(work_dir / "out.txt", "wb") as out:
    proc = subprocess.run(
        args,
        stdout=out,
        stderr=subprocess.STDOUT,
        cwd=cwd,
    )
(work_dir / "code.txt").write_text(str(proc.returncode), encoding="utf-8")
'''


def _spawn_via_interactive_task(user_name: str, exe: str, args: list, cwd: str | None) -> int:
    """Fallback: run the command through a one-shot interactive-token task."""
    task_name = TASK_NAME_PREFIX + uuid.uuid4().hex[:16]
    tmp = Path(os.environ.get("TEMP", "C:\\Windows\\Temp")) / task_name
    tmp.mkdir(parents=True, exist_ok=True)
    try:
        (tmp / "args.json").write_text(
            json.dumps(
                {"args": [exe, *args], "cwd": str(cwd) if cwd else None}
            ),
            encoding="utf-8",
        )
        runner = tmp / "runner.py"
        runner.write_text(RUNNER_TEMPLATE, encoding="utf-8")
        task_cmd = (
            f'"{sys.executable}" "{runner}" "{tmp}"'
        )
        create = subprocess.run(
            [
                "schtasks", "/Create", "/F", "/TN", task_name,
                "/SC", "ONCE", "/ST", "23:59", "/RU", user_name, "/IT",
                "/TR", task_cmd,
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
            subprocess.run(
                ["schtasks", "/Delete", "/F", "/TN", task_name],
                capture_output=True,
                timeout=120,
            )
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
        try:
            exit_code = int(code_file.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            exit_code = 0
        if exit_code != 0:
            try:
                tail = out_file.read_bytes()[-2000:]
                if tail:
                    print(
                        "agy-launcher: task child failed; output tail:\n"
                        + tail.decode(errors="replace"),
                        file=sys.stderr,
                        flush=True,
                    )
            except OSError:
                pass
        return exit_code
    finally:
        subprocess.run(
            ["schtasks", "/Delete", "/F", "/TN", task_name],
            capture_output=True,
            timeout=120,
        )
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)


def main(argv: list[str]) -> int:
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

    if not _adjust_privileges(advapi32, kernel32, ("SeTcbPrivilege",)):
        return _fail("could not enable SeTcbPrivilege", EXIT_QUERY_TOKEN_FAILED)
    _adjust_privileges(
        advapi32,
        kernel32,
        ("SeAssignPrimaryTokenPrivilege", "SeIncreaseQuotaPrivilege"),
    )

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
    user_name = _token_user_name(advapi32, kernel32, user_token)

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
    if not user_name:
        print(
            "agy-launcher: could not resolve the session user account name; "
            "interactive-token fallback unavailable",
            file=sys.stderr,
            flush=True,
        )
        return _fail(f"CreateProcessAsUserW failed for {exe}", exit_code, winerror)
    return _spawn_via_interactive_task(user_name, exe, args[1:], cwd)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
