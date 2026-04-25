from __future__ import annotations

import pytest

from tools import desktop


@pytest.mark.asyncio
async def test_desktop_key_resets_input_state(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    monkeypatch.setattr(desktop, "_find_usecomputer", lambda: "/tmp/usecomputer")

    async def fake_run(cmd: list[str], display: str, timeout: int = 15):
        calls.append(" ".join(cmd))
        return 0, "", ""

    monkeypatch.setattr(desktop, "_run", fake_run)
    monkeypatch.setattr(desktop.shutil, "which", lambda name: "/usr/bin/xdotool" if name == "xdotool" else None)

    result = await desktop.execute_desktop_key({"key": "ctrl+s", "display": ":10"})

    assert result == "Pressed 'ctrl+s' on DISPLAY=:10"
    assert any("press ctrl+s" in call for call in calls)
    assert any("keyup Shift_L" in call for call in calls)
    assert any("mouseup 1" in call for call in calls)


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
