"""Bounded GDI screen capture. No files, GPU encoder, subprocesses or audio."""
from __future__ import annotations

import ctypes
import hashlib
import io
import json
import os
import threading
from contextlib import contextmanager
from ctypes import wintypes as W

from orchestrator.desktop_contract import DesktopError, MAX_FRAME_BYTES


def fit_size(width, height, small=False):
    maximum = (1280, 720) if small else (1600, 900)
    scale = min(1, maximum[0] / width, maximum[1] / height)
    return max(1, round(width * scale)), max(1, round(height * scale))


def jpeg_bytes(image, max_bytes=MAX_FRAME_BYTES):
    """Bounded work: at most four encodes, never loop until a byte target fits."""
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or not 0 < max_bytes <= MAX_FRAME_BYTES:
        raise DesktopError("desktop_invalid_size")
    if max_bytes < MAX_FRAME_BYTES:
        # Motion-first frames trade chroma detail for a smaller, predictable payload.
        for quality in (70, 55):
            out = io.BytesIO()
            image.save(out, format="JPEG", quality=quality, subsampling=2, optimize=False)
            if out.tell() <= max_bytes:
                return out.getvalue()
        for divisor, quality in ((4, 50), (2, 40)):
            scaled = image.resize((max(1, image.width * (divisor - 1) // divisor),
                                   max(1, image.height * (divisor - 1) // divisor)))
            out = io.BytesIO()
            scaled.save(out, format="JPEG", quality=quality, subsampling=2, optimize=False)
            if out.tell() <= max_bytes:
                return out.getvalue()
        raise DesktopError("desktop_frame_too_large", 413)
    for quality in (75, 60, 45):
        out = io.BytesIO()
        image.save(out, format="JPEG", quality=quality, subsampling=0, optimize=False)
        data = out.getvalue()
        if len(data) <= max_bytes:
            return data
    image = image.resize((max(1, image.width * 3 // 4), max(1, image.height * 3 // 4)))
    out = io.BytesIO()
    image.save(out, format="JPEG", quality=55, optimize=False)
    if out.tell() > max_bytes:
        raise DesktopError("desktop_frame_too_large", 413)
    return out.getvalue()


class NativeDesktop:
    def __init__(self):
        if os.name != "nt":
            raise DesktopError("desktop_platform_unsupported", 501)
        from . import win32
        self.win32 = win32
        self.u = win32.user32
        self.g = ctypes.WinDLL("gdi32", use_last_error=True)
        # Explicit pointer-sized ABI on Win64. Never rely on ctypes int defaults.
        signatures = [
            (self.u, "GetDC", [W.HWND], W.HDC),
            (self.u, "ReleaseDC", [W.HWND, W.HDC], ctypes.c_int),
            (self.u, "SetThreadDpiAwarenessContext", [ctypes.c_void_p], ctypes.c_void_p),
            (self.g, "CreateCompatibleDC", [W.HDC], W.HDC),
            (self.g, "DeleteDC", [W.HDC], W.BOOL),
            (self.g, "DeleteObject", [W.HGDIOBJ], W.BOOL),
            (self.g, "SelectObject", [W.HDC, W.HGDIOBJ], W.HGDIOBJ),
            (self.g, "SetStretchBltMode", [W.HDC, ctypes.c_int], ctypes.c_int),
            (self.g, "SetBrushOrgEx", [W.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_void_p], W.BOOL),
            (self.g, "StretchBlt", [W.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, W.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, W.DWORD], W.BOOL),
            (self.g, "CreateDIBSection", [W.HDC, ctypes.c_void_p, W.UINT, ctypes.POINTER(ctypes.c_void_p), W.HANDLE, W.DWORD], W.HBITMAP),
            (self.g, "GdiFlush", [], W.BOOL),
        ]
        for lib, name, args, result in signatures:
            fn = getattr(lib, name)
            fn.argtypes, fn.restype = args, result
        self.keys, self.buttons = set(), set()
        self.metadata_lock = threading.RLock()

    @contextmanager
    def dpi(self):
        previous = self.u.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
        if not previous:
            raise DesktopError("desktop_dpi_unavailable", 503)
        try:
            yield
        finally:
            self.u.SetThreadDpiAwarenessContext(previous)

    def available(self):
        if not self.win32.get_desktop_state().get("interactive"):
            raise DesktopError("desktop_locked", 423)

    def displays(self):
        # ctypes callback signatures are installed for this call; serialize
        # metadata calls from frame and input HTTP threads.
        with self.metadata_lock:
            return self._displays()

    def _displays(self):
        self.available()
        class Info(ctypes.Structure):
            _fields_ = [("cbSize", W.DWORD), ("rcMonitor", W.RECT), ("rcWork", W.RECT), ("dwFlags", W.DWORD), ("szDevice", W.WCHAR * 32)]
        callback_type = ctypes.WINFUNCTYPE(W.BOOL, W.HANDLE, W.HDC, ctypes.POINTER(W.RECT), W.LPARAM)
        self.u.EnumDisplayMonitors.argtypes = [W.HDC, ctypes.POINTER(W.RECT), callback_type, W.LPARAM]
        self.u.EnumDisplayMonitors.restype = W.BOOL
        self.u.GetMonitorInfoW.argtypes = [W.HANDLE, ctypes.POINTER(Info)]
        self.u.GetMonitorInfoW.restype = W.BOOL
        rows, failed = [], []
        @callback_type
        def collect(handle, _dc, _rect, _param):
            info = Info(); info.cbSize = ctypes.sizeof(info)
            if not self.u.GetMonitorInfoW(handle, ctypes.byref(info)):
                failed.append(True); return False
            r = info.rcMonitor
            rows.append({"id": info.szDevice, "x": r.left, "y": r.top, "width": r.right-r.left, "height": r.bottom-r.top, "primary": bool(info.dwFlags & 1)})
            return True
        with self.dpi():
            if not self.u.EnumDisplayMonitors(None, None, collect, 0) or failed or not rows:
                raise DesktopError("desktop_displays_unavailable", 503)
        rows.sort(key=lambda d: (not d["primary"], d["id"]))
        revision = hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()[:24]
        return rows, revision

    def cursor(self):
        with self.metadata_lock:
            return self._cursor()

    def _cursor(self):
        class Cursor(ctypes.Structure):
            _fields_ = [("cbSize", W.DWORD), ("flags", W.DWORD), ("hCursor", W.HANDLE), ("ptScreenPos", self.win32.POINT)]
        self.u.GetCursorInfo.argtypes = [ctypes.POINTER(Cursor)]
        self.u.GetCursorInfo.restype = W.BOOL
        value = Cursor(); value.cbSize = ctypes.sizeof(value)
        with self.dpi():
            if not self.u.GetCursorInfo(ctypes.byref(value)):
                return {"visible": False}
        return {"x": value.ptScreenPos.x, "y": value.ptScreenPos.y, "visible": bool(value.flags & 1)}

    def capture(self, rect, size):
        from PIL import Image
        self.available()
        class Header(ctypes.Structure):
            _fields_ = [("size", W.DWORD), ("width", W.LONG), ("height", W.LONG), ("planes", W.WORD), ("bits", W.WORD), ("compression", W.DWORD), ("image_size", W.DWORD), ("xp", W.LONG), ("yp", W.LONG), ("used", W.DWORD), ("important", W.DWORD)]
        width, height = size
        if not 0 < width <= 1600 or not 0 < height <= 900:
            raise DesktopError("desktop_invalid_size")
        header = Header(ctypes.sizeof(Header), width, -height, 1, 32, 0, 0, 0, 0, 0, 0)
        screen = memory = bitmap = old = None
        with self.dpi():
            try:
                screen = self.u.GetDC(None)
                if not screen: raise ctypes.WinError(ctypes.get_last_error())
                memory = self.g.CreateCompatibleDC(screen)
                if not memory: raise ctypes.WinError(ctypes.get_last_error())
                bits = ctypes.c_void_p()
                bitmap = self.g.CreateDIBSection(screen, ctypes.byref(header), 0, ctypes.byref(bits), None, 0)
                if not bitmap or not bits.value: raise ctypes.WinError(ctypes.get_last_error())
                old = self.g.SelectObject(memory, bitmap)
                if not old or old == ctypes.c_void_p(-1).value: raise ctypes.WinError(ctypes.get_last_error())
                self.g.SetStretchBltMode(memory, 4)  # HALFTONE; preserve small text.
                self.g.SetBrushOrgEx(memory, 0, 0, None)
                if not self.g.StretchBlt(memory, 0, 0, width, height, screen, rect["x"], rect["y"], rect["width"], rect["height"], 0x00CC0020 | 0x40000000):
                    raise ctypes.WinError(ctypes.get_last_error())
                self.g.GdiFlush()
                raw = ctypes.string_at(bits, width * height * 4)
                return Image.frombytes("RGB", (width, height), raw, "raw", "BGRX")
            finally:
                if old and memory: self.g.SelectObject(memory, old)
                if bitmap: self.g.DeleteObject(bitmap)
                if memory: self.g.DeleteDC(memory)
                if screen: self.u.ReleaseDC(None, screen)

    def inject(self, event):
        """Input is called under the controller lock; only its keys are released."""
        w, kind = self.win32, event["kind"]
        if kind == "reset":
            return self.reset()
        self.available()
        with self.dpi():
            if kind in {"move", "down", "wheel"}:
                if not self.u.SetCursorPos(event["px"], event["py"]):
                    raise ctypes.WinError(ctypes.get_last_error())
            if kind in {"down", "up"}:
                button = event["button"]
                if kind == "up" and button not in self.buttons: return
                flag = {"left": (0x2, 0x4), "right": (0x8, 0x10), "middle": (0x20, 0x40)}[button][kind == "up"]
                if kind == "down": self.buttons.add(button)
                w._send_inputs([w.INPUT(type=w.INPUT_MOUSE, mi=w.MOUSEINPUT(0, 0, 0, flag, 0, 0))])
                if kind == "up": self.buttons.discard(button)
            elif kind == "wheel":
                flag = 0x1000 if event.get("horizontal") else 0x800
                w._send_inputs([w.INPUT(type=w.INPUT_MOUSE, mi=w.MOUSEINPUT(0, 0, event["delta"] & 0xFFFFFFFF, flag, 0, 0))])
            elif kind in {"key_down", "key_up"}:
                key = event["key"]
                if kind == "key_up" and key not in self.keys: return
                vk, extended = key_code(key)
                if kind == "key_down": self.keys.add(key)
                w._send_inputs([w._keyboard_input(vk=vk, flags=(1 if extended else 0) | (2 if kind == "key_up" else 0))])
                if kind == "key_up": self.keys.discard(key)
            elif kind == "text":
                # Unicode code units support supplementary characters. No clipboard.
                data = event["text"].replace("\r\n", "\n").replace("\r", "\n").encode("utf-16-le")
                items = []
                for i in range(0, len(data), 2):
                    code = int.from_bytes(data[i:i+2], "little")
                    if code in {9, 10}:
                        vk = 9 if code == 9 else 13
                        items.extend([w._keyboard_input(vk=vk), w._keyboard_input(vk=vk, flags=2)])
                    else:
                        items.extend([w._keyboard_input(scan=code, flags=4), w._keyboard_input(scan=code, flags=6)])
                for i in range(0, len(items), 128): w._send_inputs(items[i:i+128])

    def reset(self):
        # Never call legacy reset_input_state(), which releases unowned input.
        for key in tuple(self.keys):
            vk, extended = key_code(key)
            self.win32._send_inputs([self.win32._keyboard_input(vk=vk, flags=2 | (1 if extended else 0))])
            self.keys.discard(key)
        for button in tuple(self.buttons):
            flag = {"left": 4, "right": 16, "middle": 64}[button]
            w = self.win32
            w._send_inputs([w.INPUT(type=w.INPUT_MOUSE, mi=w.MOUSEINPUT(0, 0, 0, flag, 0, 0))])
            self.buttons.discard(button)


def key_code(code):
    if code.startswith("Key"): return ord(code[-1]), False
    if code.startswith("Digit"): return ord(code[-1]), False
    if code.startswith("F") and code[1:].isdigit(): return 111 + int(code[1:]), False
    keys = {"Enter":13,"Tab":9,"Escape":27,"Backspace":8,"Delete":46,"Space":32,"ArrowUp":38,"ArrowDown":40,"ArrowLeft":37,"ArrowRight":39,"Home":36,"End":35,"PageUp":33,"PageDown":34,"Insert":45,"ShiftLeft":160,"ShiftRight":161,"ControlLeft":162,"ControlRight":163,"AltLeft":164,"AltRight":165,"MetaLeft":91,"MetaRight":92}
    return keys[code], code in {"Delete","Insert","Home","End","PageUp","PageDown","ArrowUp","ArrowDown","ArrowLeft","ArrowRight","ControlRight","AltRight","MetaLeft","MetaRight"}
