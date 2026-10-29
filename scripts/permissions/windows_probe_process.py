"""Start one fixed sentinel under a disposable Windows account and owned job.

Test infrastructure only: this does not start HERV3, install a service, or
establish an L2 boundary. It never invokes a shell or accepts arbitrary code.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import os
from pathlib import Path
import re
import subprocess
import uuid

from scripts.permissions.windows_probe_staging import StageError


class ProbeCleanupUnverified(StageError):
    """The owned child or job could not be confirmed stopped."""


@dataclass(frozen=True, slots=True)
class ChildFacts:
    user_sid: str
    elevated: bool
    administrators: bool
    owned_job: bool


def run_owned_sentinel(api, identity, stage) -> None:
    """Run fixed Python code after checking the suspended child and its job."""
    if (not re.fullmatch(r"HashiProbe_[0-9a-f]{8}", str(identity.name))
            or not isinstance(identity.password, str) or not identity.password
            or not isinstance(identity.sid, str) or identity.sid != stage.sid
            or stage._handle is None):
        raise StageError("an active disposable identity and stage are required")
    stage._check_identity()
    executable = stage.python / "python.exe"
    if not executable.is_file() or not stage.scratch.is_dir():
        raise StageError("isolated test runtime is unavailable")
    sentinel = stage.scratch / ("sentinel-" + uuid.uuid4().hex + ".bin")
    nonce = uuid.uuid4().bytes
    job = api.create_job()
    process = None
    try:
        process = api.start_suspended(identity, executable, stage.scratch, sentinel, nonce, job)
        facts = api.facts(process, job)
        if (not isinstance(facts, ChildFacts) or facts.user_sid != identity.sid or facts.elevated
                or facts.administrators or not facts.owned_job):
            raise StageError("suspended test process has the wrong identity or job")
        api.resume(process)
        exit_code = api.wait(process, 20_000)
        if exit_code is None:
            raise StageError("test process timed out")
        if exit_code != 0:
            raise StageError("test process failed")
    finally:
        cleanup_error = None
        try:
            api.close_job(job)
        except Exception as exc:
            cleanup_error = exc
        if process is not None:
            try:
                if not api.stopped(process, 5_000):
                    cleanup_error = StageError("test process did not stop")
            except Exception as exc:
                cleanup_error = exc
            try:
                api.close_process(process)
            except Exception as exc:
                cleanup_error = exc
        if cleanup_error is not None:
            raise ProbeCleanupUnverified("test process cleanup is unverified") from cleanup_error
    try:
        if sentinel.read_bytes() != nonce:
            raise StageError("test process result did not match")
    except OSError as exc:
        raise StageError("test process result is unavailable") from exc


class _BasicLimit(ctypes.Structure):
    _fields_ = [
        ("process_time", ctypes.c_longlong), ("job_time", ctypes.c_longlong),
        ("flags", wintypes.DWORD), ("minimum_working", ctypes.c_size_t),
        ("maximum_working", ctypes.c_size_t), ("active_process_limit", wintypes.DWORD),
        ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD),
        ("scheduling", wintypes.DWORD),
    ]


class _IoCounters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_ulonglong) for name in (
        "read_operations", "write_operations", "other_operations",
        "read_bytes", "write_bytes", "other_bytes",
    )]


class _ExtendedLimit(ctypes.Structure):
    _fields_ = [
        ("basic", _BasicLimit), ("io", _IoCounters), ("process_memory", ctypes.c_size_t),
        ("job_memory", ctypes.c_size_t), ("peak_process_memory", ctypes.c_size_t),
        ("peak_job_memory", ctypes.c_size_t),
    ]


class WindowsProbeProcess:
    """Windows API implementation for the fixed sentinel, with a kill-on-close job."""

    def __init__(self, computer_name: str) -> None:
        if os.name != "nt" or not computer_name:
            raise StageError("native Windows and a local computer name are required")
        from scripts.permissions import windows_appcontainer_probe as probe

        self.probe = probe
        self.kernel = probe.KERNEL
        self.adv = probe.ADVAPI
        self.computer_name = computer_name
        self.kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        self.kernel.CreateJobObjectW.restype = wintypes.HANDLE
        self.kernel.SetInformationJobObject.argtypes = [
            wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
        ]
        self.kernel.SetInformationJobObject.restype = wintypes.BOOL
        self.kernel.IsProcessInJob.argtypes = [wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)]
        self.kernel.IsProcessInJob.restype = wintypes.BOOL
        self.kernel.GetWindowsDirectoryW.argtypes = [wintypes.LPWSTR, wintypes.UINT]
        self.kernel.GetWindowsDirectoryW.restype = wintypes.UINT
        self.adv.LogonUserW.argtypes = [
            wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR,
            wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE),
        ]
        self.adv.LogonUserW.restype = wintypes.BOOL
        self.adv.CreateProcessAsUserW.argtypes = [
            wintypes.HANDLE, wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.c_void_p,
            ctypes.c_void_p, wintypes.BOOL, wintypes.DWORD, ctypes.c_void_p,
            wintypes.LPCWSTR, ctypes.POINTER(probe.STARTUPINFOW),
            ctypes.POINTER(probe.PROCESS_INFORMATION),
        ]
        self.adv.CreateProcessAsUserW.restype = wintypes.BOOL

    def create_job(self):
        job = self.kernel.CreateJobObjectW(None, None)
        if not job:
            raise StageError("cannot create the test job")
        limits = _ExtendedLimit()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.kernel.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            self.kernel.CloseHandle(job)
            raise StageError("cannot configure test job cleanup")
        return job

    def start_suspended(self, identity, executable: Path, scratch: Path, sentinel: Path, nonce: bytes, job):
        probe = self.probe
        size = probe.SIZE_T()
        self.kernel.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(size))
        if not size.value:
            raise StageError("cannot size the test process attributes")
        attributes = ctypes.create_string_buffer(size.value)
        initialized = False
        process = probe.PROCESS_INFORMATION()
        token = wintypes.HANDLE()
        created = False
        try:
            if not self.adv.LogonUserW(
                identity.name, self.computer_name, identity.password, 2, 0, ctypes.byref(token)
            ):
                raise StageError(f"cannot log on test identity (Windows error {ctypes.get_last_error()})")
            if not self.kernel.InitializeProcThreadAttributeList(attributes, 1, 0, ctypes.byref(size)):
                raise StageError("cannot initialize test process attributes")
            initialized = True
            job_list = (wintypes.HANDLE * 1)(job)
            if not self.kernel.UpdateProcThreadAttribute(
                attributes, 0, 0x0002000D, ctypes.byref(job_list),
                ctypes.sizeof(job_list), None, None,
            ):
                raise StageError("cannot bind test process to its job")
            startup = probe.STARTUPINFOEXW()
            startup.StartupInfo.cb = ctypes.sizeof(startup)
            startup.lpAttributeList = ctypes.cast(attributes, ctypes.c_void_p)
            program = "import sys; f=open(sys.argv[1],'xb'); f.write(bytes.fromhex(sys.argv[2])); f.close()"
            command = ctypes.create_unicode_buffer(subprocess.list2cmdline(
                [str(executable), "-I", "-S", "-c", program, str(sentinel), nonce.hex()]
            ))
            windows = ctypes.create_unicode_buffer(32768)
            count = self.kernel.GetWindowsDirectoryW(windows, len(windows))
            if not count or count >= len(windows):
                raise StageError("cannot determine the Windows directory")
            env = {
                "SystemRoot": windows.value, "WINDIR": windows.value,
                "TEMP": str(scratch), "TMP": str(scratch), "USERPROFILE": str(scratch),
                "APPDATA": str(scratch), "LOCALAPPDATA": str(scratch),
            }
            environment = ctypes.create_unicode_buffer(
                "\0".join(f"{key}={value}" for key, value in sorted(env.items())) + "\0\0"
            )
            flags = 0x00080000 | 0x00000004 | 0x00000400 | 0x08000000
            if not self.adv.CreateProcessAsUserW(
                token, str(executable), command, None, None, False, flags,
                environment, str(scratch), ctypes.byref(startup.StartupInfo), ctypes.byref(process),
            ):
                raise StageError(f"cannot start suspended test process (Windows error {ctypes.get_last_error()})")
            created = True
            return process
        finally:
            if initialized:
                self.kernel.DeleteProcThreadAttributeList(attributes)
            token_closed = not token or bool(self.kernel.CloseHandle(token))
            if not created or not token_closed:
                # A partially returned process is always ours; never look up or
                # kill another process by name or PID.
                cleanup_ok = token_closed
                if process.hProcess:
                    if not self.kernel.TerminateProcess(process.hProcess, 1):
                        cleanup_ok = False
                    if self.kernel.WaitForSingleObject(process.hProcess, 5_000) != 0:
                        cleanup_ok = False
                    if not self.kernel.CloseHandle(process.hProcess):
                        cleanup_ok = False
                if process.hThread and not self.kernel.CloseHandle(process.hThread):
                    cleanup_ok = False
                if not cleanup_ok:
                    raise ProbeCleanupUnverified("partial test process cleanup is unverified")

    def facts(self, process, job) -> ChildFacts:
        probe = self.probe
        member = wintypes.BOOL()
        if not self.kernel.IsProcessInJob(process.hProcess, job, ctypes.byref(member)):
            raise StageError("cannot verify the child job")
        token = wintypes.HANDLE()
        if not self.adv.OpenProcessToken(process.hProcess, 0x0008, ctypes.byref(token)):
            raise StageError("cannot inspect the child token")
        try:
            user = probe._token_data(token, 1)
            sid = probe._sid_text(ctypes.cast(user, ctypes.POINTER(probe.SID_AND_ATTRIBUTES)).contents.Sid)
            elevation = probe._token_data(token, 20)
            elevated = bool(ctypes.cast(elevation, ctypes.POINTER(wintypes.DWORD)).contents.value)
            groups = probe._token_data(token, 2)
            count = ctypes.cast(groups, ctypes.POINTER(probe.TOKEN_GROUPS)).contents.GroupCount
            entries = ctypes.cast(
                ctypes.addressof(groups) + probe.TOKEN_GROUPS.Groups.offset,
                ctypes.POINTER(probe.SID_AND_ATTRIBUTES),
            )
            administrators = any(probe._sid_text(entries[index].Sid) == "S-1-5-32-544"
                                 for index in range(count))
            return ChildFacts(sid, elevated, administrators, bool(member.value))
        finally:
            self.kernel.CloseHandle(token)

    def resume(self, process) -> None:
        if self.kernel.ResumeThread(process.hThread) == 0xFFFFFFFF:
            raise StageError("cannot resume the verified test process")

    def wait(self, process, milliseconds: int) -> int | None:
        status = self.kernel.WaitForSingleObject(process.hProcess, milliseconds)
        if status == 0x102:
            return None
        if status != 0:
            raise StageError("cannot wait for the test process")
        code = wintypes.DWORD()
        if not self.kernel.GetExitCodeProcess(process.hProcess, ctypes.byref(code)):
            raise StageError("cannot inspect the test process exit")
        return code.value

    def stopped(self, process, milliseconds: int) -> bool:
        return self.kernel.WaitForSingleObject(process.hProcess, milliseconds) == 0

    def close_process(self, process) -> None:
        thread_closed = bool(self.kernel.CloseHandle(process.hThread))
        process_closed = bool(self.kernel.CloseHandle(process.hProcess))
        if not thread_closed or not process_closed:
            raise StageError("test process handle cleanup is unverified")

    def close_job(self, job) -> None:
        if not self.kernel.CloseHandle(job):
            raise StageError("test job cleanup is unverified")
