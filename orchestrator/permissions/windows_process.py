"""Verified suspended Windows children, atomic Jobs and an explicit handle list.

The caller is a trusted supervisor running under its configured nonadmin
identity. This module never logs on another user, enables privileges, launches a
shell implicitly, or resumes an unverified business process.
"""
from __future__ import annotations

import ctypes
from dataclasses import dataclass
from hashlib import sha256
import os
from pathlib import Path
import subprocess
from ctypes import wintypes

from orchestrator.permissions.windows_jobs import WindowsJob
from orchestrator.permissions.windows_token import token_is_lpac

_DWORD, _HANDLE, _PTR, _SIZE = wintypes.DWORD, wintypes.HANDLE, wintypes.LPVOID, ctypes.c_size_t


class _Startup(ctypes.Structure):
    _fields_ = [
        ("cb", _DWORD), ("reserved", wintypes.LPWSTR), ("desktop", wintypes.LPWSTR),
        ("title", wintypes.LPWSTR), ("x", _DWORD), ("y", _DWORD), ("x_size", _DWORD), ("y_size", _DWORD),
        ("x_count", _DWORD), ("y_count", _DWORD), ("fill", _DWORD), ("flags", _DWORD),
        ("show", wintypes.WORD), ("reserved2_size", wintypes.WORD), ("reserved2", _PTR),
        ("stdin", _HANDLE), ("stdout", _HANDLE), ("stderr", _HANDLE),
    ]


class _StartupEx(ctypes.Structure):
    _fields_ = [("startup", _Startup), ("attributes", _PTR)]


class _Process(ctypes.Structure):
    _fields_ = [("process", _HANDLE), ("thread", _HANDLE), ("pid", _DWORD), ("thread_id", _DWORD)]


class _Capabilities(ctypes.Structure):
    _fields_ = [("sid", _PTR), ("capabilities", _PTR), ("count", _DWORD), ("reserved", _DWORD)]


class _Attributes(ctypes.Structure):
    _fields_ = [("size", _DWORD), ("descriptor", _PTR), ("inherit", wintypes.BOOL)]


@dataclass(frozen=True, slots=True)
class ProcessObservation:
    process_id: int
    user_sid: str
    appcontainer_sid: str | None
    capability_count: int
    lpac: bool
    elevated: bool
    atomic_job: bool
    executable_digest: str


def _api():
    if os.name != "nt":
        raise OSError("Windows process supervision is unavailable")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    security = ctypes.WinDLL("advapi32", use_last_error=True)
    signatures = {
        "InitializeProcThreadAttributeList": ([_PTR, _DWORD, _DWORD, ctypes.POINTER(_SIZE)], wintypes.BOOL),
        "UpdateProcThreadAttribute": ([_PTR, _DWORD, _SIZE, _PTR, _SIZE, _PTR, _PTR], wintypes.BOOL),
        "DeleteProcThreadAttributeList": ([_PTR], None),
        "CreateProcessW": ([wintypes.LPCWSTR, wintypes.LPWSTR, _PTR, _PTR, wintypes.BOOL, _DWORD,
                            _PTR, wintypes.LPCWSTR, ctypes.POINTER(_Startup), ctypes.POINTER(_Process)], wintypes.BOOL),
        "CreateFileW": ([wintypes.LPCWSTR, _DWORD, _DWORD, ctypes.POINTER(_Attributes), _DWORD, _DWORD, _HANDLE], _HANDLE),
        "GetHandleInformation": ([_HANDLE, ctypes.POINTER(_DWORD)], wintypes.BOOL),
        "ResumeThread": ([_HANDLE], _DWORD),
        "WaitForSingleObject": ([_HANDLE, _DWORD], _DWORD),
        "GetExitCodeProcess": ([_HANDLE, ctypes.POINTER(_DWORD)], wintypes.BOOL),
        "CloseHandle": ([_HANDLE], wintypes.BOOL),
        "TerminateProcess": ([_HANDLE, wintypes.UINT], wintypes.BOOL),
        "LocalFree": ([wintypes.HLOCAL], wintypes.HLOCAL),
    }
    for name, (arguments, result) in signatures.items():
        method = getattr(kernel, name)
        method.argtypes, method.restype = arguments, result
    security.OpenProcessToken.argtypes = [_HANDLE, _DWORD, ctypes.POINTER(_HANDLE)]
    security.OpenProcessToken.restype = wintypes.BOOL
    security.GetTokenInformation.argtypes = [_HANDLE, ctypes.c_int, _PTR, _DWORD, ctypes.POINTER(_DWORD)]
    security.GetTokenInformation.restype = wintypes.BOOL
    security.ConvertStringSidToSidW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(_PTR)]
    security.ConvertStringSidToSidW.restype = wintypes.BOOL
    security.ConvertSidToStringSidW.argtypes = [_PTR, ctypes.POINTER(wintypes.LPWSTR)]
    security.ConvertSidToStringSidW.restype = wintypes.BOOL
    return kernel, security


def _checked(result):
    if not result:
        raise ctypes.WinError(ctypes.get_last_error())
    return result


def _sid_text(pointer, kernel, security):
    value = wintypes.LPWSTR()
    _checked(security.ConvertSidToStringSidW(pointer, ctypes.byref(value)))
    try:
        return value.value
    finally:
        kernel.LocalFree(ctypes.cast(value, wintypes.HLOCAL))


def _observe(process, kernel, security, job, digest):
    token = _HANDLE()
    _checked(security.OpenProcessToken(process.process, 0x0008, ctypes.byref(token)))
    try:
        def data(kind, *, optional=False):
            size = _DWORD()
            security.GetTokenInformation(token, kind, None, 0, ctypes.byref(size))
            if not size.value:
                if optional:
                    return None
                raise ctypes.WinError(ctypes.get_last_error())
            buffer = ctypes.create_string_buffer(size.value)
            _checked(security.GetTokenInformation(token, kind, buffer, size.value, ctypes.byref(size)))
            return buffer

        user = data(1)
        user_sid = _sid_text(ctypes.cast(user, ctypes.POINTER(_PTR))[0], kernel, security)
        container = bool(ctypes.cast(data(29), ctypes.POINTER(_DWORD))[0])
        app_sid = _sid_text(ctypes.cast(data(31), ctypes.POINTER(_PTR))[0], kernel, security) if container else None
        capability_count = int(ctypes.cast(data(30), ctypes.POINTER(_DWORD))[0]) if container else 0
        lpac = token_is_lpac(token, security) if container else False
        return ProcessObservation(
            int(process.pid), user_sid, app_sid, capability_count,
            lpac,
            bool(ctypes.cast(data(20), ctypes.POINTER(_DWORD))[0]), job.contains(process.process), digest,
        )
    finally:
        kernel.CloseHandle(token)


class OwnedWindowsProcess:
    def __init__(self, process, job, observation, kernel):
        self._process, self._job, self.observation, self._kernel = process, job, observation, kernel
        self._started, self._stopped = False, False

    @property
    def process_handle(self):
        if self._stopped:
            raise OSError("owned process is closed")
        return int(self._process.process)

    def start(self):
        if self._started or self._stopped or not self._job.contains(self._process.process):
            raise OSError("owned process is not in a verified suspended state")
        if self._kernel.ResumeThread(self._process.thread) == 0xFFFFFFFF:
            raise ctypes.WinError(ctypes.get_last_error())
        self._started = True

    def poll(self):
        if self._stopped:
            return True
        result = self._kernel.WaitForSingleObject(self._process.process, 0)
        if result not in {0, 258}:
            raise ctypes.WinError(ctypes.get_last_error())
        return result == 0

    def exit_code(self):
        if self._stopped or not self.poll():
            raise OSError("the owned process has not exited")
        value = _DWORD()
        _checked(self._kernel.GetExitCodeProcess(self._process.process, ctypes.byref(value)))
        return int(value.value)

    def stop(self, *, timeout_ms=4500):
        if self._stopped:
            return
        # This proves the whole Job empty, not merely the root PID exited.
        # On failure retain handles for later cleanup and report unknown.
        self._job.stop(timeout_ms=timeout_ms)
        self._kernel.CloseHandle(self._process.thread)
        self._kernel.CloseHandle(self._process.process)
        self._stopped = True


def launch_suspended(*, executable: Path, arguments: list[str], cwd: Path,
                     environment: dict[str, str], expected_user_sid: str,
                     executable_digest: str, inherited_handles=(),
                     appcontainer_sid: str | None = None, require_lpac=False):
    kernel, security = _api()
    executable, cwd = Path(executable), Path(cwd)
    if (not executable.is_absolute() or not cwd.is_absolute() or not cwd.is_dir()
            or not isinstance(arguments, list) or any(not isinstance(arg, str) or "\0" in arg for arg in arguments)
            or not isinstance(expected_user_sid, str) or not expected_user_sid.startswith("S-1-")
            or len(executable_digest) != 64
            or sha256(executable.read_bytes()).hexdigest() != executable_digest):
        raise ValueError("a fixed qualified executable and owned working directory are required")
    if require_lpac and appcontainer_sid is None:
        raise ValueError("LPAC requires an AppContainer identity")
    if (not isinstance(environment, dict)
            or any(not isinstance(key, str) or not key or "=" in key or "\0" in key
                   or not isinstance(value, str) or "\0" in value for key, value in environment.items())):
        raise ValueError("a complete sanitized environment is required")
    process, job, sid, null_handle = _Process(), WindowsJob(), _PTR(), None
    attributes, initialized, transferred = None, False, False
    cleanup_failure, cleanup_cause = None, None
    try:
        count = 2 + (1 if appcontainer_sid else 0) + (1 if require_lpac else 0)
        size = _SIZE()
        kernel.InitializeProcThreadAttributeList(None, count, 0, ctypes.byref(size))
        if not size.value:
            raise ctypes.WinError(ctypes.get_last_error())
        attributes = ctypes.create_string_buffer(size.value)
        _checked(kernel.InitializeProcThreadAttributeList(attributes, count, 0, ctypes.byref(size)))
        initialized = True

        def attribute(kind, value):
            _checked(kernel.UpdateProcThreadAttribute(
                attributes, 0, kind, ctypes.byref(value), ctypes.sizeof(value), None, None,
            ))

        jobs = job.creation_attribute()
        attribute(0x0002000D, jobs)
        if appcontainer_sid:
            _checked(security.ConvertStringSidToSidW(appcontainer_sid, ctypes.byref(sid)))
            caps = _Capabilities(sid, None, 0, 0)
            attribute(0x00020009, caps)
        if require_lpac:
            opt_out = _DWORD(1)  # PROCESS_CREATION_ALL_APPLICATION_PACKAGES_OPT_OUT
            attribute(0x0002000F, opt_out)
        sec = _Attributes(ctypes.sizeof(_Attributes), None, True)
        null_handle = kernel.CreateFileW("NUL", 0xC0000000, 3, ctypes.byref(sec), 3, 0, None)
        if null_handle == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        permitted = tuple(dict.fromkeys([int(null_handle), *inherited_handles]))
        for handle in permitted:
            flags = _DWORD()
            _checked(kernel.GetHandleInformation(handle, ctypes.byref(flags)))
            if not flags.value & 1:
                raise ValueError("only explicitly inheritable owned handles may be passed")
        handles = (_HANDLE * len(permitted))(*permitted)
        attribute(0x00020002, handles)
        startup = _StartupEx()
        startup.startup.cb = ctypes.sizeof(startup)
        startup.startup.flags = 0x00000100
        startup.startup.stdin = startup.startup.stdout = startup.startup.stderr = null_handle
        startup.attributes = ctypes.cast(attributes, _PTR)
        command = ctypes.create_unicode_buffer(subprocess.list2cmdline([str(executable), *arguments]))
        env = ctypes.create_unicode_buffer("\0".join(f"{key}={value}" for key, value in sorted(
            environment.items(), key=lambda pair: pair[0].casefold(),
        )) + "\0\0")
        created = kernel.CreateProcessW(
            str(executable), command, None, None, True, 0x00080000 | 0x00000004 | 0x00000400 | 0x08000000,
            env, str(cwd), ctypes.byref(startup.startup), ctypes.byref(process),
        )
        if not created:
            creation_error = ctypes.WinError(ctypes.get_last_error())
            # PROCESS_INFORMATION is undefined on failure. The kernel has not
            # returned owned process handles; do not terminate/close arbitrary
            # stale output values and hide the actual creation error.
            process = _Process()
            raise creation_error
        observation = _observe(process, kernel, security, job, executable_digest)
        if (observation.user_sid != expected_user_sid or observation.appcontainer_sid != appcontainer_sid
                or observation.elevated or not observation.atomic_job or observation.capability_count != 0
                or require_lpac and not observation.lpac):
            verification_error = PermissionError("suspended child differs from the qualified execution identity")
            verification_error.observation = observation
            raise verification_error
        owned = OwnedWindowsProcess(process, job, observation, kernel)
        transferred = True
        return owned
    finally:
        if not transferred:
            cleanup_error = None
            try:
                job.stop(timeout_ms=4500)
            except Exception as exc:
                cleanup_error = exc
            # A failed atomic-Job verification cannot make an escaped,
            # suspended root disappear. Stop only the retained created handle.
            if process.process and kernel.WaitForSingleObject(process.process, 0) != 0:
                terminated = kernel.TerminateProcess(process.process, 1)
                termination_error = ctypes.get_last_error() if not terminated else 0
                stopped = kernel.WaitForSingleObject(process.process, 4500)
                if stopped != 0:
                    cleanup_error = OSError(
                        f"created suspended process cleanup is unverified (terminate={termination_error}, wait={stopped})"
                    )
            if cleanup_error is not None:
                cleanup_failure, cleanup_cause = WindowsProcessCleanupFailed(process, job, kernel), cleanup_error
            else:
                if process.thread:
                    kernel.CloseHandle(process.thread)
                if process.process:
                    kernel.CloseHandle(process.process)
        if initialized:
            kernel.DeleteProcThreadAttributeList(attributes)
        if sid:
            kernel.LocalFree(sid)
        if null_handle and null_handle != ctypes.c_void_p(-1).value:
            kernel.CloseHandle(null_handle)
        if cleanup_failure is not None:
            raise cleanup_failure from cleanup_cause


class WindowsProcessCleanupFailed(OSError):
    """Retain exact owned handles for cleanup; there is deliberately no start API."""

    def __init__(self, process, job, kernel):
        self._process, self._job, self._kernel = process, job, kernel
        super().__init__("created process cleanup is unverified")

    def retry_cleanup(self):
        if self._process.process and self._kernel.WaitForSingleObject(self._process.process, 0) != 0:
            _checked(self._kernel.TerminateProcess(self._process.process, 1))
            if self._kernel.WaitForSingleObject(self._process.process, 4500) != 0:
                raise OSError("created process still has not stopped")
        self._job.stop(timeout_ms=4500)
        if self._process.thread:
            self._kernel.CloseHandle(self._process.thread)
            self._process.thread = None
        if self._process.process:
            self._kernel.CloseHandle(self._process.process)
            self._process.process = None
