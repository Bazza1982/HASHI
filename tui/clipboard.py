"""Native Windows clipboard adapter used in addition to terminal OSC 52."""
from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys


def _copy_native_windows(text: str) -> bool:
    import ctypes

    cf_unicode_text = 13
    gmem_moveable = 0x0002
    kernel32 = ctypes.windll.kernel32
    user32 = ctypes.windll.user32
    kernel32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalFree.argtypes = [ctypes.c_void_p]
    user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
    user32.SetClipboardData.restype = ctypes.c_void_p
    payload = str(text).encode("utf-16-le") + b"\x00\x00"
    handle = kernel32.GlobalAlloc(gmem_moveable, len(payload))
    if not handle:
        return False
    pointer = kernel32.GlobalLock(handle)
    if not pointer:
        kernel32.GlobalFree(handle)
        return False
    ctypes.memmove(pointer, payload, len(payload))
    kernel32.GlobalUnlock(handle)
    if not user32.OpenClipboard(None):
        kernel32.GlobalFree(handle)
        return False
    try:
        user32.EmptyClipboard()
        if not user32.SetClipboardData(cf_unicode_text, handle):
            return False
        handle = None  # The clipboard owns the allocation after success.
        return True
    finally:
        user32.CloseClipboard()
        if handle:
            kernel32.GlobalFree(handle)


def _is_wsl() -> bool:
    return bool(os.environ.get("WSL_DISTRO_NAME")) or "microsoft" in platform.release().casefold()


def _copy_wsl_to_windows(text: str) -> bool:
    powershell = shutil.which("powershell.exe")
    if not powershell:
        return False
    command = (
        "[Console]::InputEncoding=[Text.Encoding]::UTF8; "
        "Set-Clipboard -Value ([Console]::In.ReadToEnd())"
    )
    try:
        completed = subprocess.run(
            [powershell, "-NoProfile", "-NonInteractive", "-Command", command],
            input=str(text).encode("utf-8"),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0


def copy_to_windows_clipboard(text: str) -> bool:
    """Copy to the host clipboard when running on Windows or under WSL."""

    if sys.platform == "win32":
        try:
            return _copy_native_windows(str(text))
        except (AttributeError, OSError):
            return False
    if _is_wsl():
        return _copy_wsl_to_windows(str(text))
    return False


__all__ = ["copy_to_windows_clipboard"]
