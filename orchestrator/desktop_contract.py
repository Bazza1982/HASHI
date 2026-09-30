"""Client-neutral, bounded manual desktop contract (Functions; no model calls)."""
from __future__ import annotations

import math
import re
from collections.abc import Mapping

VERSION = 1
ACTIONS = frozenset({"desktop_info", "desktop_frame", "desktop_view", "desktop_control", "desktop_input", "desktop_close"})
PIN_FIELDS = ("instance_id", "capability_id", "device_id", "user_session_id", "worker_generation")
MAX_FRAME_BYTES = 512 * 1024
MAX_TEXT = 4096
CONTROL_TTL = 8.0
SESSION_TTL = 60.0
ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}$")


class DesktopError(ValueError):
    def __init__(self, code: str, status: int = 400):
        super().__init__(code)
        self.code, self.status = code, status


def identifier(value, field="id") -> str:
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise DesktopError(f"desktop_invalid_{field}")
    return value


def fields(value, allowed, required=()) -> dict:
    if not isinstance(value, Mapping) or set(value) - set(allowed) or set(required) - set(value):
        raise DesktopError("desktop_invalid_fields")
    return dict(value)


def integer(value, low, high) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise DesktopError("desktop_invalid_number")
    return value


def unit(value) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 1:
        raise DesktopError("desktop_invalid_coordinate")
    return float(value)


def view_options(value) -> dict:
    v = fields(value, {"display_id", "small", "crop"}, {"display_id"})
    if not isinstance(v["display_id"], str) or not 1 <= len(v["display_id"]) <= 128:
        raise DesktopError("desktop_invalid_display")
    if "small" in v and not isinstance(v["small"], bool):
        raise DesktopError("desktop_invalid_size")
    crop = v.get("crop") or {"x": 0, "y": 0, "width": 1, "height": 1}
    crop = fields(crop, {"x", "y", "width", "height"}, {"x", "y", "width", "height"})
    crop = {k: unit(n) for k, n in crop.items()}
    if crop["width"] < .05 or crop["height"] < .05 or crop["x"] + crop["width"] > 1.000001 or crop["y"] + crop["height"] > 1.000001:
        raise DesktopError("desktop_invalid_crop")
    return {"display_id": v["display_id"], "small": v.get("small", False), "crop": crop}


def validate_input(value) -> dict:
    v = fields(value, {"seq", "kind", "x", "y", "button", "key", "text", "delta", "horizontal", "frame_id", "view_revision"}, {"seq", "kind"})
    integer(v["seq"], 1, 2**53 - 1)
    kind = v["kind"]
    if kind not in {"move", "down", "up", "wheel", "key_down", "key_up", "text", "reset"}:
        raise DesktopError("desktop_invalid_input")
    if kind in {"move", "down", "wheel"}:
        v["x"], v["y"] = unit(v.get("x")), unit(v.get("y"))
    if kind in {"down", "up"} and v.get("button") not in {"left", "right", "middle"}:
        raise DesktopError("desktop_invalid_button")
    if kind in {"key_down", "key_up"}:
        key = v.get("key")
        if not isinstance(key, str) or not re.fullmatch(r"(?:Key[A-Z]|Digit[0-9]|F(?:[1-9]|1[0-2])|Enter|Tab|Escape|Backspace|Delete|Space|Arrow(?:Up|Down|Left|Right)|Home|End|PageUp|PageDown|Insert|ShiftLeft|ShiftRight|ControlLeft|ControlRight|AltLeft|AltRight|MetaLeft|MetaRight)", key):
            raise DesktopError("desktop_invalid_key")
    if kind == "text":
        text = v.get("text")
        if not isinstance(text, str) or not 1 <= len(text) <= MAX_TEXT or "\0" in text or any(0xD800 <= ord(c) <= 0xDFFF for c in text):
            raise DesktopError("desktop_invalid_text")
    if kind == "wheel":
        integer(v.get("delta"), -1200, 1200)
        if not isinstance(v.get("horizontal", False), bool):
            raise DesktopError("desktop_invalid_wheel")
    # Releases always remain possible, including with an expired frame.
    if kind not in {"reset", "up", "key_up"}:
        identifier(v.get("frame_id"), "frame")
        identifier(v.get("view_revision"), "view_revision")
    return v
