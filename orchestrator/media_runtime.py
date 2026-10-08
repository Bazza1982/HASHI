"""Instance-owned external media executables; no interpreter dependencies."""
from __future__ import annotations

import os
from pathlib import Path

from orchestrator.config_json import read_config_json


def configured_media_executable(name: str, *, bridge_home: Path | None = None) -> str | None:
    """Read an optional private platform choice without writing a fallback."""
    home = bridge_home or os.environ.get("BRIDGE_HOME")
    if not home:
        return None
    config_path = Path(home) / "state" / "platform" / "media.json"
    try:
        document = read_config_json(config_path)
    except FileNotFoundError:
        return None
    if document.get("schema_version") != 1:
        raise ValueError("Unsupported media platform configuration")
    executables = document.get("executables")
    if not isinstance(executables, dict):
        raise ValueError("Invalid media platform executables")
    selected = executables.get(name)
    if selected is None:
        return None
    if not isinstance(selected, str) or not selected.strip():
        raise ValueError("Invalid configured media executable")
    executable = Path(selected).expanduser()
    if not executable.is_absolute() or not executable.is_file():
        raise ValueError(f"Configured {name} executable is unavailable")
    return str(executable)
