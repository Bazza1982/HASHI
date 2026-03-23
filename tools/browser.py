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


async def execute_browser_scroll(args: dict) -> str:
    """
    Scroll the page or a specific element.
    Use x/y for pixel offset, or selector to scroll an element into view.
    """
    url = str(args.get("url", "")).strip()
    if not url:
        return "Error: url is required"

    cdp_url = args.get("cdp_url")
    headed = bool(args.get("headed", False))
    selector = args.get("selector")
    x = int(args.get("x", 0))
    y = int(args.get("y", 500))

    pw = browser = context = page = None
    try:
        pw, browser, context, page = await _get_page(cdp_url=cdp_url, headed=headed)
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)

        if selector:
            el = await page.query_selector(selector)
            if el:
                await el.scroll_into_view_if_needed()
                return f"OK: scrolled '{selector}' into view on {url}"
            else:
                return f"Error: selector '{selector}' not found"
        else:
            await page.mouse.wheel(x, y)
            return f"OK: scrolled by ({x}, {y}) on {url}"

    except Exception as e:
        return f"Error scrolling on {url}: {e}"
    finally:
        if pw:
            try:
                if not cdp_url and browser:
                    await browser.close()
                await pw.stop()
            except Exception:
                pass


async def execute_browser_hover(args: dict) -> str:
    """Hover the mouse over an element by CSS selector."""
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
        await page.hover(selector, timeout=10000)
        await asyncio.sleep(0.3)
        return f"OK: hovered over '{selector}' on {url}"

    except Exception as e:
        return f"Error hovering '{selector}' on {url}: {e}"
    finally:
        if pw:
            try:
                if not cdp_url and browser:
                    await browser.close()
                await pw.stop()
            except Exception:
                pass


async def execute_browser_key(args: dict) -> str:
    """
    Press one or more keyboard keys on a page (or focused element).
    key examples: "Enter", "Tab", "Escape", "ArrowDown", "Control+a", "Control+c"
    """
    url = str(args.get("url", "")).strip()
    key = str(args.get("key", "")).strip()
    if not url:
        return "Error: url is required"
    if not key:
        return "Error: key is required"

    cdp_url = args.get("cdp_url")
    headed = bool(args.get("headed", False))
    selector = args.get("selector")  # optional: focus this element first

    pw = browser = context = page = None
    try:
        pw, browser, context, page = await _get_page(cdp_url=cdp_url, headed=headed)
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        if selector:
            await page.click(selector, timeout=10000)
        await page.keyboard.press(key)
        await asyncio.sleep(0.3)
        return f"OK: pressed '{key}' on {url}"

    except Exception as e:
        return f"Error pressing '{key}' on {url}: {e}"
    finally:
        if pw:
            try:
                if not cdp_url and browser:
                    await browser.close()
                await pw.stop()
            except Exception:
                pass


async def execute_browser_select(args: dict) -> str:
    """Select an option from a <select> dropdown by value, label, or index."""
    url = str(args.get("url", "")).strip()
    selector = str(args.get("selector", "")).strip()
    if not url:
        return "Error: url is required"
    if not selector:
        return "Error: selector is required"

    cdp_url = args.get("cdp_url")
    headed = bool(args.get("headed", False))
    value = args.get("value")
    label = args.get("label")
    index = args.get("index")

    if value is None and label is None and index is None:
        return "Error: one of value, label, or index is required"

    pw = browser = context = page = None
    try:
        pw, browser, context, page = await _get_page(cdp_url=cdp_url, headed=headed)
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)

        select_args: dict = {}
        if value is not None:
            select_args["value"] = str(value)
        elif label is not None:
            select_args["label"] = str(label)
        elif index is not None:
            select_args["index"] = int(index)

        selected = await page.select_option(selector, **select_args, timeout=10000)
        return f"OK: selected {selected} in '{selector}' on {url}"

    except Exception as e:
        return f"Error selecting in '{selector}' on {url}: {e}"
    finally:
        if pw:
            try:
                if not cdp_url and browser:
                    await browser.close()
                await pw.stop()
            except Exception:
                pass


async def execute_browser_wait_for(args: dict) -> str:
    """
    Navigate to a URL and wait until a CSS selector appears in the DOM.
    Returns element's innerText once found.
    """
    url = str(args.get("url", "")).strip()
    selector = str(args.get("selector", "")).strip()
    if not url:
        return "Error: url is required"
    if not selector:
        return "Error: selector is required"

    cdp_url = args.get("cdp_url")
    headed = bool(args.get("headed", False))
    timeout_ms = int(args.get("timeout_ms", 10000))

    pw = browser = context = page = None
    try:
        pw, browser, context, page = await _get_page(cdp_url=cdp_url, headed=headed)
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        el = await page.wait_for_selector(selector, timeout=timeout_ms)
        text = await el.inner_text() if el else ""
        return f"OK: found '{selector}' — text: {text[:500]}"

    except Exception as e:
        return f"Error waiting for '{selector}' on {url}: {e}"
    finally:
        if pw:
            try:
                if not cdp_url and browser:
                    await browser.close()
                await pw.stop()
            except Exception:
                pass


async def execute_browser_get_attribute(args: dict) -> str:
    """Get the value of an HTML attribute from an element."""
    url = str(args.get("url", "")).strip()
    selector = str(args.get("selector", "")).strip()
    attribute = str(args.get("attribute", "")).strip()
    if not url:
        return "Error: url is required"
    if not selector:
        return "Error: selector is required"
    if not attribute:
        return "Error: attribute is required"

    cdp_url = args.get("cdp_url")
    headed = bool(args.get("headed", False))

    pw = browser = context = page = None
    try:
        pw, browser, context, page = await _get_page(cdp_url=cdp_url, headed=headed)
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        value = await page.get_attribute(selector, attribute, timeout=10000)
        if value is None:
            return f"Error: attribute '{attribute}' not found on '{selector}'"
        return value

    except Exception as e:
        return f"Error getting attribute '{attribute}' from '{selector}' on {url}: {e}"
    finally:
        if pw:
            try:
                if not cdp_url and browser:
                    await browser.close()
                await pw.stop()
            except Exception:
                pass


async def execute_browser_drag(args: dict) -> str:
    """Drag an element from source selector to target selector."""
    url = str(args.get("url", "")).strip()
    source = str(args.get("source", "")).strip()
    target = str(args.get("target", "")).strip()
    if not url:
        return "Error: url is required"
    if not source or not target:
        return "Error: source and target selectors are required"

    cdp_url = args.get("cdp_url")
    headed = bool(args.get("headed", False))

    pw = browser = context = page = None
    try:
        pw, browser, context, page = await _get_page(cdp_url=cdp_url, headed=headed)
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await page.drag_and_drop(source, target, timeout=15000)
        return f"OK: dragged '{source}' → '{target}' on {url}"

    except Exception as e:
        return f"Error dragging '{source}' → '{target}' on {url}: {e}"
    finally:
        if pw:
            try:
                if not cdp_url and browser:
                    await browser.close()
                await pw.stop()
            except Exception:
                pass


async def execute_browser_upload(args: dict) -> str:
    """Upload a file to a file input element."""
    url = str(args.get("url", "")).strip()
    selector = str(args.get("selector", "")).strip()
    file_path = str(args.get("file_path", "")).strip()
    if not url:
        return "Error: url is required"
    if not selector:
        return "Error: selector is required"
    if not file_path:
        return "Error: file_path is required"

    cdp_url = args.get("cdp_url")
    headed = bool(args.get("headed", False))

    pw = browser = context = page = None
    try:
        pw, browser, context, page = await _get_page(cdp_url=cdp_url, headed=headed)
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await page.set_input_files(selector, file_path, timeout=10000)
        return f"OK: uploaded '{file_path}' to '{selector}' on {url}"

    except Exception as e:
        return f"Error uploading file to '{selector}' on {url}: {e}"
    finally:
        if pw:
            try:
                if not cdp_url and browser:
                    await browser.close()
                await pw.stop()
            except Exception:
                pass


async def execute_browser_session(args: dict) -> str:
    """
    Execute a multi-step browser session — multiple actions on the same page
    without reloading between steps.

    steps: list of action dicts, each with an "action" key:
      { "action": "goto",         "url": "..." }
      { "action": "click",        "selector": "..." }
      { "action": "fill",         "selector": "...", "text": "..." }
      { "action": "submit" }                        # press Enter
      { "action": "key",          "key": "Tab" }
      { "action": "scroll",       "x": 0, "y": 500 }
      { "action": "scroll_to",    "selector": "..." }
      { "action": "hover",        "selector": "..." }
      { "action": "select",       "selector": "...", "value"|"label"|"index": ... }
      { "action": "wait_for",     "selector": "...", "timeout_ms": 5000 }
      { "action": "screenshot",   "full_page": false }   -> appends base64 to results
      { "action": "get_text" }                           -> appends visible text
      { "action": "evaluate",     "script": "() => ..." }
      { "action": "wait",         "ms": 1000 }
      { "action": "back" }
      { "action": "forward" }
      { "action": "reload" }

    Returns a summary of each step result.
    """
    url = str(args.get("url", "")).strip()
    steps = args.get("steps", [])
    if not url and not steps:
        return "Error: provide url (initial) and/or steps"

    cdp_url = args.get("cdp_url")
    headed = bool(args.get("headed", False))

    pw = browser = context = page = None
    results: list[str] = []

    try:
        pw, browser, context, page = await _get_page(cdp_url=cdp_url, headed=headed)

        # Navigate to initial URL if provided
        if url:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            results.append(f"[goto] {url}")

        for i, step in enumerate(steps):
            action = str(step.get("action", "")).strip()
            try:
                if action == "goto":
                    dest = str(step["url"])
                    await page.goto(dest, wait_until="domcontentloaded", timeout=30000)
                    results.append(f"[goto] {dest}")

                elif action == "click":
                    sel = str(step["selector"])
                    await page.click(sel, timeout=10000)
                    await asyncio.sleep(0.3)
                    results.append(f"[click] {sel}")

                elif action == "fill":
                    sel = str(step["selector"])
                    text = str(step.get("text", ""))
                    await page.fill(sel, text, timeout=10000)
                    results.append(f"[fill] {sel} = '{text}'")

                elif action == "submit":
                    await page.keyboard.press("Enter")
                    await asyncio.sleep(0.5)
                    results.append("[submit] Enter pressed")

                elif action == "key":
                    key = str(step["key"])
                    sel = step.get("selector")
                    if sel:
                        await page.click(sel, timeout=5000)
                    await page.keyboard.press(key)
                    await asyncio.sleep(0.2)
                    results.append(f"[key] {key}")

                elif action == "scroll":
                    x = int(step.get("x", 0))
                    y = int(step.get("y", 500))
                    await page.mouse.wheel(x, y)
                    results.append(f"[scroll] ({x}, {y})")

                elif action == "scroll_to":
                    sel = str(step["selector"])
                    el = await page.query_selector(sel)
                    if el:
                        await el.scroll_into_view_if_needed()
                        results.append(f"[scroll_to] {sel}")
                    else:
                        results.append(f"[scroll_to] ERROR: '{sel}' not found")

                elif action == "hover":
                    sel = str(step["selector"])
                    await page.hover(sel, timeout=10000)
                    await asyncio.sleep(0.2)
                    results.append(f"[hover] {sel}")

                elif action == "select":
                    sel = str(step["selector"])
                    select_args: dict = {}
                    if "value" in step:
                        select_args["value"] = str(step["value"])
                    elif "label" in step:
                        select_args["label"] = str(step["label"])
                    elif "index" in step:
                        select_args["index"] = int(step["index"])
                    selected = await page.select_option(sel, **select_args, timeout=10000)
                    results.append(f"[select] {sel} → {selected}")

                elif action == "wait_for":
                    sel = str(step["selector"])
                    timeout_ms = int(step.get("timeout_ms", 10000))
                    el = await page.wait_for_selector(sel, timeout=timeout_ms)
                    text = (await el.inner_text())[:200] if el else ""
                    results.append(f"[wait_for] {sel} found — '{text}'")

                elif action == "screenshot":
                    full_page = bool(step.get("full_page", False))
                    png = await page.screenshot(full_page=full_page)
                    b64 = base64.b64encode(png).decode()
                    results.append(f"[screenshot] base64:{b64}")

                elif action == "get_text":
                    text = await page.evaluate("() => document.body.innerText")
                    max_len = int(step.get("max_length", 5000))
                    results.append(f"[get_text] {text.strip()[:max_len]}")

                elif action == "evaluate":
                    script = str(step["script"])
                    result = await page.evaluate(script)
                    results.append(f"[evaluate] {result}")

                elif action == "wait":
                    ms = int(step.get("ms", 1000))
                    await asyncio.sleep(ms / 1000)
                    results.append(f"[wait] {ms}ms")

                elif action == "back":
                    await page.go_back()
                    results.append("[back]")

                elif action == "forward":
                    await page.go_forward()
                    results.append("[forward]")

                elif action == "reload":
                    await page.reload()
                    results.append("[reload]")

                else:
                    results.append(f"[step {i}] ERROR: unknown action '{action}'")

            except Exception as step_err:
                results.append(f"[{action}] ERROR: {step_err}")

        return "\n".join(results)

    except Exception as e:
        return f"Error in browser session: {e}"
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
