"""Native desktop text routing with a fake USER32 boundary, never OS input."""
from contextlib import nullcontext
import ctypes
from types import SimpleNamespace

import pytest

from orchestrator.desktop_contract import DesktopError
from tools.windows_helper.desktop_capture import NativeDesktop


class Function:
    def __init__(self, call):
        self.call = call

    def __call__(self, *args):
        return self.call(*args)


def desktop(*, name="RichEditD2DPT", readonly=False, changed=False, timeout=False):
    calls = []
    focus_queries = 0

    def gui(thread, pointer):
        nonlocal focus_queries
        focus_queries += 1
        pointer._obj.focus = 202 if changed and focus_queries > 1 else 201
        return 1

    def process(window, pointer):
        pointer._obj.value = 301
        return 101

    def class_name(window, buffer, size):
        buffer.value = name
        return len(name)

    def replace(window, message, undo, text, flags, wait, result):
        calls.append(("replace", window, message, undo, text, flags, wait))
        return 0 if timeout else 1

    target = object.__new__(NativeDesktop)
    target.u = SimpleNamespace(**{
        name: Function(call) for name, call in {
            "GetGUIThreadInfo": gui, "GetWindowThreadProcessId": process,
            "GetClassNameW": class_name, "GetWindowLongW": lambda *args: 0x800 if readonly else 0,
            "IsWindowUnicode": lambda *args: True, "GetForegroundWindow": lambda: 102,
            "SendMessageTimeoutW": replace,
        }.items()
    })
    target.available = lambda: None
    target.dpi = nullcontext
    target.win32 = SimpleNamespace(
        _keyboard_input=lambda **args: args,
        _send_inputs=lambda inputs: calls.append(("keys", inputs)),
    )
    return target, calls


def test_modern_richedit_replaces_unicode_selection_once_without_keys():
    target, calls = desktop()
    text = "HASHI desktop recovery verified 2026-11-09\r\n中文🌸\tlast"
    target.inject({"kind": "text", "text": text})
    assert calls == [("replace", 201, 0xC2, 1, text.replace("\r\n", "\r"), 0x23, 500)]


@pytest.mark.parametrize("options", [{"name": "Chrome_RenderWidgetHostHWND"}, {"readonly": True}])
def test_other_or_readonly_controls_keep_existing_keyboard_semantics(options):
    target, calls = desktop(**options)
    target.inject({"kind": "text", "text": "🌸\n\t"})
    assert len(calls) == 1 and calls[0][0] == "keys"
    assert calls[0][1] == [
        {"scan": 0xD83C, "flags": 4}, {"scan": 0xD83C, "flags": 6},
        {"scan": 0xDF38, "flags": 4}, {"scan": 0xDF38, "flags": 6},
        {"vk": 13}, {"vk": 13, "flags": 2},
        {"vk": 9}, {"vk": 9, "flags": 2},
    ]


def test_changed_focus_rejects_before_edit_or_keyboard_fallback():
    target, calls = desktop(changed=True)
    with pytest.raises(DesktopError, match="desktop_target_changed"):
        target.inject({"kind": "text", "text": "not dispatched"})
    assert calls == []


def test_uncertain_native_timeout_never_falls_back_or_replays():
    target, calls = desktop(timeout=True)
    with pytest.raises(DesktopError, match="desktop_input_failed"):
        target.inject({"kind": "text", "text": "once"})
    assert len(calls) == 1 and calls[0][0] == "replace"
# HASHI-Fingerprint: hfp_6931885cf8994e11996cda593ec73754
