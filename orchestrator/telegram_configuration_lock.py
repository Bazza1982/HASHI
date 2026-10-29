"""Serialize Telegram setup transactions before taking configuration locks."""
from __future__ import annotations

from pathlib import Path

from orchestrator.config_json import file_write_lock


def telegram_configuration_lock(secrets_path: str | Path):
    """Use one lock for every transaction sharing this secrets document."""
    path = Path(secrets_path).expanduser()
    return file_write_lock(path.with_name(path.name + ".telegram-setup"))
