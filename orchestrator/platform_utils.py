# orchestrator/platform_utils.py
from __future__ import annotations
import sys

IS_WINDOWS = sys.platform == "win32"
IS_MACOS = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")
IS_POSIX = not IS_WINDOWS


def default_tts_provider() -> str:
    """Return the best default TTS provider for this platform."""
    if IS_WINDOWS:
        return "windows"
    if IS_MACOS:
        return "macos"
    return "edge"


def open_url(url: str) -> None:
    """Open a URL in the default browser, cross-platform."""
    import subprocess
    if IS_WINDOWS:
        subprocess.Popen(["start", url], shell=True)
    elif IS_MACOS:
        subprocess.Popen(["open", url])
    else:
        subprocess.Popen(["xdg-open", url])