from __future__ import annotations

import pytest

from tools import browser


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
