"""Bind a Windows file path to held handles before a supervised read.

This primitive is not an authorization check, sandbox, or process supervisor.
The caller must use an already authorized immutable Scope, serialize the
authority start/revoke barrier, and keep all returned handles private.
Only local drive paths are supported; reparse points, hard links, UNC paths,
and case-sensitive directories are rejected rather than guessed.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import ntpath
import os
import re

from orchestrator.permissions.models import Scope

_READ_ACCESS = 0x00100081  # SYNCHRONIZE | FILE_READ_ATTRIBUTES | FILE_READ_DATA/LIST_DIRECTORY
_SHARE_READ_WRITE = 0x3  # Deliberately excludes FILE_SHARE_DELETE.
_OPEN_EXISTING = 3
_BACKUP_SEMANTICS = 0x02000000
_OPEN_REPARSE_POINT = 0x00200000
_OBJ_CASE_INSENSITIVE = 0x40
_OBJ_DONT_REPARSE = 0x1000
_NT_OPEN = 1
_NT_SYNCHRONOUS = 0x20
_NT_DIRECTORY = 0x1
_NT_NON_DIRECTORY = 0x40
_DIRECTORY_ATTRIBUTE = 0x10
_REPARSE_ATTRIBUTE = 0x400
_CASE_SENSITIVE_INFO = 23


class FileBindingError(ValueError):
    """The approved file could not be bound safely."""


class _UnicodeString(ctypes.Structure):
    _fields_ = [("Length", wintypes.USHORT), ("MaximumLength", wintypes.USHORT),
                ("Buffer", wintypes.LPWSTR)]


class _ObjectAttributes(ctypes.Structure):
    _fields_ = [
        ("Length", wintypes.ULONG), ("RootDirectory", wintypes.HANDLE),
        ("ObjectName", ctypes.POINTER(_UnicodeString)), ("Attributes", wintypes.ULONG),
        ("SecurityDescriptor", ctypes.c_void_p), ("SecurityQualityOfService", ctypes.c_void_p),
    ]


class _IoStatus(ctypes.Structure):
    _fields_ = [("Status", ctypes.c_void_p), ("Information", ctypes.c_size_t)]


class _FileInformation(ctypes.Structure):
    _fields_ = [
        ("attributes", wintypes.DWORD), ("creation", wintypes.FILETIME),
        ("access", wintypes.FILETIME), ("write", wintypes.FILETIME),
        ("volume_serial", wintypes.DWORD), ("size_high", wintypes.DWORD),
        ("size_low", wintypes.DWORD), ("links", wintypes.DWORD),
        ("index_high", wintypes.DWORD), ("index_low", wintypes.DWORD),
    ]


class _CaseSensitiveInfo(ctypes.Structure):
    _fields_ = [("flags", wintypes.ULONG)]


class _Windows:
    def __init__(self):
        if os.name != "nt":
            raise FileBindingError("native Windows is required")
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.nt = ctypes.WinDLL("ntdll", use_last_error=True)
        self.kernel.CreateFileW.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
            wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
        ]
        self.kernel.CreateFileW.restype = wintypes.HANDLE
        self.kernel.GetFileInformationByHandle.argtypes = [
            wintypes.HANDLE, ctypes.POINTER(_FileInformation),
        ]
        self.kernel.GetFileInformationByHandle.restype = wintypes.BOOL
        self.kernel.GetFileInformationByHandleEx.argtypes = [
            wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
        ]
        self.kernel.GetFileInformationByHandleEx.restype = wintypes.BOOL
        self.kernel.GetFinalPathNameByHandleW.argtypes = [
            wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD,
        ]
        self.kernel.GetFinalPathNameByHandleW.restype = wintypes.DWORD
        self.kernel.ReadFile.argtypes = [
            wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p,
        ]
        self.kernel.ReadFile.restype = wintypes.BOOL
        self.kernel.SetFilePointerEx.argtypes = [
            wintypes.HANDLE, ctypes.c_longlong, ctypes.c_void_p, wintypes.DWORD,
        ]
        self.kernel.SetFilePointerEx.restype = wintypes.BOOL
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel.CloseHandle.restype = wintypes.BOOL
        self.nt.NtCreateFile.argtypes = [
            ctypes.POINTER(wintypes.HANDLE), wintypes.ULONG,
            ctypes.POINTER(_ObjectAttributes), ctypes.POINTER(_IoStatus),
            ctypes.c_void_p, wintypes.ULONG, wintypes.ULONG, wintypes.ULONG,
            wintypes.ULONG, ctypes.c_void_p, wintypes.ULONG,
        ]
        self.nt.NtCreateFile.restype = ctypes.c_long

    def open_root(self, root: str) -> int:
        # No FILE_SHARE_DELETE: opened path components cannot be renamed or
        # replaced while the bound operation is holding their handles.
        handle = self.kernel.CreateFileW(
            root, _READ_ACCESS, _SHARE_READ_WRITE, None, _OPEN_EXISTING,
            _BACKUP_SEMANTICS | _OPEN_REPARSE_POINT, None,
        )
        if handle in (None, ctypes.c_void_p(-1).value):
            raise FileBindingError("cannot open approved volume root")
        return handle

    def open_child(self, parent: int, name: str, *, directory: bool) -> int:
        buffer = ctypes.create_unicode_buffer(name)
        length = len(name.encode("utf-16-le"))
        string = _UnicodeString(length, length + 2, ctypes.cast(buffer, wintypes.LPWSTR))
        attributes = _ObjectAttributes(
            ctypes.sizeof(_ObjectAttributes), parent, ctypes.pointer(string),
            _OBJ_CASE_INSENSITIVE | _OBJ_DONT_REPARSE, None, None,
        )
        handle = wintypes.HANDLE()
        io = _IoStatus()
        options = _OPEN_REPARSE_POINT | _NT_SYNCHRONOUS | (_NT_DIRECTORY if directory else _NT_NON_DIRECTORY)
        status = self.nt.NtCreateFile(
            ctypes.byref(handle), _READ_ACCESS, ctypes.byref(attributes), ctypes.byref(io),
            None, 0, _SHARE_READ_WRITE, _NT_OPEN, options, None, 0,
        )
        if status < 0 or not handle.value:
            raise FileBindingError("cannot safely open approved path component")
        return handle.value

    def check(self, handle: int, expected: str, *, directory: bool) -> None:
        info = _FileInformation()
        if not self.kernel.GetFileInformationByHandle(handle, ctypes.byref(info)):
            raise FileBindingError("cannot inspect approved file")
        if bool(info.attributes & _DIRECTORY_ATTRIBUTE) != directory or info.attributes & _REPARSE_ATTRIBUTE:
            raise FileBindingError("reparse points and unexpected object types are not allowed")
        if not directory and info.links != 1:
            raise FileBindingError("multiple hard links are not allowed")
        if directory:
            case = _CaseSensitiveInfo()
            if not self.kernel.GetFileInformationByHandleEx(
                handle, _CASE_SENSITIVE_INFO, ctypes.byref(case), ctypes.sizeof(case)
            ):
                raise FileBindingError("cannot verify directory case sensitivity")
            if case.flags & 1:
                raise FileBindingError("case-sensitive directories are not supported")
        buffer = ctypes.create_unicode_buffer(32768)
        size = self.kernel.GetFinalPathNameByHandleW(handle, buffer, len(buffer), 0)
        if not size or size >= len(buffer):
            raise FileBindingError("cannot verify final file path")
        actual = buffer.value
        if actual.startswith("\\\\?\\"):
            actual = actual[4:]
        if ntpath.normcase(ntpath.normpath(actual)) != ntpath.normcase(ntpath.normpath(expected)):
            raise FileBindingError("opened object differs from the approved path")


class BoundWindowsRead:
    """A held final handle plus every directory handle from its drive root."""

    def __init__(self, api: _Windows, handles: list[int]) -> None:
        self._api = api
        self._handles = handles

    def close(self) -> None:
        while self._handles:
            self._api.kernel.CloseHandle(self._handles.pop())

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()

    def read(self, *, max_bytes: int) -> bytes:
        if not self._handles:
            raise FileBindingError("bound handle is closed")
        if type(max_bytes) is not int or not 0 < max_bytes <= 16 * 1024 * 1024:
            raise FileBindingError("invalid read limit")
        handle = self._handles[-1]
        if not self._api.kernel.SetFilePointerEx(handle, 0, None, 0):
            raise FileBindingError("cannot seek approved file")
        result = bytearray()
        while len(result) <= max_bytes:
            count = min(65536, max_bytes + 1 - len(result))
            buffer = ctypes.create_string_buffer(count)
            received = wintypes.DWORD()
            if not self._api.kernel.ReadFile(handle, buffer, count, ctypes.byref(received), None):
                raise FileBindingError("cannot read approved file")
            if not received.value:
                return bytes(result)
            result.extend(buffer.raw[:received.value])
        raise FileBindingError("file exceeds the read limit")


def open_bound_read(scope: Scope) -> BoundWindowsRead:
    """Hold a safe file object; authorization must be checked by the caller."""
    if not isinstance(scope, Scope) or scope.kind != "file" or "read" not in scope.actions:
        raise FileBindingError("an approved read scope is required")
    drive, tail = ntpath.splitdrive(scope.resource)
    if not re.fullmatch(r"[a-zA-Z]:", drive) or not tail.startswith("\\"):
        raise FileBindingError("only absolute local drive paths are supported")
    names = tail.lstrip("\\").split("\\")
    if not names or any(
        not name or name in {".", ".."} or len(name.encode("utf-16-le")) > 510
        or any(char in name for char in ':/*?"<>|\x00') or name.endswith((" ", "."))
        for name in names
    ):
        raise FileBindingError("unsafe approved path component")
    api = _Windows()
    handles: list[int] = []
    try:
        current = drive + "\\"
        root = api.open_root(current)
        handles.append(root)
        api.check(root, current, directory=True)
        for index, name in enumerate(names):
            directory = index < len(names) - 1
            child = api.open_child(handles[-1], name, directory=directory)
            handles.append(child)
            current = ntpath.join(current, name)
            api.check(child, current, directory=directory)
        return BoundWindowsRead(api, handles)
    except Exception:
        while handles:
            api.kernel.CloseHandle(handles.pop())
        raise
