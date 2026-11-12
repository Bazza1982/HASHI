"""A desktop label cannot veto an otherwise successful native frame."""
from __future__ import annotations

import base64
import io
from types import SimpleNamespace

import pytest
from PIL import Image

from orchestrator.desktop_contract import DesktopError
from tools.desktop_session import DesktopController
from tools.windows_helper.desktop_capture import NativeDesktop


class CapturableDesktop(NativeDesktop):
    def __init__(self):
        # Arrange the native result, while keeping the real admission method.
        self.win32 = SimpleNamespace(get_desktop_state=lambda: {
            "available": True, "interactive": False, "locked": True,
            "desktop_name": "Winlogon",
        })
        self.captures = 0

    def displays(self):
        self.available()
        return [{"id": "screen", "x": 0, "y": 0, "width": 160,
                 "height": 90, "primary": True}], "layout"

    def desktop_state(self):
        return {"available": True, "locked": True, "secure_desktop": True}

    def capture(self, rect, size):
        self.available()
        self.captures += 1
        return Image.new("RGB", size, (30, 90, 180))

    def cursor(self):
        return {"visible": False}

    def reset(self):
        return None


def test_native_success_is_projected_as_a_real_frame_despite_locked_label():
    native = CapturableDesktop()
    controller = DesktopController(native, lambda: None, watchdog=False)
    try:
        controller.handle("desktop_info", {}, "owner", "session")
        frame = controller.frame("", "session")
        assert native.captures == 1
        assert frame["meta"]["windows_locked"] is True
        with Image.open(io.BytesIO(base64.b64decode(frame["jpeg"], validate=True))) as image:
            assert image.size == (160, 90)
            assert all(abs(a - b) < 5 for a, b in zip(image.getpixel((30, 30)), (30, 90, 180)))
    finally:
        controller.close()


def test_actual_native_capture_failure_is_not_converted_to_success():
    native = CapturableDesktop()
    def fail_capture(_rect, _size):
        raise DesktopError("desktop_capture_failed", 503)
    native.capture = fail_capture
    controller = DesktopController(native, lambda: None, watchdog=False)
    try:
        controller.handle("desktop_info", {}, "owner", "session")
        with pytest.raises(DesktopError, match="desktop_capture_failed"):
            controller.frame("", "session")
        assert controller.sessions["session"].frame_cache is None
    finally:
        controller.close()
# HASHI-Fingerprint: hfp_cc9510091c9149cbbcc06f365daafc2b
