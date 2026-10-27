"""Canonical Telegram actor and stable PAO owner-scope resolution.

Telegram account identity is replaceable.  The PAO owner scope is not: it
keeps the same human's Sessions, Workzones, and connector preferences stable
across an account migration.
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
    """Return the stable PAO owner scope, deriving it only for legacy config."""

    scope = str(global_config.get("owner_scope_id") or "").strip()
    if not scope:
        scope = f"user:{telegram_owner_id or 0}"
    if len(scope) > 256 or any(ord(character) < 33 for character in scope):
        raise ValueError("global.owner_scope_id is invalid")
    return scope


__all__ = ["resolve_owner_scope_id", "resolve_telegram_owner_id"]
