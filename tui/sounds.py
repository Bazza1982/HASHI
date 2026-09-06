"""Small, dependency-free notification sounds for the HASHI TUI."""
from __future__ import annotations

import logging
import os
import sys

logger = logging.getLogger(__name__)

MESSAGE_SOUND_ALIASES = {
    "sent": "SystemQuestion",
    "received": "SystemAsterisk",
}
_DISABLED_VALUES = frozenset({"0", "false", "no", "off"})


def _play_windows_alias(alias: str) -> None:
    import winsound

    flags = winsound.SND_ALIAS | winsound.SND_ASYNC
    flags |= getattr(winsound, "SND_SYSTEM", 0)
    winsound.PlaySound(alias, flags)


def play_message_sound(event: str) -> bool:
    """Play an asynchronous Windows sound for a sent or received message."""

    alias = MESSAGE_SOUND_ALIASES.get(str(event or "").strip().casefold())
    enabled = os.environ.get("HASHI_TUI_SOUNDS", "1").strip().casefold()
    if not alias or enabled in _DISABLED_VALUES or sys.platform != "win32":
        return False
    try:
        _play_windows_alias(alias)
    except (ImportError, OSError, RuntimeError) as exc:
        logger.debug("TUI message sound unavailable: event=%s error=%s", event, exc)
        return False
    return True
