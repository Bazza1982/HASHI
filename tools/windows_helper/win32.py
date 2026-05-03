from __future__ import annotations

import ctypes
import math
import time
from contextlib import contextmanager
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
ULONG_PTR = getattr(wintypes, "ULONG_PTR", ctypes.c_size_t)
HGLOBAL = getattr(wintypes, "HGLOBAL", wintypes.HANDLE)

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
user32.keybd_event.argtypes = [ctypes.c_ubyte, ctypes.c_ubyte, wintypes.DWORD, ULONG_PTR]
user32.keybd_event.restype = None
user32.mouse_event.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ULONG_PTR]
user32.mouse_event.restype = None
user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
user32.SetCursorPos.restype = wintypes.BOOL
user32.GetCursorPos.argtypes = [ctypes.c_void_p]
user32.GetCursorPos.restype = wintypes.BOOL
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.GetAsyncKeyState.restype = ctypes.c_short
user32.MapVirtualKeyW.argtypes = [wintypes.UINT, wintypes.UINT]
user32.MapVirtualKeyW.restype = wintypes.UINT
user32.VkKeyScanW.argtypes = [wintypes.WCHAR]
user32.VkKeyScanW.restype = ctypes.c_short
user32.OpenClipboard.argtypes = [wintypes.HWND]
user32.OpenClipboard.restype = wintypes.BOOL
user32.CloseClipboard.argtypes = []
user32.CloseClipboard.restype = wintypes.BOOL
user32.EmptyClipboard.argtypes = []
user32.EmptyClipboard.restype = wintypes.BOOL
user32.GetClipboardData.argtypes = [wintypes.UINT]
user32.GetClipboardData.restype = HGLOBAL
user32.SetClipboardData.argtypes = [wintypes.UINT, HGLOBAL]
user32.SetClipboardData.restype = HGLOBAL
kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
kernel32.GlobalAlloc.restype = HGLOBAL
kernel32.GlobalLock.argtypes = [HGLOBAL]
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalUnlock.argtypes = [HGLOBAL]
kernel32.GlobalUnlock.restype = wintypes.BOOL

SW_RESTORE = 9
SW_SHOW = 5
WM_CLOSE = 0x0010
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_HWHEEL = 0x01000
MAPVK_VK_TO_VSC = 0
WHEEL_DELTA = 120
CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002
GMEM_ZEROINIT = 0x0040
VK_LBUTTON = 0x01
VK_RBUTTON = 0x02
VK_MBUTTON = 0x04

INPUT_MOUSE = 0
INPUT_KEYBOARD = 1


class POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class INPUT_UNION(ctypes.Union):
    _fields_ = [
        ("mi", MOUSEINPUT),
        ("ki", KEYBDINPUT),
        ("hi", HARDWAREINPUT),
    ]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [
        ("type", wintypes.DWORD),
        ("u", INPUT_UNION),
    ]


user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
user32.SendInput.restype = wintypes.UINT

VK_ALIASES = {
    "shift": 0x10,
    "ctrl": 0x11,
    "control": 0x11,
    "alt": 0x12,
    "win": 0x5B,
    "meta": 0x5B,
    "cmd": 0x5B,
    "enter": 0x0D,
    "return": 0x0D,
    "tab": 0x09,
    "esc": 0x1B,
    "escape": 0x1B,
    "space": 0x20,
    "backspace": 0x08,
    "delete": 0x2E,
    "del": 0x2E,
    "insert": 0x2D,
    "home": 0x24,
    "end": 0x23,
    "pageup": 0x21,
    "pagedown": 0x22,
    "up": 0x26,
    "down": 0x28,
    "left": 0x25,
    "right": 0x27,
}


def _window_title(hwnd: int) -> str:
    length = user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buffer = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buffer, len(buffer))
    return buffer.value.strip()


def _keyboard_input(vk: int = 0, scan: int = 0, flags: int = 0) -> INPUT:
    return INPUT(
        type=INPUT_KEYBOARD,
        ki=KEYBDINPUT(
            wVk=vk,
            wScan=scan,
            dwFlags=flags,
            time=0,
            dwExtraInfo=0,
        ),
    )


def _mouse_input(flags: int, data: int = 0) -> INPUT:
    return INPUT(
        type=INPUT_MOUSE,
        mi=MOUSEINPUT(
            dx=0,
            dy=0,
            mouseData=data,
            dwFlags=flags,
            time=0,
            dwExtraInfo=0,
        ),
    )


def _send_inputs(inputs: list[INPUT]) -> None:
    if not inputs:
        return
    array = (INPUT * len(inputs))(*inputs)
    sent = user32.SendInput(len(inputs), array, ctypes.sizeof(INPUT))
    if sent != len(inputs):
        raise ctypes.WinError(ctypes.get_last_error())


@contextmanager
def _open_clipboard(retries: int = 8, delay: float = 0.025):
    opened = False
    for _ in range(max(1, retries)):
        if user32.OpenClipboard(None):
            opened = True
            break
        time.sleep(delay)
    if not opened:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        yield
    finally:
        user32.CloseClipboard()


def _get_clipboard_unicode_text() -> str | None:
    with _open_clipboard():
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return None
        ptr = kernel32.GlobalLock(handle)
        if not ptr:
            return None
        try:
            return ctypes.wstring_at(ptr)
        finally:
            kernel32.GlobalUnlock(handle)


def _set_clipboard_unicode_text(text: str) -> None:
    data = (text + "\0").encode("utf-16-le")
    handle = kernel32.GlobalAlloc(GMEM_MOVEABLE | GMEM_ZEROINIT, len(data))
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    ptr = kernel32.GlobalLock(handle)
    if not ptr:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        ctypes.memmove(ptr, data, len(data))
    finally:
        kernel32.GlobalUnlock(handle)
    with _open_clipboard():
        if not user32.EmptyClipboard():
            raise ctypes.WinError(ctypes.get_last_error())
        if not user32.SetClipboardData(CF_UNICODETEXT, handle):
            raise ctypes.WinError(ctypes.get_last_error())


def _vk_for_token(token: str) -> int:
    lowered = token.strip().lower()
    if lowered in VK_ALIASES:
        return VK_ALIASES[lowered]
    if len(lowered) == 2 and lowered.startswith("f") and lowered[1].isdigit():
        return 0x70 + int(lowered[1]) - 1
    if len(lowered) == 3 and lowered.startswith("f") and lowered[1:].isdigit():
        number = int(lowered[1:])
        if 1 <= number <= 24:
            return 0x70 + number - 1
    if len(token) == 1:
        mapped = user32.VkKeyScanW(token)
        if mapped == -1:
            raise ValueError(f"unsupported key token: {token}")
        return mapped & 0xFF
    raise ValueError(f"unsupported key token: {token}")


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


def _is_key_down(vk: int) -> bool:
    return bool(user32.GetAsyncKeyState(vk) & 0x8000)


def reset_input_state(*, release_mouse: bool = True) -> dict:
    released_keys = []
    for vk, name in ((0x10, "SHIFT"), (0x11, "CTRL"), (0x12, "ALT"), (0x5B, "LWIN"), (0x5C, "RWIN")):
        user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)
        released_keys.append(name)
    released_mouse = []
    if release_mouse:
        for vk, flag, name in (
            (VK_LBUTTON, MOUSEEVENTF_LEFTUP, "left"),
            (VK_RBUTTON, MOUSEEVENTF_RIGHTUP, "right"),
            (VK_MBUTTON, MOUSEEVENTF_MIDDLEUP, "middle"),
        ):
            if _is_key_down(vk):
                user32.mouse_event(flag, 0, 0, 0, 0)
                released_mouse.append(name)
    return {
        "ok": True,
        "released_keys": released_keys,
        "released_mouse": released_mouse,
    }


def get_cursor_position() -> dict:
    point = POINT()
    if not user32.GetCursorPos(ctypes.byref(point)):
        raise ctypes.WinError(ctypes.get_last_error())
    return {"x": int(point.x), "y": int(point.y)}


def move_mouse(x: int, y: int) -> dict:
    if not user32.SetCursorPos(int(x), int(y)):
        raise ctypes.WinError(ctypes.get_last_error())
    return get_cursor_position()


def click_mouse(x: int, y: int, button: str = "left", count: int = 1) -> dict:
    reset_input_state(release_mouse=True)
    move_mouse(x, y)
    button_value = button.strip().lower()
    flag_map = {
        "left": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
        "right": (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
        "middle": (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP),
    }
    if button_value not in flag_map:
        raise ValueError(f"unsupported mouse button: {button}")
    down_flag, up_flag = flag_map[button_value]
    inputs: list[INPUT] = []
    for _ in range(max(1, int(count))):
        inputs.append(_mouse_input(down_flag))
        inputs.append(_mouse_input(up_flag))
    _send_inputs(inputs)
    time.sleep(0.025)
    return {"x": int(x), "y": int(y), "button": button_value, "count": max(1, int(count))}


def drag_mouse(
    from_x: int,
    from_y: int,
    to_x: int,
    to_y: int,
    *,
    button: str = "left",
    curve_x: int | None = None,
    curve_y: int | None = None,
) -> dict:
    button_value = button.strip().lower()
    flag_map = {
        "left": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
        "right": (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
        "middle": (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP),
    }
    if button_value not in flag_map:
        raise ValueError(f"unsupported mouse button: {button}")

    move_mouse(from_x, from_y)
    down_flag, up_flag = flag_map[button_value]
    _send_inputs([_mouse_input(down_flag)])
    time.sleep(0.03)

    start = (float(from_x), float(from_y))
    end = (float(to_x), float(to_y))
    control = (
        (float(curve_x), float(curve_y))
        if curve_x is not None and curve_y is not None
        else None
    )
    distance = math.hypot(end[0] - start[0], end[1] - start[1])
    steps = max(8, min(120, int(distance / 8) or 8))

    for i in range(1, steps + 1):
        t = i / steps
        if control is None:
            x = start[0] + (end[0] - start[0]) * t
            y = start[1] + (end[1] - start[1]) * t
        else:
            one_minus_t = 1.0 - t
            x = (
                one_minus_t * one_minus_t * start[0]
                + 2.0 * one_minus_t * t * control[0]
                + t * t * end[0]
            )
            y = (
                one_minus_t * one_minus_t * start[1]
                + 2.0 * one_minus_t * t * control[1]
                + t * t * end[1]
            )
        move_mouse(int(round(x)), int(round(y)))
        time.sleep(0.005)

    time.sleep(0.03)
    _send_inputs([_mouse_input(up_flag)])
    return {
        "from": {"x": int(from_x), "y": int(from_y)},
        "to": {"x": int(to_x), "y": int(to_y)},
        "button": button_value,
        "curve": (
            {"x": int(curve_x), "y": int(curve_y)}
            if curve_x is not None and curve_y is not None
            else None
        ),
    }


def scroll_mouse(direction: str = "down", amount: int = 1, *, horizontal: bool = False) -> dict:
    direction_value = direction.strip().lower()
    step = WHEEL_DELTA * max(1, int(amount))
    if direction_value in {"down", "right"}:
        step = -step
    elif direction_value not in {"up", "left"}:
        raise ValueError(f"unsupported scroll direction: {direction}")
    flag = MOUSEEVENTF_HWHEEL if horizontal else MOUSEEVENTF_WHEEL
    _send_inputs([_mouse_input(flag, step)])
    return {"direction": direction_value, "amount": max(1, int(amount)), "horizontal": horizontal}


def press_key_combo(shortcut: str) -> dict:
    tokens = [token.strip() for token in shortcut.split("+") if token.strip()]
    if not tokens:
        raise ValueError("shortcut must not be empty")
    modifier_vks = [_vk_for_token(token) for token in tokens[:-1]]
    main_vk = _vk_for_token(tokens[-1])
    inputs: list[INPUT] = []
    for vk in modifier_vks:
        inputs.append(_keyboard_input(vk=vk, scan=user32.MapVirtualKeyW(vk, MAPVK_VK_TO_VSC), flags=0))
    inputs.append(_keyboard_input(vk=main_vk, scan=user32.MapVirtualKeyW(main_vk, MAPVK_VK_TO_VSC), flags=0))
    inputs.append(_keyboard_input(vk=main_vk, scan=user32.MapVirtualKeyW(main_vk, MAPVK_VK_TO_VSC), flags=KEYEVENTF_KEYUP))
    for vk in reversed(modifier_vks):
        inputs.append(_keyboard_input(vk=vk, scan=user32.MapVirtualKeyW(vk, MAPVK_VK_TO_VSC), flags=KEYEVENTF_KEYUP))
    _send_inputs(inputs)
    return {"shortcut": shortcut}


def type_text(text: str) -> dict:
    if not text:
        return {"text_length": 0}
    inputs: list[INPUT] = []
    for char in text:
        codepoint = ord(char)
        inputs.append(_keyboard_input(scan=codepoint, flags=KEYEVENTF_UNICODE))
        inputs.append(_keyboard_input(scan=codepoint, flags=KEYEVENTF_UNICODE | KEYEVENTF_KEYUP))
    _send_inputs(inputs)
    return {"text_length": len(text)}


def paste_text(text: str, *, restore_clipboard: bool = True) -> dict:
    if not text:
        return {"text_length": 0, "method": "clipboard_paste"}
    previous = _get_clipboard_unicode_text() if restore_clipboard else None
    _set_clipboard_unicode_text(text)
    time.sleep(0.08)
    press_key_combo("ctrl+v")
    time.sleep(max(0.35, min(1.25, len(text) / 1200)))
    if restore_clipboard and previous is not None:
        _set_clipboard_unicode_text(previous)
    return {"text_length": len(text), "method": "clipboard_paste"}


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
