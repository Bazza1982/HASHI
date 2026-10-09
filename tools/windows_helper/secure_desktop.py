"""Opt-in, local Windows SYSTEM desktop adapter; existing PAO owns all leases.

The privileged host only exposes bounded screen/input primitives through an ACL
restricted named pipe. It has no TCP listener, file/command or credential API.
No retries of input, including partial Unicode entry, are permitted here.
"""
from __future__ import annotations

import base64
import ctypes
import io
import json
import os
import struct
import threading
import time
from ctypes import wintypes as W
from pathlib import Path

from orchestrator.desktop_contract import DesktopError
from .desktop_capture import NativeDesktop, key_code

MAX_RESPONSE = 8_000_000


class SecureDesktop(NativeDesktop):
    def __init__(self, configuration):
        super().__init__()
        self.configuration = dict(configuration)
        self.rpc_guard = threading.Lock()

    def available(self):
        result = self.desktop_state()
        if not result.get('available') or result.get('session_id') != self.configuration['session_id']:
            raise DesktopError('desktop_secure_host_unavailable', 503)

    def desktop_state(self):
        return self._call('status', {})

    def cursor(self):
        return {**self._call('cursor', {}), 'visible': True}

    def capture(self, rect, size):
        from PIL import Image
        result = self._call('capture', {**rect, 'output_width': size[0], 'output_height': size[1]})
        data = base64.b64decode(result['image'], validate=True)
        if len(data) > 5_500_000:
            raise DesktopError('desktop_invalid_frame', 502)
        with Image.open(io.BytesIO(data)) as image:
            if image.size != tuple(size):
                raise DesktopError('desktop_invalid_frame', 502)
            return image.convert('RGB')

    def inject(self, event):
        if event['kind'] == 'reset':
            return self.reset()
        args = {k: event[k] for k in ('kind', 'px', 'py', 'button', 'delta', 'horizontal', 'text') if k in event}
        if event['kind'] in {'key_down', 'key_up'}:
            args['vk'], args['extended'] = key_code(event['key'])
        result = self._call('input', args)
        if result.get('injected') is not True:
            raise DesktopError('desktop_input_failed', 503)

    def reset(self):
        return self._call('reset', {})

    def _call(self, operation, args):
        # Serialize bridge IO only. Controller capture work does not hold its
        # mutation lock; individual native calls have a bounded IO deadline.
        with self.rpc_guard:
            try:
                return pipe_request(self.configuration, operation, args)
            except DesktopError:
                raise
            except Exception as exc:
                raise DesktopError('desktop_secure_host_unavailable', 503) from exc


def native_desktop_for(bridge_home):
    path = Path(bridge_home) / 'state' / 'windows-desktop-bridge.json'
    if not path.exists():
        return NativeDesktop()
    from orchestrator.config_json import read_config_json
    value = read_config_json(path)
    return SecureDesktop(value)


def pipe_request(config, operation, args):
    """Verify SYSTEM pipe ownership/session before sending any image/input data."""
    if os.name != 'nt':
        raise DesktopError('desktop_platform_unsupported', 501)
    k = ctypes.WinDLL('kernel32', use_last_error=True)
    a = ctypes.WinDLL('advapi32', use_last_error=True)
    signatures = {
        'WaitNamedPipeW': ([W.LPCWSTR, W.DWORD], W.BOOL),
        'CreateFileW': ([W.LPCWSTR, W.DWORD, W.DWORD, ctypes.c_void_p, W.DWORD, W.DWORD, W.HANDLE], W.HANDLE),
        'CloseHandle': ([W.HANDLE], W.BOOL),
        'GetNamedPipeServerProcessId': ([W.HANDLE, ctypes.POINTER(W.DWORD)], W.BOOL),
        'GetNamedPipeServerSessionId': ([W.HANDLE, ctypes.POINTER(W.DWORD)], W.BOOL),
        'CreateEventW': ([ctypes.c_void_p, W.BOOL, W.BOOL, W.LPCWSTR], W.HANDLE),
        'LocalFree': ([ctypes.c_void_p], ctypes.c_void_p),
        'WaitForSingleObject': ([W.HANDLE, W.DWORD], W.DWORD),
    }
    for name, (arguments, result) in signatures.items():
        fn = getattr(k, name); fn.argtypes = arguments; fn.restype = result
    a.GetSecurityInfo.argtypes = [W.HANDLE, ctypes.c_int, W.DWORD, ctypes.POINTER(ctypes.c_void_p),
                                 ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    a.GetSecurityInfo.restype = W.DWORD
    a.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(W.LPWSTR)]
    a.ConvertSidToStringSidW.restype = W.BOOL
    pipe = '\\\\.\\pipe\\' + config['pipe_name']
    deadline = time.monotonic() + 2
    while True:
        ready = k.WaitNamedPipeW(pipe, 100)
        handle = k.CreateFileW(pipe, 0xC0000000, 0, None, 3, 0x40000000, None) if ready else ctypes.c_void_p(-1).value
        if handle != ctypes.c_void_p(-1).value:
            break
        # A server instance is briefly absent between local requests. Only
        # connection establishment is retried: no request bytes have been sent.
        if ctypes.get_last_error() not in {2, 121, 231} or time.monotonic() >= deadline:
            raise DesktopError('desktop_secure_host_unavailable', 503)
        time.sleep(.01)
    try:
        pid, session = W.DWORD(), W.DWORD()
        # Query the authenticated pipe endpoint. ProcessIdToSessionId requests
        # broader process access and fails for SYSTEM under a limited worker.
        if not k.GetNamedPipeServerProcessId(handle, ctypes.byref(pid)) or not k.GetNamedPipeServerSessionId(handle, ctypes.byref(session)):
            raise DesktopError('desktop_target_changed', 409)
        # A limited interactive worker cannot inspect SYSTEM process handles.
        # Authenticate the pipe's kernel owner instead; a limited user cannot
        # create a SYSTEM-owned pipe or add another instance to its ACL.
        owner, descriptor, sid = ctypes.c_void_p(), ctypes.c_void_p(), W.LPWSTR()
        try:
            if a.GetSecurityInfo(handle, 1, 1, ctypes.byref(owner), None, None, None, ctypes.byref(descriptor)) or not a.ConvertSidToStringSidW(owner, ctypes.byref(sid)):
                raise DesktopError('desktop_target_changed', 409)
            if sid.value != 'S-1-5-18' or session.value != config['session_id']:
                raise DesktopError('desktop_target_changed', 409)
        finally:
            if sid: k.LocalFree(sid)
            if descriptor: k.LocalFree(descriptor)
        request = json.dumps({'operation': operation, 'args': args}, ensure_ascii=True).encode()
        if len(request) > 32768:
            raise DesktopError('desktop_request_too_large', 413)
        _pipe_io(k, handle, struct.pack('<I', len(request)) + request, writing=True)
        length = struct.unpack('<I', _pipe_io(k, handle, 4))[0]
        if not 0 < length <= MAX_RESPONSE:
            raise DesktopError('desktop_invalid_frame', 502)
        response = json.loads(_pipe_io(k, handle, length))
        if response.get('ok') is not True:
            raise DesktopError('desktop_secure_host_unavailable', 503)
        return response['result']
    finally:
        k.CloseHandle(handle)


def _pipe_io(k, handle, value, *, writing=False):
    class Overlapped(ctypes.Structure):
        _fields_ = [('internal', ctypes.c_size_t), ('internal_high', ctypes.c_size_t),
                    ('offset', W.DWORD), ('offset_high', W.DWORD), ('event', W.HANDLE)]
    for name in ('ReadFile', 'WriteFile'):
        fn = getattr(k, name)
        fn.argtypes = [W.HANDLE, ctypes.c_void_p, W.DWORD, ctypes.POINTER(W.DWORD), ctypes.POINTER(Overlapped)]
        fn.restype = W.BOOL
    k.GetOverlappedResult.argtypes = [W.HANDLE, ctypes.POINTER(Overlapped), ctypes.POINTER(W.DWORD), W.BOOL]
    k.GetOverlappedResult.restype = W.BOOL
    k.CancelIoEx.argtypes = [W.HANDLE, ctypes.POINTER(Overlapped)]
    total = len(value) if writing else value
    buffer = ctypes.create_string_buffer(value if writing else total)
    offset = 0
    while offset < total:
        event = k.CreateEventW(None, True, False, None)
        if not event:
            raise OSError('desktop IO event unavailable')
        o = Overlapped(event=event); transferred = W.DWORD()
        try:
            fn = k.WriteFile if writing else k.ReadFile
            if not fn(handle, ctypes.byref(buffer, offset), total-offset, ctypes.byref(transferred), ctypes.byref(o)):
                if ctypes.get_last_error() != 997:
                    raise OSError('desktop IO failed')
                if k.WaitForSingleObject(event, 3500) != 0:
                    k.CancelIoEx(handle, ctypes.byref(o))
                    # Keep the IO buffer alive until cancellation completes.
                    k.GetOverlappedResult(handle, ctypes.byref(o), ctypes.byref(transferred), True)
                    raise TimeoutError('desktop IO timeout')
                if not k.GetOverlappedResult(handle, ctypes.byref(o), ctypes.byref(transferred), False):
                    raise OSError('desktop IO failed')
            if not transferred.value:
                raise OSError('desktop IO closed')
            offset += transferred.value
        finally:
            k.CloseHandle(event)
    return buffer.raw[:total]
