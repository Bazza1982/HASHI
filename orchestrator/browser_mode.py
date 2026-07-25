from __future__ import annotations

from dataclasses import dataclass
from html import escape

from orchestrator.command_ui import card_title


BROWSER_MODE_SOURCE_PREFIX = "browser"

CLI_NATIVE_BROWSER_BACKENDS = frozenset({"codex-cli", "claude-cli", "gemini-cli"})


@dataclass(frozen=True)
class BrowserRoute:
    route_id: str
    name: str
    source: str
    summary: str
    instruction: str


BROWSER_ROUTES: dict[str, BrowserRoute] = {
    "1": BrowserRoute(
        route_id="1",
        name="HASHI headless browser",
        source="browser:headless",
        summary="Browser task via HASHI headless browser",
        instruction=(
            "Use HASHI browser tools in standalone/headless mode for public web pages, "
            "JavaScript-heavy pages, screenshots, extraction, and careful page interaction. "
            "Do not use the logged-in browser extension bridge for this route."
        ),
    ),
    "2": BrowserRoute(
        route_id="2",
        name="CLI backend native browsing",
        source="browser:native-cli",
        summary="Browser task via CLI-native browsing",
        instruction=(
            "Use the CLI backend's own browsing or search capability when it is available. "
            "This route is instruction-only from HASHI's perspective and is intended for "
            "Codex CLI, Claude CLI, and Gemini CLI backends."
        ),
    ),
    "3": BrowserRoute(
        route_id="3",
        name="Brave search",
        source="browser:brave",
        summary="Browser task via Brave search",
        instruction=(
            "Use HASHI web_search first for discovery, then web_fetch or other direct HTTP "
            "fetching for public source pages. Prefer source links and concise citations. "
            "Do not use browser GUI control unless the task later requires it."
        ),
    ),
    "4": BrowserRoute(
        route_id="4",
        name="HASHI browser extension",
        source="browser:extension",
        summary="Browser task via HASHI browser extension",
        instruction=(
            "Use the HASHI browser extension bridge for the real logged-in Windows browser "
            "when authentication, cookies, or the user's live browser state are required. "
            "Read and inspect freely when authorized by the task, but ask for explicit "
            "confirmation before destructive actions, submissions, purchases, account changes, "
            "or bulk edits."
        ),
    ),
}


def get_browser_menu_text() -> str:
    return get_browser_status_text()


def get_browser_examples_text() -> str:
    return (
        f"{card_title('🌐', 'Browser examples')}\n\n"
        "<b>1 · Headless page work</b>\n"
        "<code>/browser 1 Inspect this public dashboard and summarize the visible table.</code>\n\n"
        "<b>2 · CLI-native browsing</b>\n"
        "<code>/browser 2 Research this topic using the CLI backend's own browsing tools.</code>\n\n"
        "<b>3 · Brave search research</b>\n"
        "<code>/browser 3 Find recent sources about mandatory CSR assurance and cite the strongest ones.</code>\n\n"
        "<b>4 · Logged-in browser work</b>\n"
        "<code>/browser 4 Open the logged-in library page and download the PDF I may access.</code>"
    )


def get_browser_status_text(
    *,
    active_backend: str | None = None,
    brave_configured: bool | None = None,
    extension_bridge_configured: bool | None = None,
) -> str:
    backend = (active_backend or "unknown").strip() or "unknown"
    native_status = "available for this backend" if backend in CLI_NATIVE_BROWSER_BACKENDS else "not confirmed for this backend"

    if brave_configured is None:
        brave_icon = "🟡"
        brave_status = "not checked"
    else:
        brave_icon = "🟢" if brave_configured else "🔴"
        brave_status = "configured" if brave_configured else "missing `brave_api_key`"

    if extension_bridge_configured is None:
        extension_icon = "🟡"
        extension_status = "not checked"
    else:
        extension_icon = "🟢" if extension_bridge_configured else "🔴"
        extension_status = "bridge socket present" if extension_bridge_configured else "bridge socket not detected"

    native_icon = "🟢" if backend in CLI_NATIVE_BROWSER_BACKENDS else "🟡"
    headless_icon = "🟢"
    headless_status = "available"

    return (
        f"{card_title('🌐', 'Browser routes')}\n\n"
        f"<b>Current backend</b> · <code>{escape(backend)}</code>\n"
        "<b>Changes</b> · route selection applies to this request only\n\n"
        "🟢 confirmed online • 🟡 not checked / unknown • 🔴 offline or misconfigured\n\n"
        "<b>ROUTES</b>\n"
        f"{headless_icon} <b>1 · HEADLESS</b> · {headless_status}\n"
        "   Public web, JS pages, screenshots. Uses HASHI standalone Playwright/browser tools.\n"
        f"{native_icon} <b>2 · NATIVE</b> · {native_status}\n"
        f"   Backend-owned browsing/search. Active backend: <code>{escape(backend)}</code>.\n"
        f"{brave_icon} <b>3 · SEARCH</b> · {brave_status}\n"
        "   Public research with citations. Uses Brave search and source pages.\n"
        f"{extension_icon} <b>4 · LOGGED-IN</b> · {extension_status}\n"
        "   Real Windows browser session via HASHI extension for authenticated pages.\n\n"
        "<b>Use</b>\n"
        "<code>/browser &lt;1-4&gt; &lt;task&gt;</code>\n"
        "<code>/browser examples</code>"
    )


def build_browser_task_prompt(route_id: str, task: str) -> tuple[str, str, str]:
    route = BROWSER_ROUTES.get((route_id or "").strip())
    if route is None:
        raise ValueError("route must be one of 1, 2, 3, or 4")

    cleaned = (task or "").strip()
    if not cleaned:
        raise ValueError("task is required")

    prompt = (
        f"The user wants this handled in /browser route {route.route_id}: {route.name}.\n"
        f"{route.instruction}\n\n"
        "Task:\n"
        f"{cleaned}"
    ).strip()
    return prompt, route.source, route.summary
