"""Read-only permission status through the shared command registry.

Permission decisions, policy changes and actual-control observations require a
separate trusted service. This entry does not construct one or issue authority.
"""
from __future__ import annotations

import asyncio
from html import escape
from typing import Any

from orchestrator import ui_language
from orchestrator.command_registry import RuntimeCommand
from orchestrator.command_ui import card_title


def _authorized(runtime: Any, update: Any) -> bool:
    checker = getattr(runtime, "_is_authorized_user", None)
    actor = getattr(getattr(update, "effective_user", None), "id", None)
    return bool(callable(checker) and checker(actor))


def _read_level(runtime: Any) -> str:
    # Only the existing configuration owner interprets and reads this setting.
    from orchestrator.config_admin import ConfigAdmin

    config = getattr(runtime, "global_config", None)
    if config is None or getattr(config, "config_path", None) is None:
        raise ValueError("agent configuration is unavailable")
    snapshot = ConfigAdmin(config).read_agent_permission_settings(runtime.name)
    return snapshot.settings.requested_level.value


async def safety_command(runtime: Any, update: Any, context: Any) -> None:
    if not _authorized(runtime, update):
        return
    args = [str(item).strip().casefold() for item in (getattr(context, "args", None) or [])]
    if args and args != ["status"]:
        await runtime._reply_text(update, ui_language.tr("safety.action_unavailable"))
        return
    try:
        level = await asyncio.to_thread(_read_level, runtime)
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        await runtime._reply_text(update, ui_language.tr("safety.config_unavailable"))
        return
    text = "\n".join((
        card_title("🛡️", ui_language.tr("safety.title")),
        "",
        f"<b>{escape(ui_language.tr('safety.configured'))}</b> · <code>{escape(level)}</code>",
        escape(ui_language.tr("safety.control_unknown")),
    ))
    await runtime._reply_text(update, text, parse_mode="HTML")


COMMANDS = [RuntimeCommand("safety", "View permission status", safety_command)]
CALLBACKS = []
