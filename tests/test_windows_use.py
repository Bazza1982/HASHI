from __future__ import annotations

import pytest

from tools.schemas import TOOL_SCHEMA_MAP
from tools import windows_use


def test_resolve_provider_auto_routes_by_action() -> None:
    assert windows_use._resolve_provider("auto", "screenshot") == "windows-mcp"
    assert windows_use._resolve_provider("auto", "mouse_move") == "windows-mcp"
    assert windows_use._resolve_provider("auto", "click") == "windows-mcp"
    assert windows_use._resolve_provider("auto", "scroll") == "windows-mcp"
    assert windows_use._resolve_provider("auto", "type") == "usecomputer"
    assert windows_use._resolve_provider("auto", "key") == "usecomputer"
    assert windows_use._resolve_provider("auto", "window_focus") == "usecomputer"


def test_is_auto_provider_only_matches_auto() -> None:
    assert windows_use._is_auto_provider(None) is True
    assert windows_use._is_auto_provider("auto") is True
    assert windows_use._is_auto_provider("usecomputer") is False
    assert windows_use._is_auto_provider("windows-mcp") is False


@pytest.mark.asyncio
async def test_windows_type_focuses_selected_window_before_typing(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict] = []

    async def fake_focus(args: dict) -> str:
        calls.append(args)
        return "Focused window id=1 title=Notepad"

    async def fake_usecomputer(body: str, timeout: int = 30):
        assert "Invoke-Usecomputer -Args @('type'" in body
        return {"ok": True, "output": ""}, None

    monkeypatch.setattr(windows_use, "execute_windows_window_focus", fake_focus)
    monkeypatch.setattr(windows_use, "_run_usecomputer_json", fake_usecomputer)

    result = await windows_use.execute_windows_type(
        {
            "provider": "auto",
            "text": "hello",
            "title_contains": "Notepad",
        }
    )

    assert result == "Typed 5 chars on Windows host via usecomputer"
    assert calls == [
        {
            "provider": "auto",
            "window_id": 0,
            "pid": 0,
            "title_contains": "Notepad",
            "exact_title": "",
        }
    ]


def test_windows_tool_schemas_include_window_controls_and_stability_args() -> None:
    assert "windows_window_list" in TOOL_SCHEMA_MAP
    assert "windows_window_focus" in TOOL_SCHEMA_MAP
    close_props = TOOL_SCHEMA_MAP["windows_window_close"]["function"]["parameters"]["properties"]
    type_props = TOOL_SCHEMA_MAP["windows_type"]["function"]["parameters"]["properties"]

    assert "dismiss_unsaved" in close_props
    assert "force" in close_props
    assert "wait_ms" in close_props
    assert "focus_first" in type_props
    assert "title_contains" in type_props


@pytest.mark.asyncio
async def test_windows_screenshot_auto_falls_back_to_usecomputer(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_windows_mcp(payload: dict, timeout: int = 90):
        return None, "No module named uv"

    async def fake_usecomputer(body: str, timeout: int = 30):
        assert "screenshot" in body
        return {
            "file_size": 4,
            "metadata": {"ok": True},
            "saved_to": None,
            "base64": "dGVzdA==",
        }, None

    monkeypatch.setattr(windows_use, "_run_windows_mcp_json", fake_windows_mcp)
    monkeypatch.setattr(windows_use, "_run_usecomputer_json", fake_usecomputer)

    result = await windows_use.execute_windows_screenshot({"provider": "auto"})

    assert "provider=usecomputer" in result
