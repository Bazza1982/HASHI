"""Local Windows pipe transport with explicit ACLs and kernel peer identity.

No pickle, caller-selected role, localhost trust, or shared frontend secret is
accepted as execution/approval identity. The listener owns each pending handle.
AppContainer execution endpoints use the Windows LOCAL pipe namespace.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
import re
import threading

_MAX_FRAME = 16 * 1024 * 1024
_PIPE = re.compile(r"\\\\\.\\pipe\\LOCAL\\HASHI\.Permissions\.[A-Za-z0-9_.-]{1,160}\Z")
_SID = re.compile(r"S-1-[0-9]+(?:-[0-9]+){1,15}\Z")


@dataclass(frozen=True, slots=True)
class WindowsPeer:
    process_id: int
    user_sid: str
    appcontainer_sid: str | None
    elevated: bool
    lpac: bool


@dataclass(frozen=True, slots=True)
class PipePeerPolicy:
    user_sid: str
    appcontainer_sid: str | None = None
    process_id: int | None = None
    require_lpac: bool = False

    def __post_init__(self):
        for sid in (self.user_sid, self.appcontainer_sid):
            if sid is not None and (not isinstance(sid, str) or _SID.fullmatch(sid) is None):
                raise ValueError("an explicit Windows SID is required")
        if self.process_id is not None and (type(self.process_id) is not int or self.process_id <= 0):
            raise ValueError("a retained process identity is required")
        if self.require_lpac and self.appcontainer_sid is None:
            raise ValueError("LPAC requires an AppContainer endpoint")

    def verify(self, peer):
        if (not isinstance(peer, WindowsPeer) or peer.user_sid != self.user_sid
                or peer.appcontainer_sid != self.appcontainer_sid
                or self.process_id is not None and peer.process_id != self.process_id
                or self.appcontainer_sid is not None and peer.elevated
                or self.require_lpac and not peer.lpac):
            raise PermissionError("Windows pipe peer is outside the assigned role")


def _windows():
    if os.name != "nt":
        raise OSError("Windows named-pipe transport is unavailable on this platform")
    import ctypes
    from ctypes import wintypes

    return ctypes, wintypes, ctypes.WinDLL("kernel32", use_last_error=True), ctypes.WinDLL("advapi32", use_last_error=True)


def _sid_text(pointer, ctypes, wintypes, kernel, security):
    if not pointer:
        return None
    value = wintypes.LPWSTR()
    convert = security.ConvertSidToStringSidW
    convert.argtypes, convert.restype = [wintypes.LPVOID, ctypes.POINTER(wintypes.LPWSTR)], wintypes.BOOL
    free = kernel.LocalFree
    free.argtypes, free.restype = [wintypes.HLOCAL], wintypes.HLOCAL
    if not convert(pointer, ctypes.byref(value)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return value.value
    finally:
        free(ctypes.cast(value, wintypes.HLOCAL))


def pipe_peer(handle) -> WindowsPeer:
    from orchestrator.permissions.windows_token import token_is_lpac

    ctypes, wintypes, kernel, security = _windows()
    get_pid = kernel.GetNamedPipeClientProcessId
    get_pid.argtypes, get_pid.restype = [wintypes.HANDLE, ctypes.POINTER(wintypes.ULONG)], wintypes.BOOL
    pid = wintypes.ULONG()
    if not get_pid(handle, ctypes.byref(pid)) or not pid.value:
        raise ctypes.WinError(ctypes.get_last_error())
    impersonate = security.ImpersonateNamedPipeClient
    impersonate.argtypes, impersonate.restype = [wintypes.HANDLE], wintypes.BOOL
    revert = security.RevertToSelf
    revert.argtypes, revert.restype = [], wintypes.BOOL
    current_thread = kernel.GetCurrentThread
    current_thread.argtypes, current_thread.restype = [], wintypes.HANDLE
    open_token = security.OpenThreadToken
    open_token.argtypes, open_token.restype = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.BOOL, ctypes.POINTER(wintypes.HANDLE),
    ], wintypes.BOOL
    query = security.GetTokenInformation
    query.argtypes, query.restype = [
        wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
    ], wintypes.BOOL
    close = kernel.CloseHandle
    close.argtypes, close.restype = [wintypes.HANDLE], wintypes.BOOL
    if not impersonate(handle):
        raise ctypes.WinError(ctypes.get_last_error())
    token = wintypes.HANDLE()
    try:
        if not open_token(current_thread(), 0x0008, True, ctypes.byref(token)):
            raise ctypes.WinError(ctypes.get_last_error())

        def information(kind, *, optional=False):
            size = wintypes.DWORD()
            query(token, kind, None, 0, ctypes.byref(size))
            if not size.value:
                if optional:
                    return None
                raise ctypes.WinError(ctypes.get_last_error())
            buffer = ctypes.create_string_buffer(size.value)
            if not query(token, kind, buffer, size.value, ctypes.byref(size)):
                if optional:
                    return None
                raise ctypes.WinError(ctypes.get_last_error())
            return buffer

        user = information(1)
        user_sid = _sid_text(ctypes.cast(user, ctypes.POINTER(wintypes.LPVOID))[0],
                             ctypes, wintypes, kernel, security)
        is_container = bool(ctypes.cast(information(29), ctypes.POINTER(wintypes.DWORD))[0])
        app_sid = None
        if is_container:
            app_sid = _sid_text(ctypes.cast(information(31), ctypes.POINTER(wintypes.LPVOID))[0],
                                ctypes, wintypes, kernel, security)
            if app_sid is None:
                raise PermissionError("AppContainer identity is missing")
        elevated = bool(ctypes.cast(information(20), ctypes.POINTER(wintypes.DWORD))[0])
        lpac = token_is_lpac(token, security) if is_container else False
        return WindowsPeer(pid.value, user_sid, app_sid, elevated, lpac)
    finally:
        try:
            if token:
                close(token)
        finally:
            if not revert():
                raise ctypes.WinError(ctypes.get_last_error())


class JsonPipe:
    def __init__(self, connection):
        self._connection = connection
        self._send_lock = threading.Lock()

    @property
    def handle(self):
        return self._connection.fileno()

    def send(self, value):
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
        if len(encoded) > _MAX_FRAME:
            raise ValueError("permission IPC frame exceeds the limit")
        with self._send_lock:
            self._connection.send_bytes(encoded)

    def receive(self, *, timeout_s=30):
        if not 0 < timeout_s <= 3600:
            raise ValueError("permission IPC timeout is out of range")
        if not self._connection.poll(timeout_s):
            raise TimeoutError("permission IPC peer did not respond")
        # A Windows PipeConnection reads one bounded kernel message. Closing
        # this owned handle cancels its overlapped I/O during service shutdown.
        encoded = self._connection.recv_bytes(_MAX_FRAME)
        try:
            value = json.loads(encoded.decode("utf-8"))
        except (UnicodeError, ValueError) as exc:
            raise ValueError("invalid permission IPC JSON") from exc
        if not isinstance(value, dict):
            raise ValueError("permission IPC frame must be an object")
        return value

    def close(self):
        self._connection.close()


class WindowsPipeListener:
    def __init__(self, name, *, service_sid, peer_policy: PipePeerPolicy):
        if not isinstance(name, str) or _PIPE.fullmatch(name) is None:
            raise ValueError("an assigned local permission pipe name is required")
        if not isinstance(service_sid, str) or _SID.fullmatch(service_sid) is None:
            raise ValueError("the service Windows SID is required")
        if not isinstance(peer_policy, PipePeerPolicy):
            raise ValueError("a fixed OS peer policy is required")
        self.name, self.service_sid, self.policy = name, service_sid, peer_policy
        self._closed = threading.Event()
        self._pending = None
        self._lock = threading.Lock()
        self._first = True
        self._spare = None

    def _create(self):
        ctypes, wintypes, kernel, security = _windows()
        convert = security.ConvertStringSecurityDescriptorToSecurityDescriptorW
        convert.argtypes, convert.restype = [
            wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(wintypes.LPVOID), ctypes.POINTER(wintypes.ULONG),
        ], wintypes.BOOL
        descriptor = wintypes.LPVOID()
        # The restricted SID must itself pass AccessCheck; granting Everyone
        # or the ordinary Users group would not represent an execution role.
        client_sid = self.policy.appcontainer_sid or self.policy.user_sid
        sddl = f"D:P(A;;GA;;;{self.service_sid})(A;;GRGW;;;{client_sid})"
        if not convert(sddl, 1, ctypes.byref(descriptor), None):
            raise ctypes.WinError(ctypes.get_last_error())

        class Attributes(ctypes.Structure):
            _fields_ = [("length", wintypes.DWORD), ("descriptor", wintypes.LPVOID),
                        ("inherit", wintypes.BOOL)]

        attributes = Attributes(ctypes.sizeof(Attributes), descriptor, False)
        create = kernel.CreateNamedPipeW
        create.argtypes, create.restype = [
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
            wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(Attributes),
        ], wintypes.HANDLE
        free = kernel.LocalFree
        free.argtypes, free.restype = [wintypes.HLOCAL], wintypes.HLOCAL
        try:
            # Duplex + overlapped + first-instance protection. Reject remote
            # clients at the kernel boundary, before parsing any application data.
            access = 0x00000003 | 0x40000000 | (0x00080000 if self._first else 0)
            handle = create(self.name, access, 0x00000004 | 0x00000002 | 0x00000008,
                            8, 65536, 65536, 1000, ctypes.byref(attributes))
            if handle == ctypes.c_void_p(-1).value:
                raise ctypes.WinError(ctypes.get_last_error())
            self._first = False
            return int(handle)
        finally:
            free(descriptor)

    def accept(self, *, timeout_s=1):
        import _winapi
        from multiprocessing.connection import PipeConnection

        if self._closed.is_set():
            raise OSError("permission listener is closed")
        with self._lock:
            handle, self._spare = self._spare, None
            if handle is None:
                handle = self._create()
            self._pending = handle
        owned = True
        try:
            overlapped = _winapi.ConnectNamedPipe(handle, overlapped=True)
            status = _winapi.WaitForSingleObject(overlapped.event, int(timeout_s * 1000))
            if status == _winapi.WAIT_TIMEOUT:
                overlapped.cancel()
                try:
                    overlapped.GetOverlappedResult(True)
                except OSError as exc:
                    if exc.winerror != 995:
                        raise
                with self._lock:
                    if not self._closed.is_set():
                        self._spare = handle
                        owned = False
                return None
            if status != _winapi.WAIT_OBJECT_0:
                raise OSError("permission pipe accept failed")
            overlapped.GetOverlappedResult(True)
            with self._lock:
                # Keep the name occupied before handing off this instance.
                # A timeout/disconnect must never open a pipe-squatting gap.
                if self._closed.is_set():
                    raise OSError("permission listener is closed")
                self._spare = self._create()
            connection = PipeConnection(handle, readable=True, writable=True)
            owned = False
            pipe = JsonPipe(connection)
            try:
                hello = pipe.receive(timeout_s=5)
                if hello.get("type") != "hashi.permission-hello.v1":
                    raise PermissionError("permission IPC handshake is missing")
                peer = pipe_peer(handle)
                self.policy.verify(peer)
                return pipe, peer, hello
            except BaseException:
                pipe.close()
                raise
        finally:
            with self._lock:
                self._pending = None
            if owned:
                _winapi.CloseHandle(handle)

    def close(self):
        import _winapi

        self._closed.set()
        with self._lock:
            spare, self._spare = self._spare, None
            if spare is not None:
                _winapi.CloseHandle(spare)
            handle = self._pending
            if handle is not None:
                # Cancel owned connect I/O; accept retains handle ownership.
                ctypes, wintypes, kernel, _ = _windows()
                cancel = kernel.CancelIoEx
                cancel.argtypes, cancel.restype = [wintypes.HANDLE, wintypes.LPVOID], wintypes.BOOL
                cancel(handle, None)


def connect_pipe(name, *, timeout_s=5):
    if os.name != "nt" or not isinstance(name, str) or _PIPE.fullmatch(name) is None:
        raise ValueError("a local Windows permission pipe is required")
    import _winapi
    from multiprocessing.connection import PipeConnection

    _winapi.WaitNamedPipe(name, int(timeout_s * 1000))
    handle = _winapi.CreateFile(name, 0x80000000 | 0x40000000, 0, 0, 3,
                                 0x40000000 | 0x00100000 | 0x00010000, 0)
    try:
        _winapi.SetNamedPipeHandleState(handle, 0x00000002, None, None)
        return JsonPipe(PipeConnection(handle, readable=True, writable=True))
    except BaseException:
        _winapi.CloseHandle(handle)
        raise


def current_process_user_sid():
    """Read the current Windows identity without shelling out or changing it."""
    ctypes, wintypes, kernel, security = _windows()
    current = kernel.GetCurrentProcess
    current.argtypes, current.restype = [], wintypes.HANDLE
    open_token = security.OpenProcessToken
    open_token.argtypes, open_token.restype = [
        wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE),
    ], wintypes.BOOL
    query = security.GetTokenInformation
    query.argtypes, query.restype = [
        wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
    ], wintypes.BOOL
    close = kernel.CloseHandle
    close.argtypes, close.restype = [wintypes.HANDLE], wintypes.BOOL
    token, size = wintypes.HANDLE(), wintypes.DWORD()
    if not open_token(current(), 0x0008, ctypes.byref(token)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        query(token, 1, None, 0, ctypes.byref(size))
        if not size.value:
            raise ctypes.WinError(ctypes.get_last_error())
        buffer = ctypes.create_string_buffer(size.value)
        if not query(token, 1, buffer, size.value, ctypes.byref(size)):
            raise ctypes.WinError(ctypes.get_last_error())
        return _sid_text(ctypes.cast(buffer, ctypes.POINTER(wintypes.LPVOID))[0],
                         ctypes, wintypes, kernel, security)
    finally:
        close(token)
