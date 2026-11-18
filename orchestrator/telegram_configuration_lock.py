"""Serialize Telegram setup transactions before taking configuration locks."""
from __future__ import annotations

from pathlib import Path

from orchestrator.config_json import file_write_lock

TELEGRAM_PENDING_SECRET_KEY = "__hashi_pending_telegram_credentials_v1"
TELEGRAM_PROTECTED_SECRET_KEYS = frozenset({
    "authorized_telegram_id",
    "workbench_admin_token",
    "hashi_remote_shared_token",
    TELEGRAM_PENDING_SECRET_KEY,
})


def telegram_configuration_lock(secrets_path: str | Path):
    """Use one lock for every transaction sharing this secrets document."""
    path = Path(secrets_path).expanduser()
    return file_write_lock(path.with_name(path.name + ".telegram-setup"))
