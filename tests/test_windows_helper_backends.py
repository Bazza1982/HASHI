from __future__ import annotations

import ctypes
import json
import sys
import types
from pathlib import Path

import pytest

pytestmark = pytest.mark.platform

if not hasattr(ctypes, "WinDLL"):
    pytest.skip("Windows helper native backend tests require ctypes.WinDLL", allow_module_level=True)


async def _unused_windows_mcp(payload: dict) -> dict:
    return {"content": []}


sys.modules.setdefault(
    "tools.windows_use_mcp_client",
    types.SimpleNamespace(_run=_unused_windows_mcp),
)

from tools.windows_helper import backends  # noqa: E402


@pytest.mark.asyncio
async def test_helper_window_focus_can_maximize(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []
    target = {"id": 458844, "pid": 6132, "title": "YouTube - Google Chrome"}

    monkeypatch.setattr(backends.win32, "find_window", lambda **kwargs: target)

    def fake_focus(window: dict, *, maximize: bool = False) -> dict:
        calls.append((window, maximize))
        return {**window, "maximized": maximize}

    monkeypatch.setattr(backends.win32, "focus_window", fake_focus)

    result = await backends.execute_action(
        "window_focus",
        {"window_id": 458844, "maximize": True},
    )

    assert calls == [(target, True)]
    assert result == (
        "Focused and maximized window id=458844 "
        "title=YouTube - Google Chrome"
    )


@pytest.mark.asyncio
async def test_helper_drag_uses_native_win32_when_available(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    monkeypatch.setattr(backends.win32, "drag_mouse", lambda *args, **kwargs: calls.append((args, kwargs)) or {})
    monkeypatch.setattr(backends.win32, "reset_input_state", lambda **kwargs: {"ok": True})

    result = await backends.execute_action(
        "drag",
        {"from_x": 10, "from_y": 20, "to_x": 30, "to_y": 40},
    )

    assert "helper-native" in result
    assert calls


@pytest.mark.asyncio
async def test_helper_click_resets_input_state_before_native_click(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    monkeypatch.setattr(backends.win32, "reset_input_state", lambda **kwargs: calls.append(("reset", kwargs)) or {"ok": True})
    monkeypatch.setattr(
        backends.win32,
        "click_mouse",
        lambda *args, **kwargs: calls.append(("click", args, kwargs)) or {},
    )

    result = await backends.execute_action("click", {"x": 10, "y": 20, "button": "left"})

    assert "helper-native" in result
    assert calls[0] == ("reset", {"release_mouse": False})
    assert calls[1][0] == "click"
    assert calls[2] == ("reset", {"release_mouse": False})


@pytest.mark.asyncio
async def test_helper_type_clicks_coordinates_and_pastes_tabular_text(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    monkeypatch.setattr(backends.win32, "reset_input_state", lambda **kwargs: calls.append(("reset", kwargs)) or {"ok": True})
    monkeypatch.setattr(
        backends.win32,
        "click_mouse",
        lambda *args, **kwargs: calls.append(("click", args, kwargs)) or {},
    )
    monkeypatch.setattr(
        backends.win32,
        "paste_text",
        lambda text, **kwargs: calls.append(("paste", text, kwargs)) or {"text_length": len(text), "method": "clipboard_paste"},
    )
    monkeypatch.setattr(
        backends.win32,
        "type_text",
        lambda text: calls.append(("type", text)) or {"text_length": len(text)},
    )

    result = await backends.execute_action("type", {"x": 100, "y": 200, "text": "A\tB\n1\t2"})

    assert "helper-native (clipboard_paste)" in result
    assert calls[0] == ("reset", {"release_mouse": False})
    assert calls[1][0] == "click"
    assert calls[1][1] == (100, 200)
    assert calls[2] == ("paste", "A\tB\n1\t2", {"restore_clipboard": False})
    assert calls[3] == ("reset", {"release_mouse": False})


@pytest.mark.asyncio
async def test_helper_type_auto_uses_keys_for_plain_long_text(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    monkeypatch.setattr(backends.win32, "reset_input_state", lambda **kwargs: calls.append(("reset", kwargs)) or {"ok": True})
    monkeypatch.setattr(
        backends.win32,
        "paste_text",
        lambda text, **kwargs: calls.append(("paste", text, kwargs)) or {"text_length": len(text), "method": "clipboard_paste"},
    )
    monkeypatch.setattr(
        backends.win32,
        "type_text",
        lambda text: calls.append(("type", text)) or {"text_length": len(text)},
    )

    long_path = r"C:\Users\Print\HASHI\tmp\windows_usecomputer_eval_fixed\workspace\excel_regression.xlsx"
    result = await backends.execute_action("type", {"text": long_path})

    assert "helper-native (sendinput)" in result
    assert ("type", long_path) in calls
    assert not any(call[0] == "paste" for call in calls)


@pytest.mark.asyncio
async def test_helper_screenshot_falls_back_to_usecomputer_when_native_capture_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(backends.ImageGrab, "grab", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("screen grab failed")))
    monkeypatch.setattr(backends, "find_usecomputer", lambda: "usecomputer")

    async def fake_run(cmd: list[str], timeout: int = 30):
        shot_path = Path(cmd[2])
        shot_path.write_bytes(b"png-bytes")
        return 0, '{"ok":true}', ""

    monkeypatch.setattr(backends, "_run", fake_run)

    result = await backends.execute_action("screenshot", {"save_path": str(tmp_path / "shot.png")})

    assert "provider=helper" in result
    assert (tmp_path / "shot.png").read_bytes() == b"png-bytes"


@pytest.mark.asyncio
async def test_helper_key_falls_back_to_usecomputer_when_native_key_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        backends.win32,
        "press_key_combo",
        lambda key: (_ for _ in ()).throw(PermissionError("Access is denied")),
    )
    monkeypatch.setattr(backends.win32, "reset_input_state", lambda: {"ok": True})

    async def fake_run(cmd: list[str], timeout: int = 30):
        assert cmd[-2:] == ["press", "ctrl+s"]
        return 0, "", ""

    monkeypatch.setattr(backends, "_run", fake_run)
    monkeypatch.setattr(backends, "find_usecomputer", lambda: "usecomputer")

    result = await backends.execute_action("key", {"key": "ctrl+s"})

    assert "Pressed 'ctrl+s'" in result
    assert "helper-native" not in result


@pytest.mark.asyncio
async def test_persistent_info_uses_native_state_without_child_processes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def forbidden(*_args, **_kwargs):
        raise AssertionError("persistent worker must not launch a child process")

    monkeypatch.setattr(backends, "_run", forbidden)
    monkeypatch.setattr(backends, "_run_usecomputer", forbidden)
    monkeypatch.setattr(backends, "_mcp_text", forbidden)
    monkeypatch.setattr(backends.win32, "get_cursor_position", lambda: {"x": 12, "y": 34})
    monkeypatch.setattr(
        backends.win32,
        "get_display_info",
        lambda: {"count": 1, "virtual_screen": {"width": 1920, "height": 1080}},
    )
    monkeypatch.setattr(backends.win32, "list_windows", lambda: [])
    monkeypatch.setattr(backends.win32, "get_input_state", lambda: {"ok": True})

    result = json.loads(
        await backends.execute_action(
            "info",
            {"provider": "usecomputer", "_persistent_worker": True},
        )
    )

    assert result["provider"] == "helper-native"
    assert result["mouse_position"] == {"x": 12, "y": 34}
    assert result["displays"]["count"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "arguments", "native_name"),
    [
        ("mouse_move", {"x": 1, "y": 2}, "move_mouse"),
        ("click", {"x": 1, "y": 2}, "click_mouse"),
        (
            "drag",
            {"from_x": 1, "from_y": 2, "to_x": 3, "to_y": 4},
            "drag_mouse",
        ),
        ("type", {"text": "safe"}, "type_text"),
        ("key", {"key": "ctrl+s"}, "press_key_combo"),
        ("scroll", {"direction": "down", "amount": 1}, "scroll_mouse"),
    ],
)
async def test_persistent_native_failure_never_falls_back_to_cli(
    monkeypatch: pytest.MonkeyPatch,
    action: str,
    arguments: dict,
    native_name: str,
) -> None:
    async def forbidden(*_args, **_kwargs):
        raise AssertionError("persistent worker must not launch a child process")

    def native_failure(*_args, **_kwargs):
        raise PermissionError("native call rejected")

    monkeypatch.setattr(backends, "_run", forbidden)
    monkeypatch.setattr(backends, "_run_usecomputer", forbidden)
    monkeypatch.setattr(backends, "_mcp_text", forbidden)
    monkeypatch.setattr(backends.win32, "reset_input_state", lambda **_kwargs: {"ok": True})
    monkeypatch.setattr(backends.win32, native_name, native_failure)

    with pytest.raises(RuntimeError, match=rf"native {action} failed"):
        await backends.execute_action(
            action,
            {
                **arguments,
                "provider": "usecomputer",
                "_persistent_worker": True,
            },
        )


@pytest.mark.asyncio
async def test_persistent_screenshot_failure_never_falls_back_to_cli(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def forbidden(*_args, **_kwargs):
        raise AssertionError("persistent worker must not launch a child process")

    monkeypatch.setattr(backends, "_run", forbidden)
    monkeypatch.setattr(backends, "_run_usecomputer", forbidden)
    monkeypatch.setattr(backends, "_mcp_text", forbidden)
    monkeypatch.setattr(
        backends.ImageGrab,
        "grab",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("capture rejected")),
    )

    with pytest.raises(RuntimeError, match="native screenshot failed"):
        await backends.execute_action(
            "screenshot",
            {"provider": "usecomputer", "_persistent_worker": True},
        )


@pytest.mark.asyncio
async def test_fifty_persistent_actions_create_no_cli_children(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def forbidden(*_args, **_kwargs):
        raise AssertionError("persistent worker must not launch a child process")

    class FakeImage:
        def save(self, path: Path, *, format: str) -> None:
            assert format == "PNG"
            Path(path).write_bytes(b"fake-png")

    monkeypatch.setattr(backends, "_run", forbidden)
    monkeypatch.setattr(backends, "_run_usecomputer", forbidden)
    monkeypatch.setattr(backends, "_mcp_text", forbidden)
    monkeypatch.setattr(backends, "run_windows_mcp", forbidden)
    monkeypatch.setattr(backends.ImageGrab, "grab", lambda **_kwargs: FakeImage())
    monkeypatch.setattr(backends.win32, "reset_input_state", lambda **_kwargs: {"ok": True})
    monkeypatch.setattr(backends.win32, "move_mouse", lambda x, y: {"x": x, "y": y})
    monkeypatch.setattr(backends.win32, "click_mouse", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(backends.win32, "press_key_combo", lambda _key: {})
    monkeypatch.setattr(backends.win32, "list_windows", lambda: [])

    calls = []
    for index in range(10):
        calls.extend(
            [
                ("screenshot", {}),
                ("mouse_move", {"x": index, "y": index + 1}),
                ("click", {"x": index, "y": index + 1}),
                ("key", {"key": "escape"}),
                ("window_list", {}),
            ]
        )

    results = [
        await backends.execute_action(
            action,
            {
                **arguments,
                "provider": "usecomputer",
                "_persistent_worker": True,
            },
        )
        for action, arguments in calls
    ]

    assert len(results) == 50
    assert all("Error:" not in result for result in results)
