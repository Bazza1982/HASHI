from __future__ import annotations

import ctypes
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)

EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

user32.EnumWindows.argtypes = [EnumWindowsProc, wintypes.LPARAM]
user32.EnumWindows.restype = wintypes.BOOL
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.IsWindowVisible.restype = wintypes.BOOL
user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user32.GetWindowTextLengthW.restype = ctypes.c_int
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowTextW.restype = ctypes.c_int
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.ShowWindow.restype = wintypes.BOOL
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.SetForegroundWindow.restype = wintypes.BOOL
user32.BringWindowToTop.argtypes = [wintypes.HWND]
user32.BringWindowToTop.restype = wintypes.BOOL
user32.IsIconic.argtypes = [wintypes.HWND]
user32.IsIconic.restype = wintypes.BOOL
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.PostMessageW.restype = wintypes.BOOL
user32.GetForegroundWindow.argtypes = []
user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetKeyboardLayout.argtypes = [wintypes.DWORD]
user32.GetKeyboardLayout.restype = wintypes.HKL
user32.GetKeyboardLayoutNameW.argtypes = [wintypes.LPWSTR]
user32.GetKeyboardLayoutNameW.restype = wintypes.BOOL
user32.keybd_event.argtypes = [ctypes.c_ubyte, ctypes.c_ubyte, wintypes.DWORD, wintypes.ULONG_PTR]
user32.keybd_event.restype = None
user32.mouse_event.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.ULONG_PTR]
user32.mouse_event.restype = None

SW_RESTORE = 9
SW_SHOW = 5
WM_CLOSE = 0x0010
KEYEVENTF_KEYUP = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEUP = 0x0040


def _window_title(hwnd: int) -> str:
    length = user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buffer = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buffer, len(buffer))
    return buffer.value.strip()


def list_windows() -> list[dict]:
    items: list[dict] = []

    @EnumWindowsProc
    def _callback(hwnd: int, _lparam: int) -> bool:
        if not user32.IsWindowVisible(hwnd):
            return True
        title = _window_title(hwnd)
        if not title:
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        items.append(
            {
                "id": int(hwnd),
                "pid": int(pid.value),
                "title": title,
            }
        )
        return True

    user32.EnumWindows(_callback, 0)
    return items


def find_window(
    *,
    window_id: int = 0,
    pid: int = 0,
    title_contains: str = "",
    exact_title: str = "",
) -> dict | None:
    windows = list_windows()
    if window_id:
        return next((item for item in windows if item["id"] == window_id), None)
    if pid:
        return next((item for item in windows if item["pid"] == pid), None)
    if exact_title:
        return next((item for item in windows if item["title"] == exact_title), None)
    if title_contains:
        needle = title_contains.lower()
        return next((item for item in windows if needle in item["title"].lower()), None)
    return None


def focus_window(window: dict) -> dict:
    hwnd = int(window["id"])
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
    else:
        user32.ShowWindow(hwnd, SW_SHOW)
    user32.BringWindowToTop(hwnd)
    user32.SetForegroundWindow(hwnd)
    return window


def close_window(window: dict) -> bool:
    return bool(user32.PostMessageW(int(window["id"]), WM_CLOSE, 0, 0))


def reset_input_state() -> dict:
    for vk in (0x10, 0x11, 0x12, 0x5B, 0x5C):
        user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)
    for flag in (MOUSEEVENTF_LEFTUP, MOUSEEVENTF_RIGHTUP, MOUSEEVENTF_MIDDLEUP):
        user32.mouse_event(flag, 0, 0, 0, 0)
    return {
        "ok": True,
        "released_keys": ["SHIFT", "CTRL", "ALT", "LWIN", "RWIN"],
        "released_mouse": ["left", "right", "middle"],
    }


def get_input_state() -> dict:
    foreground = int(user32.GetForegroundWindow() or 0)
    pid = wintypes.DWORD()
    thread_id = user32.GetWindowThreadProcessId(foreground, ctypes.byref(pid)) if foreground else 0
    layout_handle = int(user32.GetKeyboardLayout(thread_id) or 0) & 0xFFFFFFFF
    buf = ctypes.create_unicode_buffer(9)
    klid = buf.value if user32.GetKeyboardLayoutNameW(buf) else None
    window = find_window(window_id=foreground) if foreground else None
    return {
        "foreground_window": window,
        "keyboard_layout": {
            "hkl": f"0x{layout_handle:08X}",
            "klid": klid,
        },
    }
