from __future__ import annotations

import json

import pytest

from tools import desktop
from tools.schemas import TOOL_SCHEMA_MAP


@pytest.fixture(autouse=True)
def _clear_desktop_caches() -> None:
    desktop._find_usecomputer.cache_clear()
    desktop._find_xdotool.cache_clear()


@pytest.mark.asyncio
async def test_desktop_key_prefers_xdotool_and_resets_input_state(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    monkeypatch.setattr(desktop.shutil, "which", lambda name: "/usr/bin/xdotool" if name == "xdotool" else None)

    async def fake_run(cmd: list[str], display: str, timeout: int = 15):
        calls.append(" ".join(cmd))
        return 0, "", ""

    monkeypatch.setattr(desktop, "_run", fake_run)

    result = await desktop.execute_desktop_key({"key": "ctrl+s", "display": ":10"})

    assert result == "Pressed 'ctrl+s' on DISPLAY=:10 via xdotool"
    assert any("key --clearmodifiers ctrl+s" in call for call in calls)
    assert any("keyup Shift_L" in call for call in calls)
    assert any("mouseup 1" in call for call in calls)


@pytest.mark.asyncio
async def test_desktop_mouse_move_prefers_xdotool(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    monkeypatch.setattr(desktop.shutil, "which", lambda name: "/usr/bin/xdotool" if name == "xdotool" else None)

    async def fake_run(cmd: list[str], display: str, timeout: int = 15):
        calls.append(" ".join(cmd))
        return 0, "", ""

    monkeypatch.setattr(desktop, "_run", fake_run)

    result = await desktop.execute_desktop_mouse_move({"x": 10, "y": 20, "display": ":10"})

    assert result == "Mouse moved to (10, 20) on DISPLAY=:10 via xdotool"
    assert any("mousemove --sync 10 20" in call for call in calls)


@pytest.mark.asyncio
async def test_desktop_type_resets_after_xdotool(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    monkeypatch.setattr(desktop.shutil, "which", lambda name: "/usr/bin/xdotool" if name == "xdotool" else None)

    async def fake_run(cmd: list[str], display: str, timeout: int = 15):
        calls.append(" ".join(cmd))
        return 0, "", ""

    monkeypatch.setattr(desktop, "_run", fake_run)

    result = await desktop.execute_desktop_type({"text": "hello", "display": ":10"})

    assert result == "Typed 5 chars via xdotool on DISPLAY=:10"
    assert any("type --delay 30 -- hello" in call for call in calls)
    assert any("keyup Control_L" in call for call in calls)


@pytest.mark.asyncio
async def test_desktop_window_list_reports_active_window(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(desktop.shutil, "which", lambda name: "/usr/bin/xdotool" if name == "xdotool" else None)

    async def fake_run(cmd: list[str], display: str, timeout: int = 15):
        rendered = " ".join(cmd)
        if "search --onlyvisible --name .+" in rendered:
            return 0, "101\n202\n", ""
        if "getactivewindow" in rendered:
            return 0, "202\n", ""
        if "getwindowname 101" in rendered:
            return 0, "Terminal\n", ""
        if "getwindowname 202" in rendered:
            return 0, "Chrome\n", ""
        if "getwindowpid 101" in rendered:
            return 0, "111\n", ""
        if "getwindowpid 202" in rendered:
            return 0, "222\n", ""
        if "getwindowgeometry --shell 101" in rendered:
            return 0, "X=1\nY=2\nWIDTH=300\nHEIGHT=200\n", ""
        if "getwindowgeometry --shell 202" in rendered:
            return 0, "X=11\nY=12\nWIDTH=800\nHEIGHT=600\n", ""
        raise AssertionError(rendered)

    monkeypatch.setattr(desktop, "_run", fake_run)

    result = await desktop.execute_desktop_window_list({"display": ":10"})
    payload = json.loads(result)

    assert payload["display"] == ":10"
    assert len(payload["windows"]) == 2
    assert payload["windows"][1]["title"] == "Chrome"
    assert payload["windows"][1]["is_active"] is True


def test_desktop_tool_schemas_include_window_controls() -> None:
    assert "desktop_window_list" in TOOL_SCHEMA_MAP
    assert "desktop_window_focus" in TOOL_SCHEMA_MAP
