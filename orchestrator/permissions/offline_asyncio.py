"""Asyncio timers, Tasks and executor-backed private IPC for a networkless LPAC.

CPython's Windows provider imports _overlapped, whose initialization probes a
Winsock socket. That probe is denied before a networkless host can start.
This process-local provider uses the standard BaseEventLoop scheduler and a
real threading.Event wakeup. It exposes no socket or subprocess transports.
The fixed host installs it before importing asyncio, after verifying its token.
"""
from __future__ import annotations

import importlib.machinery
import sys
import threading

PROFILE_ID = "windows-lpac-offline-v1"


class _ThreadWakeup:
    def __init__(self):
        self.event = threading.Event()

    def select(self, timeout=None):
        self.event.wait(timeout)
        self.event.clear()
        return []


def _types(base_events, events):
    class OfflineEventLoop(base_events.BaseEventLoop):
        def __init__(self):
            super().__init__()
            self._selector = _ThreadWakeup()

        def _write_to_self(self):
            self._selector.event.set()

        def _process_events(self, ready):
            if ready:
                raise RuntimeError("the offline loop received an external I/O event")

        async def create_connection(self, *args, **kwargs):
            raise NotImplementedError("the offline loop has no network transports")

        async def create_server(self, *args, **kwargs):
            raise NotImplementedError("the offline loop has no network transports")

        async def create_datagram_endpoint(self, *args, **kwargs):
            raise NotImplementedError("the offline loop has no network transports")

    class OfflinePolicy(events.BaseDefaultEventLoopPolicy):
        _loop_factory = OfflineEventLoop
        _hashi_profile = PROFILE_ID

    return OfflineEventLoop, OfflinePolicy


def new_event_loop():
    """Construct the real scheduler independently of the Windows import hook."""
    from asyncio import base_events, events

    loop, _ = _types(base_events, events)
    return loop()


def _require_lpac():
    import ctypes
    from ctypes import wintypes
    from orchestrator.permissions.windows_token import token_is_lpac

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    security = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    security.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    security.OpenProcessToken.restype = wintypes.BOOL
    security.GetTokenInformation.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
    ]
    security.GetTokenInformation.restype = wintypes.BOOL
    token, value, size = wintypes.HANDLE(), wintypes.DWORD(), wintypes.DWORD()
    if not security.OpenProcessToken(kernel.GetCurrentProcess(), 8, ctypes.byref(token)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        if (not security.GetTokenInformation(token, 29, ctypes.byref(value), ctypes.sizeof(value), ctypes.byref(size))
                or value.value != 1 or not token_is_lpac(token, security)):
            raise PermissionError("the offline Windows bootstrap requires an observed LPAC token")
    finally:
        if not kernel.CloseHandle(token):
            raise ctypes.WinError(ctypes.get_last_error())


def install():
    """Install only for an isolated fixed Windows entry, never a shared Worker."""
    if sys.platform != "win32":
        return None
    _require_lpac()
    if "asyncio" in sys.modules:
        policy = sys.modules["asyncio"].get_event_loop_policy()
        if getattr(policy, "_hashi_profile", None) != PROFILE_ID:
            raise RuntimeError("asyncio was initialized before the fixed offline bootstrap")
        return PROFILE_ID

    class WindowsProvider:
        def find_spec(self, fullname, path=None, target=None):
            if fullname == "asyncio.windows_events":
                return importlib.machinery.ModuleSpec(fullname, self)
            return None

        def create_module(self, spec):
            return None

        def exec_module(self, module):
            _, policy = _types(sys.modules["asyncio.base_events"], sys.modules["asyncio.events"])
            module.DefaultEventLoopPolicy = policy
            module.__all__ = ("DefaultEventLoopPolicy",)
            module._hashi_profile = PROFILE_ID

    provider = WindowsProvider()
    sys.meta_path.insert(0, provider)
    try:
        __import__("asyncio")
    finally:
        sys.meta_path.remove(provider)
    return PROFILE_ID
# HASHI-Fingerprint: hfp_c66a413b706a48d9904c885f21f53123
