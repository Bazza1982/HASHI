"""Revision-checked, owner-scoped preferences for connector delivery.

The store is connector-neutral.  The former Workbench-only Telegram mirror
setting is retained as a compatibility view and migrated lazily on first
write; reads never rewrite legacy state.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from orchestrator.config_json import (
    ConfigDocument,
    new_config_json,
    read_config_json,
    write_config_json,
)

STATE_FILENAME = "frontend_delivery_preferences.json"
LEGACY_STATE_FILENAME = "workbench_telegram_state.json"
DEFAULT_MIRROR = True
_CONNECTOR_ID = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_PREFERENCE_KEY = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")


def state_path(bridge_home: Any) -> Path:
    return Path(bridge_home) / "state" / STATE_FILENAME


def legacy_state_path(bridge_home: Any) -> Path:
    return Path(bridge_home) / "state" / LEGACY_STATE_FILENAME


def _empty_state() -> dict[str, Any]:
    return {"schema_version": 1, "revision": 0, "owners": {}}


def _normalize_state(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        raise ValueError("unsupported frontend delivery preference document")
    revision = raw.get("revision")
    owners = raw.get("owners")
    if type(revision) is not int or revision < 0 or not isinstance(owners, dict):
        raise ValueError("invalid frontend delivery preference document")
    normalized: dict[str, Any] = _empty_state()
    normalized["revision"] = revision
    for owner, owner_value in owners.items():
        owner_id = str(owner).strip()
        if not owner_id or not isinstance(owner_value, dict):
            raise ValueError("invalid owner delivery preferences")
        connectors = owner_value.get("connectors")
        if not isinstance(connectors, dict):
            raise ValueError("invalid connector delivery preferences")
        normalized_connectors: dict[str, dict[str, bool]] = {}
        for connector_id, preferences in connectors.items():
            connector = str(connector_id).strip().casefold()
            if not _CONNECTOR_ID.fullmatch(connector) or not isinstance(preferences, dict):
                raise ValueError("invalid connector preference entry")
            normalized_preferences: dict[str, bool] = {}
            for key, value in preferences.items():
                preference_key = str(key).strip().casefold()
                if not _PREFERENCE_KEY.fullmatch(preference_key) or type(value) is not bool:
                    raise ValueError("connector preferences must be named boolean values")
                normalized_preferences[preference_key] = value
            normalized_connectors[connector] = normalized_preferences
        normalized["owners"][owner_id] = {"connectors": normalized_connectors}
    return normalized


def _legacy_state(bridge_home: Any) -> dict[str, Any]:
    path = legacy_state_path(bridge_home)
    if not path.is_file():
        return _empty_state()
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return _empty_state()
    if not isinstance(raw, dict) or not isinstance(raw.get("owners"), dict):
        return _empty_state()
    revision = raw.get("revision", 0)
    if type(revision) is not int or revision < 0:
        revision = 0
    migrated = _empty_state()
    migrated["revision"] = revision
    for owner, value in raw["owners"].items():
        owner_id = str(owner).strip()
        if owner_id and type(value) is bool:
            migrated["owners"][owner_id] = {
                "connectors": {"telegram": {"mirror": value}}
            }
    return migrated


def _read_snapshot(bridge_home: Any) -> tuple[dict[str, Any], ConfigDocument | None]:
    path = state_path(bridge_home)
    try:
        document = read_config_json(path)
    except FileNotFoundError:
        return _legacy_state(bridge_home), None
    return _normalize_state(document), document


def load_preferences(bridge_home: Any) -> dict[str, Any]:
    state, _document = _read_snapshot(bridge_home)
    return state


def get_connector_preference(
    bridge_home: Any,
    owner_id: Any,
    connector_id: str,
    preference: str,
    *,
    default: bool,
) -> bool:
    try:
        state = load_preferences(bridge_home)
        owner = state["owners"].get(str(owner_id).strip(), {})
        connector = owner.get("connectors", {}).get(str(connector_id).strip().casefold(), {})
        value = connector.get(str(preference).strip().casefold(), default)
        return value if type(value) is bool else default
    except (OSError, ValueError, TypeError):
        # Preserve the established visible default for reads.  Mutations below
        # deliberately propagate corruption and durability errors.
        return default


def set_connector_preference(
    bridge_home: Any,
    owner_id: Any,
    connector_id: str,
    preference: str,
    enabled: bool,
) -> dict[str, Any]:
    owner = str(owner_id).strip()
    connector = str(connector_id).strip().casefold()
    key = str(preference).strip().casefold()
    if not owner:
        raise ValueError("owner_id is required")
    if not _CONNECTOR_ID.fullmatch(connector):
        raise ValueError("connector_id is invalid")
    if not _PREFERENCE_KEY.fullmatch(key):
        raise ValueError("preference name is invalid")
    if type(enabled) is not bool:
        raise TypeError("preference value must be boolean")

    state, document = _read_snapshot(bridge_home)
    owner_entry = state["owners"].setdefault(owner, {"connectors": {}})
    connector_entry = owner_entry["connectors"].setdefault(connector, {})
    connector_entry[key] = enabled
    state["revision"] += 1
    target = state_path(bridge_home)
    target.parent.mkdir(parents=True, exist_ok=True)
    if document is None:
        snapshot = new_config_json(target, state)
        write_config_json(target, snapshot)
    else:
        write_config_json(target, state, expected_revision=document.revision)
    return state


__all__ = [
    "DEFAULT_MIRROR",
    "LEGACY_STATE_FILENAME",
    "STATE_FILENAME",
    "get_connector_preference",
    "legacy_state_path",
    "load_preferences",
    "set_connector_preference",
    "state_path",
]
