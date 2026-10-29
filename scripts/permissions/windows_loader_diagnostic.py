"""Owned-child loader events for disposable Windows probes only.

Never attach to an existing process, edit machine debugger settings, or change
the restricted token, Jobs, capabilities, environment or target file ACLs.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from pathlib import Path
import time

_POINTER = wintypes.LPVOID
_HANDLE = wintypes.HANDLE
_DWORD = wintypes.DWORD


class _ExceptionRecord(ctypes.Structure):
    _fields_ = [("code", _DWORD), ("flags", _DWORD), ("record", _POINTER),
                ("address", _POINTER), ("count", _DWORD),
                ("information", ctypes.c_size_t * 15)]


class _Exception(ctypes.Structure):
    _fields_ = [("record", _ExceptionRecord), ("first_chance", _DWORD)]


class _Creation(ctypes.Structure):
    _fields_ = [("file", _HANDLE), ("process", _HANDLE), ("thread", _HANDLE),
                ("base", _POINTER), ("offset", _DWORD), ("size", _DWORD),
                ("local", _POINTER), ("start", _POINTER), ("name", _POINTER),
                ("unicode", wintypes.WORD)]


class _Library(ctypes.Structure):
    _fields_ = [("file", _HANDLE), ("base", _POINTER), ("offset", _DWORD),
                ("size", _DWORD), ("name", _POINTER), ("unicode", wintypes.WORD)]


class _DebugString(ctypes.Structure):
    _fields_ = [("address", _POINTER), ("unicode", wintypes.WORD), ("length", wintypes.WORD)]


class _Details(ctypes.Union):
    _fields_ = [("exception", _Exception), ("creation", _Creation),
                ("library", _Library), ("exit_code", _DWORD), ("debug", _DebugString)]


class _Event(ctypes.Structure):
    _fields_ = [("kind", _DWORD), ("pid", _DWORD), ("tid", _DWORD), ("details", _Details)]


class OwnedLoaderEvents:
    def __init__(self):
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        api = self.kernel
        api.WaitForDebugEventEx.argtypes = [ctypes.POINTER(_Event), _DWORD]
        api.WaitForDebugEventEx.restype = wintypes.BOOL
        api.ContinueDebugEvent.argtypes = [_DWORD, _DWORD, _DWORD]
        api.ContinueDebugEvent.restype = wintypes.BOOL
        api.GetFinalPathNameByHandleW.argtypes = [_HANDLE, wintypes.LPWSTR, _DWORD, _DWORD]
        api.GetFinalPathNameByHandleW.restype = _DWORD
        api.ReadProcessMemory.argtypes = [_HANDLE, _POINTER, _POINTER, ctypes.c_size_t,
                                         ctypes.POINTER(ctypes.c_size_t)]
        api.ReadProcessMemory.restype = wintypes.BOOL
        api.CloseHandle.argtypes, api.CloseHandle.restype = [_HANDLE], wintypes.BOOL
        api.TerminateProcess.argtypes, api.TerminateProcess.restype = [_HANDLE, wintypes.UINT], wintypes.BOOL
        self.modules, self.exceptions, self.messages = [], [], []
        self.exited = False

    @staticmethod
    def intercept(module):
        original = module._api

        def changed():
            kernel, security = original()
            create = kernel.CreateProcessW

            def debugged(*arguments):
                values = list(arguments)
                values[5] |= 0x2  # DEBUG_ONLY_THIS_PROCESS; created owned child only.
                return create(*values)

            kernel.CreateProcessW = debugged
            return kernel, security

        module._api = changed
        return original

    def pump(self, process):
        event = _Event()
        if not self.kernel.WaitForDebugEventEx(ctypes.byref(event), 20):
            error = ctypes.get_last_error()
            if error == 121:
                return
            raise ctypes.WinError(error)
        if event.pid != process.observation.process_id:
            raise OSError("a loader event belongs to another process")
        continuation = 0x10002
        file = None
        try:
            if event.kind in {3, 6}:
                file = event.details.creation.file if event.kind == 3 else event.details.library.file
                if file:
                    name = ctypes.create_unicode_buffer(32768)
                    length = self.kernel.GetFinalPathNameByHandleW(file, name, len(name), 0)
                    if not 0 < length < len(name):
                        raise ctypes.WinError(ctypes.get_last_error())
                    self.modules.append(Path(name.value).name)
            elif event.kind == 1:
                code = int(event.details.exception.record.code)
                self.exceptions.append({"code": hex(code), "first_chance": bool(event.details.exception.first_chance)})
                if code not in {0x80000003, 0x80000004}:
                    continuation = 0x80010001
            elif event.kind == 8 and len(self.messages) < 16:
                value = event.details.debug
                count = min(2048, value.length * (2 if value.unicode else 1))
                if count:
                    data, read = ctypes.create_string_buffer(count), ctypes.c_size_t()
                    if self.kernel.ReadProcessMemory(process.process_handle, value.address, data, count, ctypes.byref(read)):
                        self.messages.append(data.raw[:read.value].decode("utf-16-le" if value.unicode else "utf-8",
                                                                        errors="replace").strip("\0")[:1500])
            elif event.kind == 5:
                self.exited = True
        finally:
            if file and not self.kernel.CloseHandle(file):
                raise OSError("owned loader file handle cleanup failed")
            if not self.kernel.ContinueDebugEvent(event.pid, event.tid, continuation):
                raise ctypes.WinError(ctypes.get_last_error())

    def terminate_and_drain(self, process):
        if not self.exited:
            if not self.kernel.TerminateProcess(process.process_handle, 1):
                raise ctypes.WinError(ctypes.get_last_error())
            deadline = time.monotonic() + 4
            while not self.exited and time.monotonic() < deadline:
                self.pump(process)
            if not self.exited:
                raise OSError("owned diagnostic child exit remains unverified")

    def facts(self):
        return {"modules": self.modules, "exceptions": self.exceptions, "messages": self.messages,
                "debugger_observed_exit": self.exited}
