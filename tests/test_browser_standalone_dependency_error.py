"""The standalone headless browser must fail with an actionable message."""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_standalone_browser_missing_playwright_has_actionable_error():
    try:
        import playwright  # noqa: F401
    except ImportError:
        pass
    else:
        pytest.skip("playwright is installed in this environment")

    from tools.browser import _get_page

    with pytest.raises(RuntimeError, match="Playwright is not installed"):
        await _get_page()
