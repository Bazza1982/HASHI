"""Telegram actor resolution and legacy local Session ownership lookup.

The legacy owner-scope value is a data key, not an authentication credential.
It is read for continuity while connector-neutral identity is migrated.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def _optional_positive_id(value: Any, *, field: str) -> int | None:
    if value in (None, "", 0, "0"):
        return None
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a positive numeric user ID")
    try:
        resolved = int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"{field} must be a positive numeric user ID"
        ) from exc
    if resolved <= 0:
        raise ValueError(f"{field} must be a positive numeric user ID")
    return resolved


def resolve_telegram_owner_id(
    global_config: Mapping[str, Any],
    secrets: Mapping[str, Any],
) -> int | None:
    """Resolve one Telegram owner using the canonical compatibility order."""

    disabled = global_config.get("telegram_primary_disabled", False)
    if not isinstance(disabled, bool):
        raise ValueError("global.telegram_primary_disabled must be boolean")
    if disabled:
        # Explicitly revoking the primary must not revive a legacy secret.
        return None
    candidates = (
        ("global.telegram_owner_id", global_config.get("telegram_owner_id")),
        ("secrets.authorized_telegram_id", secrets.get("authorized_telegram_id")),
        ("global.authorized_id", global_config.get("authorized_id")),
    )
    for field, value in candidates:
        resolved = _optional_positive_id(value, field=field)
        if resolved is not None:
            return resolved
    return None


def resolve_owner_scope_id(
    global_config: Mapping[str, Any],
    telegram_owner_id: int | None,
) -> str:
    """Locate existing local data without imposing an owner-format gate."""

    scope = str(global_config.get("owner_scope_id") or "").strip()
    if not scope:
        scope = f"user:{telegram_owner_id or 0}"
    return scope


__all__ = ["resolve_owner_scope_id", "resolve_telegram_owner_id"]
