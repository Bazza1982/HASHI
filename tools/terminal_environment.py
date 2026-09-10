"""Platform-owned interactive-console detection shared by terminal entrypoints."""
from __future__ import annotations
import os
import sys


def has_interactive_input() -> bool:
    if not sys.stdin.isatty():
        return False
    if os.name != 'nt':
        return True
    # Windows CRT isatty also returns true for NUL. Only an actual console
    # input handle may authorize implicit interactive selection or creation.
    import ctypes
    from ctypes import wintypes
    import msvcrt
    mode=wintypes.DWORD()
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.GetConsoleMode.argtypes=(wintypes.HANDLE,ctypes.POINTER(wintypes.DWORD))
    kernel.GetConsoleMode.restype=wintypes.BOOL
    try:
        handle=msvcrt.get_osfhandle(sys.stdin.fileno())
        return bool(kernel.GetConsoleMode(handle,ctypes.byref(mode)))
    except (OSError,ValueError):
        return False
