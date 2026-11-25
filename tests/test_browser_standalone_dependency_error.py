"""The standalone headless browser must fail with an actionable message."""

from __future__ import annotations

import builtins

import pytest


@pytest.mark.asyncio
async def test_standalone_browser_missing_playwright_has_actionable_error(monkeypatch):
    from tools.browser import _get_page

    original_import = builtins.__import__

    def blocked(name, *args, **kwargs):
        if name in {"playwright", "playwright.async_api"}:
            raise ModuleNotFoundError("No module named 'playwright'")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked)
    with pytest.raises(RuntimeError, match="Playwright is not installed"):
        await _get_page()
