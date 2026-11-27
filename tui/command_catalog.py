"""Runtime slash-command catalogue for the TUI, loaded without server imports.

The full registry (``orchestrator.command_registry``) imports
python-telegram-bot at module level and then loads every
``orchestrator.commands`` module. The TUI only needs command names and
descriptions for its palette, so that import is deferred to call time:

* when the server packages are present the real registry is used verbatim;
* when they are missing (rescue mode, broken venv, uninstalled packages) the
  TUI still starts with the built-in command specs and simply reports that the
  runtime command catalogue is unavailable.

Nothing here imports a server-side package at module scope, so
``import tui.app`` never drags Playwright, Telegram, cryptography or httpx into
a rescue session.
"""
from __future__ import annotations

import logging

logger = logging.getLogger("HASHI.TUI.CommandCatalog")


def load_runtime_command_info() -> tuple[list[dict[str, str]], str]:
    """Return ``(commands, notice)`` for the TUI command palette.

    ``commands`` holds ``{"name": ..., "description": ...}`` entries. ``notice``
    is an empty string on success and a short machine-readable reason when the
    registry could not be consulted.
    """

    try:
        from orchestrator.command_registry import load_runtime_commands
    except Exception as exc:  # rescue mode: server packages are not installed
        notice = f"runtime_command_catalog_unavailable:{type(exc).__name__}"
        logger.debug("TUI command palette degraded: %s", notice)
        return [], notice
    try:
        commands = [
            {"name": str(command.name), "description": str(command.description)}
            for command in load_runtime_commands()
        ]
    except Exception as exc:
        notice = f"runtime_command_catalog_failed:{type(exc).__name__}"
        logger.debug("TUI command palette degraded: %s", notice)
        return [], notice
    return commands, ""
# HASHI-Fingerprint: hfp_06764a0a73e64e15b352117f859c1d85
