from __future__ import annotations

import pytest

from tools.schemas import TOOL_SCHEMA_MAP
from tools import windows_use


@pytest.fixture(autouse=True)
def _disable_helper_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HASHI_WINDOWS_HELPER", "0")


def test_resolve_provider_auto_routes_by_action() -> None:
    assert windows_use._resolve_provider("auto", "screenshot") == "usecomputer"
    assert windows_use._resolve_provider("auto", "mouse_move") == "usecomputer"
    assert windows_use._resolve_provider("auto", "click") == "usecomputer"
    assert windows_use._resolve_provider("auto", "scroll") == "usecomputer"
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
    async def fake_usecomputer(body: str, timeout: int = 30):
        assert "Resolve-HashiWindow" in body
        assert "TitleContains 'Notepad'" in body
        assert "Invoke-Usecomputer -Args @('type'" in body
        return {"ok": True, "output": ""}, None

    async def fake_reset() -> None:
        return None

    monkeypatch.setattr(windows_use, "_run_usecomputer_json", fake_usecomputer)
    monkeypatch.setattr(windows_use, "_best_effort_reset_windows_input_state", fake_reset)

    result = await windows_use.execute_windows_type(
        {
            "provider": "auto",
            "text": "hello",
            "title_contains": "Notepad",
        }
    )

    assert result == "Typed 5 chars on Windows host via usecomputer"


def test_windows_tool_schemas_include_window_controls_and_stability_args() -> None:
    assert "windows_window_list" in TOOL_SCHEMA_MAP
    assert "windows_window_focus" in TOOL_SCHEMA_MAP
    assert "windows_reset_input_state" in TOOL_SCHEMA_MAP
    assert "windows_helper_warmup" in TOOL_SCHEMA_MAP
    close_props = TOOL_SCHEMA_MAP["windows_window_close"]["function"]["parameters"]["properties"]
    type_props = TOOL_SCHEMA_MAP["windows_type"]["function"]["parameters"]["properties"]
    key_props = TOOL_SCHEMA_MAP["windows_key"]["function"]["parameters"]["properties"]

    assert "dismiss_unsaved" in close_props
    assert "force" in close_props
    assert "wait_ms" in close_props
    assert "focus_first" in type_props
    assert "title_contains" in type_props
    assert "focus_first" in key_props
    assert "title_contains" in key_props


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


@pytest.mark.asyncio
async def test_windows_screenshot_passes_resolved_save_path_to_helper(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = {}

    async def fake_helper(action: str, args: dict) -> str | None:
        seen["action"] = action
        seen["args"] = dict(args)
        return "helper-screenshot-ok"

    monkeypatch.setattr(windows_use, "_maybe_execute_windows_helper", fake_helper)
    monkeypatch.setattr(
        windows_use,
        "_resolve_windows_save_path",
        lambda value: (r"C:\tmp\shot.png", None) if value else (None, None),
    )

    result = await windows_use.execute_windows_screenshot({"save_path": "/tmp/shot.png"})

    assert result == "helper-screenshot-ok"
    assert seen["action"] == "screenshot"
    assert seen["args"]["save_path"] == r"C:\tmp\shot.png"


@pytest.mark.asyncio
async def test_windows_key_resets_input_state_after_usecomputer(monkeypatch: pytest.MonkeyPatch) -> None:
    reset_calls: list[str] = []

    async def fake_usecomputer(body: str, timeout: int = 30):
        assert "Invoke-Usecomputer -Args @('press'" in body
        return {"ok": True, "output": ""}, None

    async def fake_reset() -> None:
        reset_calls.append("reset")

    monkeypatch.setattr(windows_use, "_run_usecomputer_json", fake_usecomputer)
    monkeypatch.setattr(windows_use, "_best_effort_reset_windows_input_state", fake_reset)

    result = await windows_use.execute_windows_key({"provider": "auto", "key": "ctrl+l"})

    assert result == "Pressed 'ctrl+l' on Windows host via usecomputer"
    assert reset_calls == ["reset"]


@pytest.mark.asyncio
async def test_windows_key_focuses_selected_window_inline(monkeypatch: pytest.MonkeyPatch) -> None:
    reset_calls: list[str] = []

    async def fake_usecomputer(body: str, timeout: int = 30):
        assert "Resolve-HashiWindow" in body
        assert "TitleContains 'Chrome'" in body
        assert "Invoke-Usecomputer -Args @('press', 'ctrl+l')" in body
        return {"ok": True, "output": ""}, None

    async def fake_reset() -> None:
        reset_calls.append("reset")

    monkeypatch.setattr(windows_use, "_run_usecomputer_json", fake_usecomputer)
    monkeypatch.setattr(windows_use, "_best_effort_reset_windows_input_state", fake_reset)

    result = await windows_use.execute_windows_key(
        {"provider": "auto", "key": "ctrl+l", "title_contains": "Chrome"}
    )

    assert result == "Pressed 'ctrl+l' on Windows host via usecomputer"
    assert reset_calls == ["reset"]


@pytest.mark.asyncio
async def test_windows_click_prefers_helper_when_available(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_helper(action: str, args: dict) -> str | None:
        assert action == "click"
        assert args["x"] == 10
        return "helper-click-ok"

    monkeypatch.setattr(windows_use, "_maybe_execute_windows_helper", fake_helper)

    result = await windows_use.execute_windows_click({"x": 10, "y": 20})

    assert result == "helper-click-ok"


@pytest.mark.asyncio
async def test_ensure_windows_helper_started_uses_pythonpath_not_working_directory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    health_calls = {"count": 0}
    seen = {}

    async def fake_healthcheck(timeout: int = 3) -> bool:
        health_calls["count"] += 1
        return health_calls["count"] >= 2

    async def fake_run_powershell_json(body: str, timeout: int = 30):
        seen["body"] = body
        return {"ok": True}, None

    monkeypatch.setattr(windows_use, "_helper_healthcheck", fake_healthcheck)
    monkeypatch.setattr(windows_use, "_run_powershell_json", fake_run_powershell_json)
    monkeypatch.setattr(
        windows_use,
        "_resolve_windows_save_path",
        lambda path: (r"\\\\wsl.localhost\\Ubuntu-22.04\\home\\lily\\projects\\hashi", None),
    )

    started = await windows_use._ensure_windows_helper_started()

    assert started is True
    assert "$env:PYTHONPATH" in seen["body"]
    assert "-WorkingDirectory $repoRoot" not in seen["body"]
    assert "-WorkingDirectory $helperWorkingDir" in seen["body"]
    assert "-WindowStyle Hidden" not in seen["body"]
    assert "'--with', 'pillow'" in seen["body"]


@pytest.mark.asyncio
async def test_windows_click_falls_back_when_helper_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    reset_calls: list[str] = []

    async def fake_helper(action: str, args: dict) -> str | None:
        return None

    async def fake_usecomputer(body: str, timeout: int = 30):
        assert "Invoke-Usecomputer -Args @('click'" in body
        return {"ok": True, "output": ""}, None

    async def fake_reset() -> None:
        reset_calls.append("reset")

    monkeypatch.setattr(windows_use, "_maybe_execute_windows_helper", fake_helper)
    monkeypatch.setattr(windows_use, "_run_usecomputer_json", fake_usecomputer)
    monkeypatch.setattr(windows_use, "_best_effort_reset_windows_input_state", fake_reset)

    result = await windows_use.execute_windows_click({"provider": "usecomputer", "x": 10, "y": 20})

    assert result == "Clicked (10, 20) button=left count=1 on Windows host"
    assert reset_calls == ["reset"]


@pytest.mark.asyncio
async def test_windows_click_resets_input_state_after_windows_mcp(monkeypatch: pytest.MonkeyPatch) -> None:
    reset_calls: list[str] = []

    async def fake_mcp(payload: dict, timeout: int = 90):
        assert payload["tool"] == "Click"
        return {"content": [{"type": "text", "text": "clicked"}]}, None

    async def fake_reset() -> None:
        reset_calls.append("reset")

    monkeypatch.setattr(windows_use, "_run_windows_mcp_json", fake_mcp)
    monkeypatch.setattr(windows_use, "_best_effort_reset_windows_input_state", fake_reset)

    result = await windows_use.execute_windows_click({"provider": "windows-mcp", "x": 10, "y": 20})

    assert result == "clicked"
    assert reset_calls == ["reset"]


@pytest.mark.asyncio
async def test_windows_reset_input_state_reports_state(monkeypatch: pytest.MonkeyPatch) -> None:
    reset_calls: list[str] = []

    async def fake_reset() -> None:
        reset_calls.append("reset")

    async def fake_state():
        return {
            "foreground_window": {"id": 1, "title": "Chrome"},
            "keyboard_layout": {"hkl": "0x00000409", "klid": "00000409"},
        }, None

    monkeypatch.setattr(windows_use, "_best_effort_reset_windows_input_state", fake_reset)
    monkeypatch.setattr(windows_use, "_get_windows_input_state", fake_state)

    result = await windows_use.execute_windows_reset_input_state({})

    assert reset_calls == ["reset"]
    assert '"message": "Windows input state reset completed"' in result
    assert '"title": "Chrome"' in result


@pytest.mark.asyncio
async def test_windows_helper_warmup_reports_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HASHI_WINDOWS_HELPER", "1")

    async def fake_start() -> bool:
        return True

    async def fake_healthcheck(timeout: int = 3) -> bool:
        return True

    monkeypatch.setattr(windows_use, "_ensure_windows_helper_started", fake_start)
    monkeypatch.setattr(windows_use, "_helper_healthcheck", fake_healthcheck)

    result = await windows_use.execute_windows_helper_warmup({})

    assert '"ok": true' in result
    assert '"healthy": true' in result
    assert "warm and ready" in result
