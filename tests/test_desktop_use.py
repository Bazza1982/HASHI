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


@pytest.mark.asyncio
async def test_desktop_type_timeout_does_not_repeat_partially_entered_text(monkeypatch):
    calls = []
    monkeypatch.setattr(desktop, "_find_xdotool", lambda: "fixture-xdotool")
    monkeypatch.setattr(desktop, "_find_usecomputer", lambda: "fixture-fallback")

    async def fake_run(cmd, display, timeout=15):
        setting = getattr(desktop, "_DESKTOP_REQUEST_TIMEOUT", None)
        calls.append((cmd, display, timeout, setting.get() if setting is not None else None))
        return 1, "", "command timed out"

    async def no_reset(_display):
        pass

    monkeypatch.setattr(desktop, "_run", fake_run)
    monkeypatch.setattr(desktop, "_best_effort_reset_desktop_input_state", no_reset)
    result = await desktop.execute_desktop_type({
        "text": "some text", "display": ":test", "timeout_s": 120,
    })
    assert len(calls) == 1
    assert calls[0][3] == 120
    assert result.startswith("Error:") and "partially" in result


@pytest.mark.asyncio
async def test_desktop_subprocess_timeout_and_cancellation_reap_fixture(tmp_path):
    import asyncio
    import sys
    import psutil

    async def start_fixture(marker, timeout):
        code = ("import os,pathlib,sys,time; "
                "pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(120)")
        return await desktop._run([sys.executable, "-c", code, str(marker)], ":fixture", timeout=timeout)

    async def wait_pid(marker):
        for _ in range(200):
            if marker.exists():
                return int(marker.read_text())
            await asyncio.sleep(0.025)
        pytest.fail("fixture subprocess did not start")

    for mode in ("timeout", "cancel"):
        marker = tmp_path / (mode + ".pid")
        task = asyncio.create_task(start_fixture(marker, 1 if mode == "timeout" else 120))
        pid = await wait_pid(marker)
        try:
            if mode == "cancel":
                task.cancel()
                with pytest.raises(asyncio.CancelledError) as cancelled:
                    await task
                assert cancelled.value.hashi_tool_details["cleanup_confirmed"] is True
            else:
                rc, _out, error = await task
                assert rc != 0 and "timed out" in error
            assert not psutil.pid_exists(pid)
        finally:
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            if psutil.pid_exists(pid):
                process = psutil.Process(pid)
                assert str(marker) in " ".join(process.cmdline())
                process.kill()
                process.wait(timeout=5)


@pytest.mark.asyncio
async def test_desktop_selected_timeout_allows_work_past_old_limit():
    import os
    import sys
    if os.environ.get("HASHI_HX036_SLOW_DESKTOP") != "1":
        pytest.skip("opt-in synthetic slow subprocess qualification")
    token = desktop._DESKTOP_REQUEST_TIMEOUT.set(22.0)
    try:
        rc, out, err = await desktop._run(
            [sys.executable, "-c", "import time; time.sleep(16); print('fixture-complete')"],
            ":fixture",
        )
    finally:
        desktop._DESKTOP_REQUEST_TIMEOUT.reset(token)
    assert rc == 0 and out == "fixture-complete" and not err


@pytest.mark.asyncio
@pytest.mark.parametrize("timeout", [0, -1, float("nan"), True, "10", 10**1000])
async def test_desktop_invalid_timeout_is_rejected_before_input(monkeypatch, timeout):
    calls = []
    monkeypatch.setattr(desktop, "_find_xdotool", lambda: "fixture-xdotool")

    async def fake_run(*args, **kwargs):
        calls.append((args, kwargs))
        return 0, "", ""

    monkeypatch.setattr(desktop, "_run", fake_run)
    result = await desktop.execute_desktop_type({"text": "fixture", "timeout_s": timeout})
    assert result.startswith("Error:") and "timeout_s" in result
    assert not calls
