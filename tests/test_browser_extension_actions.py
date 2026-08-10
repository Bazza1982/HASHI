from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools import browser


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

    assert 'const BRIDGE_VERSION = "0.1.4";' in source
    assert manifest["version"] == "0.1.4"
    assert 'if (action === "session") {\n    return actionSession(args);' in source
    assert 'if (action === "scroll") {\n    return actionScroll(args);' in source
    assert 'action === "active_tab" || action === "session_create" || action === "session"' not in source
    assert "actions: SUPPORTED_ACTIONS" in source
