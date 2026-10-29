"""Bind Windows file paths to held handles for supervised read or creation.

This primitive is not an authorization check, sandbox, or process supervisor.
The caller must use an already authorized immutable Scope, serialize the
authority start/revoke barrier, and keep all returned handles private.
Only local drive paths are supported; reparse points, hard links, UNC paths,
and case-sensitive directories are rejected rather than guessed.
Creation installs a new name only and never replaces existing files. A process
crash can leave a temporary file; protected recovery has not been implemented.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import ntpath
import os
import re
from uuid import uuid4

from orchestrator.permissions.models import Scope

_READ_ACCESS = 0x00100081  # SYNCHRONIZE | FILE_READ_ATTRIBUTES | FILE_READ_DATA/LIST_DIRECTORY
_SHARE_READ_WRITE = 0x3  # Deliberately excludes FILE_SHARE_DELETE.
_OPEN_EXISTING = 3
_BACKUP_SEMANTICS = 0x02000000
_OPEN_REPARSE_POINT = 0x00200000
_OBJ_CASE_INSENSITIVE = 0x40
_OBJ_DONT_REPARSE = 0x1000
_NT_OPEN = 1
_NT_CREATE = 2
_NT_SYNCHRONOUS = 0x20
_NT_DIRECTORY = 0x1
_NT_NON_DIRECTORY = 0x40
_DIRECTORY_ATTRIBUTE = 0x10
_REPARSE_ATTRIBUTE = 0x400
_CASE_SENSITIVE_INFO = 23
_CREATE_ACCESS = 0x00110082  # SYNCHRONIZE | DELETE | FILE_READ_ATTRIBUTES | FILE_WRITE_DATA
_NT_RENAME_INFORMATION = 10
_NT_DISPOSITION_INFORMATION = 13


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


class _RenameInformation(ctypes.Structure):
    _fields_ = [
        ("flags", wintypes.ULONG), ("root", wintypes.HANDLE),
        ("name_bytes", wintypes.ULONG), ("name", wintypes.WCHAR * 1),
    ]


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
        self.kernel.WriteFile.argtypes = [
            wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p,
        ]
        self.kernel.WriteFile.restype = wintypes.BOOL
        self.kernel.SetEndOfFile.argtypes = [wintypes.HANDLE]
        self.kernel.SetEndOfFile.restype = wintypes.BOOL
        self.kernel.FlushFileBuffers.argtypes = [wintypes.HANDLE]
        self.kernel.FlushFileBuffers.restype = wintypes.BOOL
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
        self.nt.NtSetInformationFile.argtypes = [
            wintypes.HANDLE, ctypes.POINTER(_IoStatus), ctypes.c_void_p,
            wintypes.ULONG, ctypes.c_int,
        ]
        self.nt.NtSetInformationFile.restype = ctypes.c_long

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

    def open_child(self, parent: int, name: str, *, directory: bool,
                   access: int = _READ_ACCESS, share: int = _SHARE_READ_WRITE) -> int:
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
            ctypes.byref(handle), access, ctypes.byref(attributes), ctypes.byref(io),
            None, 0, share, _NT_OPEN, options, None, 0,
        )
        if status < 0 or not handle.value:
            error = FileBindingError("cannot safely open approved path component")
            error.ntstatus = status & 0xFFFFFFFF
            raise error
        return handle.value

    def create_temporary(self, parent: int, name: str) -> int:
        buffer = ctypes.create_unicode_buffer(name)
        length = len(name.encode("utf-16-le"))
        string = _UnicodeString(length, length + 2, ctypes.cast(buffer, wintypes.LPWSTR))
        attributes = _ObjectAttributes(
            ctypes.sizeof(_ObjectAttributes), parent, ctypes.pointer(string),
            _OBJ_CASE_INSENSITIVE | _OBJ_DONT_REPARSE, None, None,
        )
        handle = wintypes.HANDLE()
        io = _IoStatus()
        status = self.nt.NtCreateFile(
            ctypes.byref(handle), _CREATE_ACCESS, ctypes.byref(attributes), ctypes.byref(io),
            None, 0, 0, _NT_CREATE, _OPEN_REPARSE_POINT | _NT_SYNCHRONOUS | _NT_NON_DIRECTORY,
            None, 0,
        )
        if status < 0 or not handle.value:
            raise FileBindingError("cannot create a private temporary file")
        return handle.value

    def write_all(self, handle: int, data: bytes) -> None:
        offset = 0
        while offset < len(data):
            chunk = data[offset:offset + 65536]
            buffer = ctypes.create_string_buffer(chunk)
            count = wintypes.DWORD()
            if not self.kernel.WriteFile(handle, buffer, len(chunk), ctypes.byref(count), None) or not count.value:
                raise FileBindingError("cannot write temporary file")
            offset += count.value
        if not self.kernel.FlushFileBuffers(handle):
            raise FileBindingError("cannot flush temporary file")

    def rename_new(self, handle: int, parent: int, name: str) -> None:
        encoded = name.encode("utf-16-le")
        size = max(ctypes.sizeof(_RenameInformation), _RenameInformation.name.offset + len(encoded))
        buffer = ctypes.create_string_buffer(size)
        information = _RenameInformation.from_buffer(buffer)
        information.flags = 0  # Never replace an existing entry.
        information.root = parent
        information.name_bytes = len(encoded)
        ctypes.memmove(ctypes.addressof(buffer) + _RenameInformation.name.offset, encoded, len(encoded))
        io = _IoStatus()
        status = self.nt.NtSetInformationFile(
            handle, ctypes.byref(io), buffer, size, _NT_RENAME_INFORMATION,
        )
        if status < 0:
            raise FileBindingError("target exists or cannot be safely created")

    def discard_temporary(self, handle: int) -> None:
        disposition = ctypes.c_ubyte(1)
        io = _IoStatus()
        status = self.nt.NtSetInformationFile(
            handle, ctypes.byref(io), ctypes.byref(disposition), 1, _NT_DISPOSITION_INFORMATION,
        )
        if status < 0:
            raise FileBindingError("temporary file cleanup could not be confirmed")

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


def _close_handles(api: _Windows, handles: list[int]) -> None:
    failed = False
    while handles:
        if not api.kernel.CloseHandle(handles.pop()):
            failed = True
    if failed:
        raise FileBindingError("handle cleanup could not be confirmed")


class _BoundHandles:
    def __init__(self, api: _Windows, handles: list[int]) -> None:
        self._api = api
        self._handles = handles

    def close(self) -> None:
        _close_handles(self._api, self._handles)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()


class BoundWindowsRead(_BoundHandles):
    """A held final handle plus every directory handle from its drive root."""

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


class BoundWindowsWrite(_BoundHandles):
    """An existing file held exclusively against other writers and replacement."""

    def commit(self, data: bytes) -> None:
        if not self._handles or not isinstance(data, bytes) or len(data) > 1024 * 1024:
            raise FileBindingError("invalid held file write")
        handle = self._handles[-1]
        if not self._api.kernel.SetFilePointerEx(handle, 0, None, 0):
            raise FileBindingError("cannot seek approved write target")
        self._api.write_all(handle, data)
        if not self._api.kernel.SetEndOfFile(handle) or not self._api.kernel.FlushFileBuffers(handle):
            raise FileBindingError("cannot durably truncate approved write target")


class BoundWindowsCreate(_BoundHandles):
    """A parent directory held stable until a new-only atomic rename."""

    def __init__(self, api: _Windows, handles: list[int], directory: str, name: str) -> None:
        super().__init__(api, handles)
        self._directory = directory
        self._name = name
        self._used = False

    def commit(self, data: bytes) -> None:
        if not self._handles or self._used:
            raise FileBindingError("create binding is closed or already used")
        if not isinstance(data, bytes) or len(data) > 1024 * 1024:
            raise FileBindingError("invalid file content or size")
        self._used = True
        parent = self._handles[-1]
        name = ".hashi-permission-" + uuid4().hex + ".tmp"
        temporary = self._api.create_temporary(parent, name)
        expected = ntpath.join(self._directory, name)
        try:
            self._api.check(temporary, expected, directory=False)
            self._api.write_all(temporary, data)
            self._api.rename_new(temporary, parent, self._name)
        except BaseException:
            # If the rename outcome is uncertain, never delete a handle which
            # might already refer to the user's target.
            try:
                self._api.check(temporary, expected, directory=False)
                self._api.discard_temporary(temporary)
            except Exception as cleanup_error:
                raise FileBindingError("temporary file cleanup could not be confirmed") from cleanup_error
            raise
        finally:
            if not self._api.kernel.CloseHandle(temporary):
                raise FileBindingError("temporary handle cleanup could not be confirmed")


def _path_parts(scope: Scope, required: frozenset[str]) -> tuple[str, list[str]]:
    if not isinstance(scope, Scope) or scope.kind != "file" or not required.issubset(scope.actions):
        raise FileBindingError("the required file scope is missing")
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
    return drive, names


def _open_parents(api: _Windows, drive: str, names: list[str]) -> tuple[list[int], str]:
    handles: list[int] = []
    try:
        current = drive + "\\"
        root = api.open_root(current)
        handles.append(root)
        api.check(root, current, directory=True)
        for name in names[:-1]:
            child = api.open_child(handles[-1], name, directory=True)
            handles.append(child)
            current = ntpath.join(current, name)
            api.check(child, current, directory=True)
        return handles, current
    except Exception:
        _close_handles(api, handles)
        raise


def open_bound_read(scope: Scope) -> BoundWindowsRead:
    """Hold a safe file object; authorization must be checked by the caller."""
    drive, names = _path_parts(scope, frozenset({"read"}))
    api = _Windows()
    handles, current = _open_parents(api, drive, names)
    try:
        child = api.open_child(handles[-1], names[-1], directory=False)
        handles.append(child)
        api.check(child, ntpath.join(current, names[-1]), directory=False)
        return BoundWindowsRead(api, handles)
    except Exception:
        _close_handles(api, handles)
        raise


def open_bound_create(scope: Scope) -> BoundWindowsCreate:
    """Bind a parent for new-file creation; caller must authorize before commit."""
    drive, names = _path_parts(scope, frozenset({"create", "write"}))
    api = _Windows()
    handles, current = _open_parents(api, drive, names)
    return BoundWindowsCreate(api, handles, current, names[-1])


def open_bound_write(scope: Scope) -> BoundWindowsWrite | BoundWindowsCreate:
    """Bind an existing exclusive writer, or a held parent for a new file."""
    drive, names = _path_parts(scope, frozenset({"create", "write"}))
    api = _Windows()
    handles, current = _open_parents(api, drive, names)
    try:
        try:
            child = api.open_child(handles[-1], names[-1], directory=False,
                                   access=0x00100082, share=1)
        except FileBindingError as error:
            if getattr(error, "ntstatus", None) != 0xC0000034:
                raise
            return BoundWindowsCreate(api, handles, current, names[-1])
        handles.append(child)
        api.check(child, ntpath.join(current, names[-1]), directory=False)
        return BoundWindowsWrite(api, handles)
    except BaseException:
        _close_handles(api, handles)
        raise
