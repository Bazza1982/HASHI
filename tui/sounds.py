"""Small, dependency-free notification sounds for the HASHI TUI."""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
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


def _play_linux_file(path: Path) -> None:
    """Play through PulseAudio/ALSA without blocking the TUI event loop."""

    player = shutil.which("paplay") or shutil.which("aplay")
    if not player:
        raise OSError("no supported Linux audio player found")
    subprocess.Popen(
        [player, str(path)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def play_message_sound(event: str, *, enabled: bool | None = None) -> bool:
    """Play an asynchronous sound for a sent or received TUI message."""

    path = MESSAGE_SOUND_FILES.get(str(event or "").strip().casefold())
    if enabled is None:
        enabled = (
            os.environ.get("HASHI_TUI_SOUNDS", "1").strip().casefold()
            not in _DISABLED_VALUES
        )
    if path is None or not enabled or not path.is_file():
        return False
    try:
        if sys.platform == "win32":
            _play_windows_file(path)
        elif sys.platform.startswith("linux"):
            _play_linux_file(path)
        else:
            return False
    except (ImportError, OSError, RuntimeError) as exc:
        logger.debug("TUI message sound unavailable: event=%s error=%s", event, exc)
        return False
    return True
