from __future__ import annotations

from dataclasses import dataclass
from html import escape

from orchestrator import ui_language
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
        f"<b>{ui_language.tr('common.current')}</b> · {ui_language.tr('browser.reference')}\n\n"
        f"<b>{ui_language.tr('browser.example.headless')}</b>\n"
        f"{ui_language.tr('browser.example.headless_command')}\n\n"
        f"<b>{ui_language.tr('browser.example.native')}</b>\n"
        f"{ui_language.tr('browser.example.native_command')}\n\n"
        f"<b>{ui_language.tr('browser.example.search')}</b>\n"
        f"{ui_language.tr('browser.example.search_command')}\n\n"
        f"<b>{ui_language.tr('browser.example.logged_in')}</b>\n"
        f"{ui_language.tr('browser.example.logged_in_command')}"
    )


def get_browser_status_text(
    *,
    active_backend: str | None = None,
    brave_configured: bool | None = None,
    extension_bridge_configured: bool | None = None,
) -> str:
    backend = (active_backend or "unknown").strip() or "unknown"
    native_status = ui_language.tr(
        "browser.native.available"
        if backend in CLI_NATIVE_BROWSER_BACKENDS
        else "browser.native.unknown"
    )

    if brave_configured is None:
        brave_icon = "🟡"
        brave_status = ui_language.tr("browser.not_checked")
    else:
        brave_icon = "🟢" if brave_configured else "🔴"
        brave_status = ui_language.tr(
            "browser.configured" if brave_configured else "browser.missing_brave"
        )

    if extension_bridge_configured is None:
        extension_icon = "🟡"
        extension_status = ui_language.tr("browser.not_checked")
    else:
        extension_icon = "🟢" if extension_bridge_configured else "🔴"
        extension_status = ui_language.tr(
            "browser.extension.connected"
            if extension_bridge_configured
            else "browser.extension.unavailable"
        )

    native_icon = "🟢" if backend in CLI_NATIVE_BROWSER_BACKENDS else "🟡"
    headless_icon = "🟢"
    headless_status = ui_language.tr("browser.available")

    return (
        f"{card_title('🌐', 'Browser routes')}\n\n"
        f"<b>{ui_language.tr('common.current')}</b> · "
        f"{ui_language.tr('common.backend')} <code>{escape(backend)}</code>\n"
        f"<b>{ui_language.tr('common.changes')}</b> · {ui_language.tr('browser.changes')}\n\n"
        f"{ui_language.tr('browser.legend')}\n\n"
        f"<b>{ui_language.tr('browser.routes')}</b>\n"
        f"{headless_icon} <b>1 · {ui_language.tr('browser.route.headless')}</b> · {headless_status}\n"
        f"   {ui_language.tr('browser.route.headless_desc')}\n"
        f"{native_icon} <b>2 · {ui_language.tr('browser.route.native')}</b> · {native_status}\n"
        f"   {ui_language.tr('browser.route.native_desc', backend=f'<code>{escape(backend)}</code>')}\n"
        f"{brave_icon} <b>3 · {ui_language.tr('browser.route.search')}</b> · {brave_status}\n"
        f"   {ui_language.tr('browser.route.search_desc')}\n"
        f"{extension_icon} <b>4 · {ui_language.tr('browser.route.logged_in')}</b> · {extension_status}\n"
        f"   {ui_language.tr('browser.route.logged_in_desc')}\n\n"
        f"<b>{ui_language.tr('common.use')}</b>\n"
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
