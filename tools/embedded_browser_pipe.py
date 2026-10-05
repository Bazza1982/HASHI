"""Raw JSONL Windows named-pipe I/O with cancellable overlapped deadlines.

Electron net.Server uses a raw pipe, not multiprocessing's AF_PIPE framing or
challenge/response protocol. Authentication remains in the bounded JSONL envelope.
"""
from __future__ import annotations

import os
import time


def exchange(endpoint: str, payload: bytes, *, timeout: float, max_response: int) -> bytes:
    if os.name != "nt":
        raise OSError("Windows named pipes require a native Windows Worker")
    import ctypes as c
    from ctypes import wintypes as w

    class OVERLAPPED(c.Structure):
        _fields_ = [("Internal", c.c_size_t), ("InternalHigh", c.c_size_t),
                    ("Offset", w.DWORD), ("OffsetHigh", w.DWORD), ("hEvent", w.HANDLE)]

    kernel = c.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, c.c_void_p, w.DWORD, w.DWORD, w.HANDLE]
    kernel.CreateFileW.restype = w.HANDLE
    kernel.CreateEventW.argtypes = [c.c_void_p, w.BOOL, w.BOOL, w.LPCWSTR]
    kernel.CreateEventW.restype = w.HANDLE
    kernel.CloseHandle.argtypes = [w.HANDLE]
    kernel.WaitNamedPipeW.argtypes = [w.LPCWSTR, w.DWORD]
    kernel.WaitForSingleObject.argtypes = [w.HANDLE, w.DWORD]
    kernel.WaitForSingleObject.restype = w.DWORD
    for name in ("ReadFile", "WriteFile"):
        fn = getattr(kernel, name)
        fn.argtypes = [w.HANDLE, c.c_void_p, w.DWORD, c.POINTER(w.DWORD), c.POINTER(OVERLAPPED)]
        fn.restype = w.BOOL
    kernel.GetOverlappedResult.argtypes = [w.HANDLE, c.POINTER(OVERLAPPED), c.POINTER(w.DWORD), w.BOOL]
    kernel.CancelIoEx.argtypes = [w.HANDLE, c.POINTER(OVERLAPPED)]
    deadline = time.monotonic() + timeout
    handle = kernel.CreateFileW(endpoint, 0xC0000000, 0, None, 3, 0x40000000, None)
    if handle == c.c_void_p(-1).value:
        error = c.get_last_error()
        if error != 231:
            raise c.WinError(error)
        kernel.WaitNamedPipeW(endpoint, max(1, int((deadline-time.monotonic())*1000)))
        handle = kernel.CreateFileW(endpoint, 0xC0000000, 0, None, 3, 0x40000000, None)
        if handle == c.c_void_p(-1).value:
            raise c.WinError(c.get_last_error())

    def io(operation, buffer, length):
        event = kernel.CreateEventW(None, True, False, None)
        if not event:
            raise c.WinError(c.get_last_error())
        overlapped = OVERLAPPED(hEvent=event)
        count = w.DWORD()
        try:
            ok = operation(handle, buffer, length, c.byref(count), c.byref(overlapped))
            if not ok and c.get_last_error() != 997:
                raise c.WinError(c.get_last_error())
            if not ok:
                remaining = max(0, int((deadline-time.monotonic())*1000))
                if kernel.WaitForSingleObject(event, remaining) != 0:
                    kernel.CancelIoEx(handle, c.byref(overlapped))
                    # Keep the buffer and OVERLAPPED alive until cancellation is
                    # acknowledged; otherwise Windows may write into freed memory.
                    kernel.GetOverlappedResult(handle, c.byref(overlapped), c.byref(count), True)
                    raise TimeoutError("desktop browser pipe deadline exceeded; action was not retried")
                if not kernel.GetOverlappedResult(handle, c.byref(overlapped), c.byref(count), False):
                    raise c.WinError(c.get_last_error())
            return count.value
        finally:
            kernel.CloseHandle(event)

    try:
        outgoing = c.create_string_buffer(payload)
        if io(kernel.WriteFile, outgoing, len(payload)) != len(payload):
            raise OSError("incomplete browser pipe write; action was not retried")
        result = bytearray()
        while b"\n" not in result:
            buffer = c.create_string_buffer(min(65536, max_response + 1 - len(result)))
            count = io(kernel.ReadFile, buffer, len(buffer))
            if not count:
                break
            result.extend(buffer.raw[:count])
            if len(result) > max_response:
                raise ValueError("desktop browser response exceeds limit")
        return bytes(result).split(b"\n", 1)[0]
    finally:
        kernel.CloseHandle(handle)
