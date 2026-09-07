"""Small, dependency-free notification sounds for the HASHI TUI."""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

_SOUND_ROOT = Path(__file__).resolve().parent / "assets" / "sounds"
MESSAGE_SOUND_FILES = {
    "sent": _SOUND_ROOT / "soft_chat_send.wav",
    "received": _SOUND_ROOT / "soft_chat_receive.wav",
}
_DISABLED_VALUES = frozenset({"0", "false", "no", "off"})


def _play_windows_file(path: Path) -> None:
    import winsound

    flags = winsound.SND_FILENAME | winsound.SND_ASYNC
    flags |= getattr(winsound, "SND_NODEFAULT", 0)
    flags |= getattr(winsound, "SND_SYSTEM", 0)
    winsound.PlaySound(str(path), flags)


def play_message_sound(event: str) -> bool:
    """Play an asynchronous Windows sound for a sent or received message."""

    path = MESSAGE_SOUND_FILES.get(str(event or "").strip().casefold())
    enabled = os.environ.get("HASHI_TUI_SOUNDS", "1").strip().casefold()
    if (
        path is None
        or enabled in _DISABLED_VALUES
        or sys.platform != "win32"
        or not path.is_file()
    ):
        return False
    try:
        _play_windows_file(path)
    except (ImportError, OSError, RuntimeError) as exc:
        logger.debug("TUI message sound unavailable: event=%s error=%s", event, exc)
        return False
    return True
