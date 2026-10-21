"""Instance-owned routing settings for Agent Presence ingress.

Agent Presence chooses one HASHI instance. Once a request reaches this
instance, HASHI alone chooses the receiving Agent from this revision-checked
instance setting. The AP caller never supplies routing authority.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from orchestrator.config_json import (
    ConfigDocument,
    new_config_json,
    read_config_json,
    write_config_json,
)


STATE_FILENAME = "agentpresence_ingress.json"
SCHEMA_VERSION = 1


def state_path(bridge_home: Any) -> Path:
    return Path(bridge_home) / "state" / STATE_FILENAME


def _empty_state() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "revision": 0,
        "default_agent": "",
    }


def _normalize_state(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict) or raw.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("unsupported Agent Presence ingress settings")
    revision = raw.get("revision")
    default_agent = raw.get("default_agent")
    if type(revision) is not int or revision < 0:
        raise ValueError("invalid Agent Presence ingress settings revision")
    if not isinstance(default_agent, str):
        raise ValueError("invalid Agent Presence default Agent")
    normalized_agent = default_agent.strip().casefold()
    if len(normalized_agent) > 64 or any(char.isspace() for char in normalized_agent):
        raise ValueError("invalid Agent Presence default Agent")
    return {
        "schema_version": SCHEMA_VERSION,
        "revision": revision,
        "default_agent": normalized_agent,
    }


def _read_snapshot(
    bridge_home: Any,
) -> tuple[dict[str, Any], ConfigDocument | None]:
    target = state_path(bridge_home)
    try:
        document = read_config_json(target)
    except FileNotFoundError:
        return _empty_state(), None
    return _normalize_state(document), document


def load_settings(bridge_home: Any) -> dict[str, Any]:
    state, _document = _read_snapshot(bridge_home)
    return state


def get_default_agent(bridge_home: Any) -> str:
    return str(load_settings(bridge_home)["default_agent"])


def set_default_agent(bridge_home: Any, agent_id: Any = "") -> dict[str, Any]:
    agent = str(agent_id or "").strip().casefold()
    if len(agent) > 64 or any(char.isspace() for char in agent):
        raise ValueError("invalid Agent Presence default Agent")

    state, document = _read_snapshot(bridge_home)
    state["default_agent"] = agent
    state["revision"] += 1
    target = state_path(bridge_home)
    target.parent.mkdir(parents=True, exist_ok=True)
    if document is None:
        write_config_json(target, new_config_json(target, state))
    else:
        write_config_json(target, state, expected_revision=document.revision)
    return state


__all__ = [
    "SCHEMA_VERSION",
    "STATE_FILENAME",
    "get_default_agent",
    "load_settings",
    "set_default_agent",
    "state_path",
]
