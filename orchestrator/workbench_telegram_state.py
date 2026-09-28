"""Compatibility facade for the retired Workbench Telegram state module.

New state is stored by the connector-neutral preference service. This module
remains as a narrow adapter for existing command and admission call sites.
"""
from __future__ import annotations

from typing import Any

from orchestrator.connector_delivery_preferences import (
    DEFAULT_MIRROR,
    LEGACY_STATE_FILENAME,
    STATE_FILENAME,
    get_connector_preference,
    legacy_state_path,
    load_preferences,
    set_connector_preference,
    state_path,
)


def load_state(bridge_home: Any) -> dict[str, Any]:
    try:
        state = load_preferences(bridge_home)
    except (OSError, TypeError, ValueError):
        # Reads retain the historical visible default. Mutations still fail
        # closed in set_connector_preference and never replace corrupt data.
        state = {"revision": 0, "owners": {}}
    return load_state_from_snapshot(state)


def _legacy_owner_view(state: dict[str, Any]) -> dict[str, Any]:
    owners: dict[str, bool] = {}
    for owner, owner_entry in state["owners"].items():
        value = (
            owner_entry.get("connectors", {})
            .get("telegram", {})
            .get("mirror")
        )
        if type(value) is bool:
            owners[owner] = value
    return {"revision": state["revision"], "owners": owners}


def load_state_from_snapshot(state: dict[str, Any]) -> dict[str, Any]:
    return _legacy_owner_view(state)


def mirror_enabled(
    bridge_home: Any,
    owner_id: Any,
    *,
    default: bool = DEFAULT_MIRROR,
) -> bool:
    return get_connector_preference(
        bridge_home,
        owner_id,
        "telegram",
        "mirror",
        default=default,
    )


def set_mirror(bridge_home: Any, owner_id: Any, enabled: bool) -> dict[str, Any]:
    state = set_connector_preference(
        bridge_home, owner_id, "telegram", "mirror", enabled
    )
    return load_state_from_snapshot(state)


def parse_mirror_arg(args: Any) -> bool | None:
    """Parse /telegram arguments, retaining the historical command syntax."""
    tokens = [str(token).strip() for token in (args or [])]
    if not tokens:
        return None
    if len(tokens) == 1:
        value = tokens[0].casefold()
        if value == "on":
            return True
        if value == "off":
            return False
    raise ValueError("Usage: /telegram on|off")


__all__ = [
    "DEFAULT_MIRROR",
    "LEGACY_STATE_FILENAME",
    "STATE_FILENAME",
    "legacy_state_path",
    "load_state",
    "mirror_enabled",
    "parse_mirror_arg",
    "set_mirror",
    "state_path",
]
