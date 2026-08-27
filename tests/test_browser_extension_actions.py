from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools import browser
from tools.schemas import TOOL_SCHEMA_MAP


ROOT = Path(__file__).resolve().parents[1]


def test_normalize_extension_bridge_screenshot_data_url() -> None:
    result = browser._normalize_extension_bridge_output(
        "screenshot",
        "data:image/png;base64,aGVsbG8=",
    )
    assert result == "screenshot:aGVsbG8="


def test_normalize_extension_bridge_leaves_non_screenshot_output() -> None:
    result = browser._normalize_extension_bridge_output(
        "get_text",
        "data:image/png;base64,aGVsbG8=",
    )
    assert result == "data:image/png;base64,aGVsbG8="


@pytest.mark.asyncio
async def test_execute_browser_screenshot_normalizes_extension_data_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_bridge(action: str, args: dict) -> str | None:
        assert action == "screenshot"
        return "screenshot:aGVsbG8="

    monkeypatch.setattr(browser, "_maybe_execute_extension_bridge", fake_bridge)
    result = await browser.execute_browser_screenshot(
        {
            "url": "https://example.com",
            "bridge_backend": "extension",
        }
    )
    assert result == "screenshot:aGVsbG8="


@pytest.mark.asyncio
async def test_execute_browser_click_prefers_extension_bridge(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_bridge(action: str, args: dict) -> str | None:
        assert action == "click"
        assert args["selector"] == "button.search"
        return "OK: clicked 'button.search'"

    monkeypatch.setattr(browser, "_maybe_execute_extension_bridge", fake_bridge)
    result = await browser.execute_browser_click(
        {
            "url": "https://example.com",
            "selector": "button.search",
            "bridge_backend": "extension",
        }
    )
    assert result == "OK: clicked 'button.search'"


@pytest.mark.asyncio
async def test_execute_browser_react_prefers_extension_bridge(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_bridge(action: str, args: dict) -> str | None:
        assert action == "react"
        assert args["post_text"] == "City2Surf 2026"
        assert args["author"] == "Jordan Xu"
        return json.dumps({"ok": True, "action": "react", "verified": True})

    monkeypatch.setattr(browser, "_maybe_execute_extension_bridge", fake_bridge)
    result = await browser.execute_browser_react(
        {
            "url": "https://www.linkedin.com/feed/",
            "post_text": "City2Surf 2026",
            "author": "Jordan Xu",
            "reaction": "like",
        }
    )

    assert json.loads(result)["verified"] is True


@pytest.mark.asyncio
async def test_execute_browser_fill_prefers_extension_bridge(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_bridge(action: str, args: dict) -> str | None:
        assert action == "fill"
        assert args["selector"] == "input[name='q']"
        assert args["text"] == "hashi browser bridge"
        return "OK: filled 'input[name=\\'q\\']'"

    monkeypatch.setattr(browser, "_maybe_execute_extension_bridge", fake_bridge)
    result = await browser.execute_browser_fill(
        {
            "url": "https://scholar.google.com",
            "selector": "input[name='q']",
            "text": "hashi browser bridge",
            "bridge_backend": "extension",
        }
    )
    assert result == "OK: filled 'input[name=\\'q\\']'"


@pytest.mark.asyncio
async def test_execute_browser_hover_passes_timing_and_position_to_extension(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_bridge(action: str, args: dict) -> str | None:
        assert action == "hover"
        assert args["selector"] == "button.reaction"
        assert args["timeout_ms"] == 8000
        assert args["wait_ms"] == 650
        assert args["x_ratio"] == 0.4
        assert args["y_ratio"] == 0.6
        return "OK: hovered 'button.reaction'"

    monkeypatch.setattr(browser, "_maybe_execute_extension_bridge", fake_bridge)
    result = await browser.execute_browser_hover(
        {
            "url": "https://www.linkedin.com/feed/",
            "selector": "button.reaction",
            "timeout_ms": 8000,
            "wait_ms": 650,
            "x_ratio": 0.4,
            "y_ratio": 0.6,
            "bridge_backend": "extension",
        }
    )
    assert result == "OK: hovered 'button.reaction'"


@pytest.mark.asyncio
async def test_execute_browser_evaluate_prefers_extension_bridge(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_bridge(action: str, args: dict) -> str | None:
        assert action == "evaluate"
        assert args["script"] == "() => document.title"
        return "\"Example Domain\""

    monkeypatch.setattr(browser, "_maybe_execute_extension_bridge", fake_bridge)
    result = await browser.execute_browser_evaluate(
        {
            "url": "https://example.com",
            "script": "() => document.title",
            "bridge_backend": "extension",
        }
    )
    assert result == "\"Example Domain\""


@pytest.mark.asyncio
async def test_structured_media_tools_use_extension_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[str] = []

    async def fake_bridge(action: str, args: dict) -> str | None:
        observed.append(action)
        assert args["bridge_backend"] == "extension"
        if action == "active_tab":
            return json.dumps(
                {
                    "tabId": 7,
                    "windowId": 9,
                    "url": "https://www.youtube.com/watch?v=abc",
                    "title": "Example video - YouTube",
                    "maximized": True,
                }
            )
        if action == "media_state":
            return json.dumps({"ok": True, "media_found": True, "state": {"paused": True}})
        return json.dumps({"ok": True, "verified": True, "time_delta": 1.25})

    monkeypatch.setattr(browser, "_maybe_execute_extension_bridge", fake_bridge)

    active = json.loads(await browser.execute_browser_active_tab({}))
    state = json.loads(await browser.execute_browser_get_media_state({}))
    played = json.loads(await browser.execute_browser_play({}))

    assert active["maximized"] is True
    assert state["state"]["paused"] is True
    assert played["verified"] is True
    assert observed == ["active_tab", "media_state", "media_play"]


@pytest.mark.asyncio
async def test_open_play_verify_requires_every_postcondition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    url = "https://www.youtube.com/watch?v=abc123"
    active_calls = 0

    async def fake_wait(_timeout_s: float) -> dict:
        return {
            "connected": True,
            "response": {"extension_meta": {"extension_version": "0.2.0"}},
        }

    async def fake_active(args: dict) -> str:
        nonlocal active_calls
        active_calls += 1
        return json.dumps(
            {
                "tabId": 7,
                "windowId": 9,
                "url": url,
                "title": "系统送我一只鸡，我卷翻了修仙界 - YouTube",
                "windowState": "maximized",
                "maximized": True,
            }
        )

    async def fake_play(args: dict) -> str:
        assert args["url"] == url
        return json.dumps(
            {
                "ok": True,
                "verified": True,
                "time_advanced": True,
                "time_delta": 1.5,
                "after": {"paused": False, "current_time": 8.5},
            }
        )

    async def fake_focus(args: dict) -> str:
        assert args == {
            "title_contains": "YouTube",
            "maximize": True,
            "_skip_helper": True,
        }
        return "Focused and maximized window id=11 title=YouTube - Google Chrome"

    monkeypatch.setattr(browser, "_wait_for_extension_connection", fake_wait)
    monkeypatch.setattr(browser, "execute_browser_active_tab", fake_active)
    monkeypatch.setattr(browser, "execute_browser_play", fake_play)
    monkeypatch.setattr(browser.platform, "system", lambda: "Windows")
    monkeypatch.setattr("tools.windows_use.execute_windows_window_focus", fake_focus)

    result = json.loads(
        await browser.execute_browser_open_play_verify(
            {"url": url, "expected_title": "系统送我一只鸡，我卷翻了修仙界"}
        )
    )

    assert result["ok"] is True
    assert result["maximized"] is True
    assert result["playback_verified"] is True
    assert active_calls == 2
    assert [stage["stage"] for stage in result["stages"]] == [
        "bridge_connect",
        "open_and_maximize",
        "play_and_verify",
        "final_identity_check",
    ]


@pytest.mark.asyncio
async def test_open_play_verify_never_promotes_a_click_to_playback_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    url = "https://www.youtube.com/watch?v=abc123"

    async def fake_wait(_timeout_s: float) -> dict:
        return {"connected": True, "response": {"extension_meta": {"extension_version": "0.2.0"}}}

    async def fake_active(_args: dict) -> str:
        return json.dumps(
            {
                "url": url,
                "title": "Example video - YouTube",
                "maximized": True,
            }
        )

    async def fake_play(_args: dict) -> str:
        return json.dumps(
            {
                "ok": False,
                "verified": False,
                "time_advanced": False,
                "after": {"paused": True, "current_time": 2.0},
                "error": "media playback did not satisfy the advancing-time postcondition",
            }
        )

    async def fake_focus(_args: dict) -> str:
        return "Focused and maximized window id=11 title=YouTube - Google Chrome"

    monkeypatch.setattr(browser, "_wait_for_extension_connection", fake_wait)
    monkeypatch.setattr(browser, "execute_browser_active_tab", fake_active)
    monkeypatch.setattr(browser, "execute_browser_play", fake_play)
    monkeypatch.setattr(browser.platform, "system", lambda: "Windows")
    monkeypatch.setattr("tools.windows_use.execute_windows_window_focus", fake_focus)

    result = json.loads(
        await browser.execute_browser_open_play_verify(
            {"url": url, "expected_title": "Example video"}
        )
    )

    assert result["ok"] is False
    assert result["failed_stage"] == "play_and_verify"
    assert result["playback_verified"] is False
    assert "advancing-time postcondition" in result["error"]


def test_media_tools_are_model_visible_with_strict_high_level_contract() -> None:
    for name in (
        "browser_active_tab",
        "browser_get_media_state",
        "browser_play",
        "browser_open_play_verify",
    ):
        assert name in TOOL_SCHEMA_MAP
    high_level = TOOL_SCHEMA_MAP["browser_open_play_verify"]["function"]["parameters"]
    assert high_level["required"] == ["url", "expected_title"]
    assert high_level["additionalProperties"] is False


@pytest.mark.asyncio
async def test_extension_contract_rejects_advanced_action_not_advertised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tools import browser_extension_bridge

    monkeypatch.setattr(browser_extension_bridge, "bridge_available", lambda: True)
    monkeypatch.setattr(
        browser_extension_bridge,
        "ensure_bridge_session",
        lambda **_kwargs: {
            "session": {"session_id": "default::momo"},
            "extension_meta": {"extension_version": "0.1.2"},
        },
    )

    result = await browser._maybe_execute_extension_bridge(
        "session",
        {"url": "https://example.com", "steps": [{"action": "get_text"}]},
    )

    assert result is not None
    assert "does not advertise the 'session' contract" in result


@pytest.mark.asyncio
async def test_extension_contract_allows_advertised_action(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tools import browser_extension_bridge

    monkeypatch.setattr(browser_extension_bridge, "bridge_available", lambda: True)
    monkeypatch.setattr(
        browser_extension_bridge,
        "ensure_bridge_session",
        lambda **_kwargs: {
            "session": {"session_id": "default::momo"},
            "extension_meta": {
                "extension_version": "0.1.4",
                "actions": ["session_create", "session"],
            },
        },
    )
    monkeypatch.setattr(
        browser_extension_bridge,
        "send_bridge_command",
        lambda action, args: {"ok": True, "output": f"executed:{action}:{args['session_id']}"},
    )

    result = await browser._maybe_execute_extension_bridge(
        "session",
        {"url": "https://example.com", "steps": [{"action": "get_text"}]},
    )

    assert result == "executed:session:default::momo"
def test_extension_source_routes_session_and_scroll_to_real_implementations() -> None:
    source = (
        ROOT / "tools" / "chrome_extension" / "hashi_browser_bridge" / "service_worker.js"
    ).read_text(encoding="utf-8")
    manifest = json.loads(
        (ROOT / "tools" / "chrome_extension" / "hashi_browser_bridge" / "manifest.json").read_text(
            encoding="utf-8"
        )
    )

    assert 'const BRIDGE_VERSION = "0.2.0";' in source
    assert manifest["version"] == "0.2.0"
    assert 'if (action === "session") {\n    return actionSession(args);' in source
    assert 'if (action === "scroll") {\n    return actionScroll(args);' in source
    assert 'if (action === "react") {\n    return actionReact(args);' in source
    assert '"click", "react", "hover"' in source
    assert "scrollable[0]" in source
    assert "reaction click did not produce a verified state change" in source
    assert 'action === "active_tab" || action === "session_create" || action === "session"' not in source
    assert "actions: SUPPORTED_ACTIONS" in source
    assert '"media_state", "media_play"' in source
    assert 'if (action === "media_state") {\n    return actionMediaState(args);' in source
    assert 'if (action === "media_play") {\n    return actionMediaPlay(args);' in source
    assert "media playback did not satisfy the advancing-time postcondition" in source
    assert 'state: "maximized"' in source
