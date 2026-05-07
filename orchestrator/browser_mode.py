from __future__ import annotations

from dataclasses import dataclass


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
    return (
        "Usage:\n"
        "/browser - show these options\n"
        "/browser status - show route availability notes\n"
        "/browser examples - show example prompts\n"
        "/browser <1-4> <task> - run an internet task with a specific route\n\n"
        "Routes:\n"
        "1. HASHI headless browser - standalone Playwright/browser tools for public or JS-heavy pages\n"
        "2. CLI backend native browsing - backend-native browsing/search for Codex, Claude, Gemini CLI\n"
        "3. Brave search - HASHI web_search plus web_fetch for public web research\n"
        "4. HASHI browser extension - logged-in Windows browser for authenticated work"
    )


def get_browser_examples_text() -> str:
    return (
        "Examples:\n"
        "/browser status\n"
        "/browser 1 Inspect this public dashboard and summarize the visible table.\n"
        "/browser 2 Research this topic using the CLI backend's own browsing tools.\n"
        "/browser 3 Find recent sources about mandatory CSR assurance and cite the strongest ones.\n"
        "/browser 4 Open the logged-in library page and download the PDF I am entitled to access."
    )


def get_browser_status_text(
    *,
    active_backend: str | None = None,
    brave_configured: bool | None = None,
    extension_bridge_configured: bool | None = None,
) -> str:
    backend = (active_backend or "unknown").strip() or "unknown"
    native_status = "available" if backend in CLI_NATIVE_BROWSER_BACKENDS else "instruction-only / not native for this backend"

    if brave_configured is None:
        brave_status = "not checked"
    else:
        brave_status = "configured" if brave_configured else "missing brave_api_key"

    if extension_bridge_configured is None:
        extension_status = "not checked"
    else:
        extension_status = "bridge socket present" if extension_bridge_configured else "bridge socket not detected"

    return (
        "/browser route status:\n"
        f"1. HASHI headless browser: available when browser tools/dependencies are installed\n"
        f"2. CLI backend native browsing: {native_status} (active backend: {backend})\n"
        f"3. Brave search: {brave_status}\n"
        f"4. HASHI browser extension: {extension_status}"
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
