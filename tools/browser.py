"""
Browser tool executor for HASHI — powered by Playwright.

Supports two modes:
  - CDP mode: connect to an already-running Chrome/Chromium with
    --remote-debugging-port, reusing the user's existing session/cookies.
  - Standalone mode: launch a fresh headless (or headed) Chromium instance.

Cross-platform: Linux, macOS, Windows.
"""
from __future__ import annotations

import asyncio
import base64
import platform
import sys
from typing import Optional


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _find_chrome_executable() -> Optional[str]:
    """Best-guess path for Chrome/Chromium on the current OS."""
    os_name = platform.system()
    candidates: list[str] = []

    if os_name == "Darwin":
        candidates = [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
        ]
    elif os_name == "Windows":
        import os
        pf = os.environ.get("PROGRAMFILES", r"C:\Program Files")
        pf86 = os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")
        candidates = [
            rf"{pf}\Google\Chrome\Application\chrome.exe",
            rf"{pf86}\Google\Chrome\Application\chrome.exe",
            rf"{pf}\Chromium\Application\chrome.exe",
        ]
    else:  # Linux / WSL
        candidates = [
            "/usr/bin/google-chrome",
            "/usr/bin/chromium-browser",
            "/usr/bin/chromium",
            "/snap/bin/chromium",
        ]

    import shutil
    for c in candidates:
        if shutil.which(c) or __import__("os").path.isfile(c):
            return c
    return None


async def _get_page(
    cdp_url: Optional[str] = None,
    headed: bool = False,
):
    """
    Return (playwright, browser, context, page).
    Caller is responsible for cleanup.

    cdp_url  — e.g. "http://localhost:9222"  → CDP attach mode
    headed   — ignored in CDP mode; in standalone mode launches visible window
    """
    from playwright.async_api import async_playwright

    pw = await async_playwright().start()

    if cdp_url:
        browser = await pw.chromium.connect_over_cdp(cdp_url)
        # reuse the first existing context (has user's cookies / session)
        contexts = browser.contexts
        if contexts:
            context = contexts[0]
        else:
            context = await browser.new_context()
        pages = context.pages
        page = pages[0] if pages else await context.new_page()
    else:
        launch_opts: dict = {
            "headless": not headed,
        }
        exe = _find_chrome_executable()
        if exe:
            launch_opts["executable_path"] = exe

        browser = await pw.chromium.launch(**launch_opts)
        context = await browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent="Mozilla/5.0 (compatible; HASHI-Browser/2.2)",
        )
        page = await context.new_page()

    return pw, browser, context, page


# ---------------------------------------------------------------------------
# Public executors (called by ToolRegistry)
# ---------------------------------------------------------------------------

async def execute_browser_screenshot(args: dict) -> str:
    """
    Navigate to a URL and return a base64-encoded PNG screenshot.
    Returns:  "screenshot:<base64>" on success  or  "Error: ..."
    """
    url = str(args.get("url", "")).strip()
    if not url:
        return "Error: url is required"

    cdp_url = args.get("cdp_url")  # e.g. "http://localhost:9222"
    headed = bool(args.get("headed", False))
    wait_ms = int(args.get("wait_ms", 1500))
    full_page = bool(args.get("full_page", False))

    pw = browser = context = page = None
    try:
        pw, browser, context, page = await _get_page(cdp_url=cdp_url, headed=headed)

        if not cdp_url or page.url == "about:blank" or url != page.url:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)

        if wait_ms > 0:
            await asyncio.sleep(wait_ms / 1000)

        png_bytes = await page.screenshot(full_page=full_page)
        b64 = base64.b64encode(png_bytes).decode()
        return f"screenshot:{b64}"

    except Exception as e:
        return f"Error taking screenshot of {url}: {e}"
    finally:
        if pw:
            try:
                if not cdp_url and browser:
                    await browser.close()
                await pw.stop()
            except Exception:
                pass


async def execute_browser_get_text(args: dict) -> str:
    """
    Navigate to a URL, fully render JS, and return visible text content.
    Much richer than web_fetch for JS-heavy pages.
    """
    url = str(args.get("url", "")).strip()
    if not url:
        return "Error: url is required"

    cdp_url = args.get("cdp_url")
    headed = bool(args.get("headed", False))
    wait_ms = int(args.get("wait_ms", 1500))
    max_length = int(args.get("max_length", 15000))

    pw = browser = context = page = None
    try:
        pw, browser, context, page = await _get_page(cdp_url=cdp_url, headed=headed)
        await page.goto(url, wait_until="networkidle", timeout=30000)

        if wait_ms > 0:
            await asyncio.sleep(wait_ms / 1000)

        text = await page.evaluate("() => document.body.innerText")
        text = text.strip()
        if len(text) > max_length:
            text = text[:max_length] + "\n...[truncated]"
        return text

    except Exception as e:
        return f"Error fetching text from {url}: {e}"
    finally:
        if pw:
            try:
                if not cdp_url and browser:
                    await browser.close()
                await pw.stop()
            except Exception:
                pass


async def execute_browser_get_html(args: dict) -> str:
    """Return the rendered (post-JS) outer HTML of a page."""
    url = str(args.get("url", "")).strip()
    if not url:
        return "Error: url is required"

    cdp_url = args.get("cdp_url")
    headed = bool(args.get("headed", False))
    wait_ms = int(args.get("wait_ms", 1500))
    max_length = int(args.get("max_length", 20000))

    pw = browser = context = page = None
    try:
        pw, browser, context, page = await _get_page(cdp_url=cdp_url, headed=headed)
        await page.goto(url, wait_until="networkidle", timeout=30000)

        if wait_ms > 0:
            await asyncio.sleep(wait_ms / 1000)

        html = await page.content()
        if len(html) > max_length:
            html = html[:max_length] + "\n<!-- truncated -->"
        return html

    except Exception as e:
        return f"Error fetching HTML from {url}: {e}"
    finally:
        if pw:
            try:
                if not cdp_url and browser:
                    await browser.close()
                await pw.stop()
            except Exception:
                pass


async def execute_browser_click(args: dict) -> str:
    """Click an element on the current page by CSS selector."""
    url = str(args.get("url", "")).strip()
    selector = str(args.get("selector", "")).strip()
    if not url:
        return "Error: url is required"
    if not selector:
        return "Error: selector is required"

    cdp_url = args.get("cdp_url")
    headed = bool(args.get("headed", False))

    pw = browser = context = page = None
    try:
        pw, browser, context, page = await _get_page(cdp_url=cdp_url, headed=headed)
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await page.click(selector, timeout=10000)
        await asyncio.sleep(0.5)
        return f"OK: clicked '{selector}' on {url}"

    except Exception as e:
        return f"Error clicking '{selector}' on {url}: {e}"
    finally:
        if pw:
            try:
                if not cdp_url and browser:
                    await browser.close()
                await pw.stop()
            except Exception:
                pass


async def execute_browser_fill(args: dict) -> str:
    """Fill a form field (by CSS selector) with text, then optionally submit."""
    url = str(args.get("url", "")).strip()
    selector = str(args.get("selector", "")).strip()
    text = str(args.get("text", ""))
    if not url:
        return "Error: url is required"
    if not selector:
        return "Error: selector is required"

    cdp_url = args.get("cdp_url")
    headed = bool(args.get("headed", False))
    submit = bool(args.get("submit", False))

    pw = browser = context = page = None
    try:
        pw, browser, context, page = await _get_page(cdp_url=cdp_url, headed=headed)
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await page.fill(selector, text, timeout=10000)
        if submit:
            await page.keyboard.press("Enter")
            await asyncio.sleep(0.5)
        return f"OK: filled '{selector}' with text on {url}" + (" and submitted" if submit else "")

    except Exception as e:
        return f"Error filling '{selector}' on {url}: {e}"
    finally:
        if pw:
            try:
                if not cdp_url and browser:
                    await browser.close()
                await pw.stop()
            except Exception:
                pass


async def execute_browser_evaluate(args: dict) -> str:
    """
    Navigate to a URL and run arbitrary JS, returning the result as a string.
    Useful for extracting specific data from a page.
    """
    url = str(args.get("url", "")).strip()
    script = str(args.get("script", "")).strip()
    if not url:
        return "Error: url is required"
    if not script:
        return "Error: script is required"

    cdp_url = args.get("cdp_url")
    headed = bool(args.get("headed", False))
    wait_ms = int(args.get("wait_ms", 1000))

    pw = browser = context = page = None
    try:
        pw, browser, context, page = await _get_page(cdp_url=cdp_url, headed=headed)
        await page.goto(url, wait_until="networkidle", timeout=30000)

        if wait_ms > 0:
            await asyncio.sleep(wait_ms / 1000)

        result = await page.evaluate(script)
        return str(result)

    except Exception as e:
        return f"Error evaluating script on {url}: {e}"
    finally:
        if pw:
            try:
                if not cdp_url and browser:
                    await browser.close()
                await pw.stop()
            except Exception:
                pass
