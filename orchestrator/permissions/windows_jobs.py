"""Owned native Windows Job: containment from creation and verified tree stop.

The Job is never inherited by an execution host and never permits breakaway.
An installed journal can reserve a private owner-protected name for crash
recovery; ordinary callers keep unnamed Jobs. Handles pass only as a creation attribute. A successful
root-process exit is not proof that the Job's descendants have stopped.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
import time
import threading

from orchestrator.permissions.windows_job_security import create_private_job, open_private_job


class JobControlError(OSError):
    pass


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


class _Accounting(ctypes.Structure):
    _fields_ = [
        ("user_time", ctypes.c_longlong), ("kernel_time", ctypes.c_longlong),
        ("period_user_time", ctypes.c_longlong), ("period_kernel_time", ctypes.c_longlong),
        ("page_faults", wintypes.DWORD), ("total_processes", wintypes.DWORD),
        ("active_processes", wintypes.DWORD), ("terminated_processes", wintypes.DWORD),
    ]


def _job_kernel():
    if os.name != "nt":
        raise JobControlError("native Windows is required")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.SetInformationJobObject.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
    ]
    kernel.SetInformationJobObject.restype = wintypes.BOOL
    kernel.QueryInformationJobObject.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p,
    ]
    kernel.QueryInformationJobObject.restype = wintypes.BOOL
    kernel.IsProcessInJob.argtypes = [wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)]
    kernel.IsProcessInJob.restype = wintypes.BOOL
    kernel.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel.TerminateJobObject.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    return kernel


class WindowsJob:
    _recovery_owners = {}
    _recovery_lock = threading.RLock()

    def __init__(self, *, max_processes=None, name=None, owner_sid=None) -> None:
        if max_processes is not None and (type(max_processes) is not int or max_processes <= 0):
            raise ValueError("a positive explicit Job process limit is required")
        if os.name != "nt":
            raise JobControlError("native Windows is required")
        kernel = _job_kernel()
        self._kernel = kernel
        if (name is None) != (owner_sid is None):
            raise ValueError("a recoverable Job requires both its name and owner SID")
        self.name, self.owner_sid = name, owner_sid
        self._handle = (kernel.CreateJobObjectW(None, None) if name is None
                        else create_private_job(kernel, name, owner_sid))
        if not self._handle:
            raise JobControlError(ctypes.get_last_error(), "cannot create execution Job")
        self._stopped = False
        try:
            limits = _ExtendedLimit()
            limits.basic.flags = 0x2000  # KILL_ON_JOB_CLOSE; no breakaway flags.
            if max_processes is not None:
                limits.basic.flags |= 0x8  # ACTIVE_PROCESS
                limits.basic.active_process_limit = max_processes
            if not kernel.SetInformationJobObject(self._handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
                raise JobControlError(ctypes.get_last_error(), "cannot configure execution Job")
            observed = _ExtendedLimit()
            if not kernel.QueryInformationJobObject(
                self._handle, 9, ctypes.byref(observed), ctypes.sizeof(observed), None,
            ) or (observed.basic.flags & 0x3800) != 0x2000:
                raise JobControlError("execution Job containment could not be observed")
            if max_processes is not None and (not observed.basic.flags & 0x8
                    or observed.basic.active_process_limit != max_processes):
                raise JobControlError("execution Job process limit could not be observed")
            self.max_processes = max_processes
        except BaseException:
            if not kernel.CloseHandle(self._handle):
                raise JobControlError(ctypes.get_last_error(), "unverified Job handle cleanup")
            self._handle = None
            raise

    @classmethod
    def recover_stop(cls, name, owner_sid, *, max_processes=1, timeout_ms=4500):
        """Stop only a reserved, owner-verified original Job; absence is kernel evidence."""
        with cls._recovery_lock:
            key = (name, owner_sid)
            owner = cls._recovery_owners.get(key)
            if owner is None:
                kernel = _job_kernel()
                handle = open_private_job(kernel, name, owner_sid)
                if handle is None:
                    return {"job_name": name, "zero_job": True, "absent": True}
                owner = cls.__new__(cls)
                owner._kernel, owner._handle, owner._stopped = kernel, handle, False
                owner.name, owner.owner_sid, owner.max_processes = name, owner_sid, max_processes
                cls._recovery_owners[key] = owner
            observed = _ExtendedLimit()
            if not owner._kernel.QueryInformationJobObject(
                owner.handle, 9, ctypes.byref(observed), ctypes.sizeof(observed), None,
            ):
                raise JobControlError("the reserved Job limits could not be observed")
            if (observed.basic.flags & 0x3800) != 0x2000 or (
                not observed.basic.flags & 0x8 or observed.basic.active_process_limit != max_processes
            ):
                # A crash can occur after CreateJobObject but before limits
                # are installed. Only the kernel's never-assigned count can
                # prove that such a reservation owned no process at all.
                accounting = _Accounting()
                if not owner._kernel.QueryInformationJobObject(
                    owner.handle, 1, ctypes.byref(accounting), ctypes.sizeof(accounting), None,
                ) or accounting.total_processes or accounting.active_processes:
                    raise JobControlError("the reserved Job has unverified containment limits")
                if not owner._kernel.CloseHandle(owner.handle):
                    raise JobControlError("empty reserved Job cleanup remains unverified")
                owner._handle, owner._stopped = None, True
                cls._recovery_owners.pop(key, None)
                return {"job_name": name, "zero_job": True, "unstarted_job": True}
            owner.stop(timeout_ms=timeout_ms)
            cls._recovery_owners.pop(key, None)
            return {"job_name": name, "zero_job": True, "absent": False}

    @property
    def handle(self):
        if self._handle is None:
            raise JobControlError("the execution Job is closed")
        return self._handle

    @property
    def stopped(self) -> bool:
        return self._stopped

    def creation_attribute(self):
        """Keep this buffer alive until CreateProcess has returned."""
        return (wintypes.HANDLE * 1)(self.handle)

    def contains(self, process_handle) -> bool:
        answer = wintypes.BOOL()
        if not self._kernel.IsProcessInJob(process_handle, self.handle, ctypes.byref(answer)):
            raise JobControlError(ctypes.get_last_error(), "cannot verify process Job binding")
        return bool(answer.value)

    def active_process_count(self) -> int:
        accounting = _Accounting()
        if not self._kernel.QueryInformationJobObject(
            self.handle, 1, ctypes.byref(accounting), ctypes.sizeof(accounting), None,
        ):
            raise JobControlError(ctypes.get_last_error(), "cannot verify execution tree state")
        return int(accounting.active_processes)

    def stop(self, *, timeout_ms: int = 5000) -> None:
        """Fence the whole Job and verify zero live processes before closing."""
        if type(timeout_ms) is not int or not 0 <= timeout_ms <= 60_000:
            raise ValueError("invalid execution cleanup deadline")
        if self._handle is None:
            if self._stopped:
                return
            raise JobControlError("execution tree stop is unverified")
        if not self._kernel.TerminateJobObject(self.handle, 1):
            raise JobControlError(ctypes.get_last_error(), "cannot terminate execution tree")
        deadline = time.monotonic() + timeout_ms / 1000
        while self.active_process_count():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise JobControlError("execution tree did not stop within its deadline")
            time.sleep(min(0.01, remaining))
        if not self._kernel.CloseHandle(self._handle):
            raise JobControlError(ctypes.get_last_error(), "execution Job handle cleanup is unverified")
        self._handle = None
        self._stopped = True

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.stop()
        return False
