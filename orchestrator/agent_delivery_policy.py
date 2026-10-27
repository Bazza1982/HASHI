"""Per-Agent overrides for connector delivery policy.

The persisted Agent row owns the override so it follows Agent moves and is
removed with the Agent. Admission reads the current revision for every new
Run; an operator change therefore applies without restarting a Function
Worker, while an already-frozen Run keeps its original route.
"""
from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from orchestrator.config_json import ConfigDocument, read_config_json


TELEGRAM_MIRROR_INHERIT = "inherit"
TELEGRAM_MIRROR_ALWAYS_ON = "always_on"
TELEGRAM_MIRROR_ALWAYS_OFF = "always_off"
TELEGRAM_MIRROR_POLICIES = frozenset(
    {
        TELEGRAM_MIRROR_INHERIT,
        TELEGRAM_MIRROR_ALWAYS_ON,
        TELEGRAM_MIRROR_ALWAYS_OFF,
    }
)


def normalize_telegram_mirror_policy(value: Any) -> str:
    """Return one persisted policy value or reject malformed configuration."""

    policy = str(value or TELEGRAM_MIRROR_INHERIT).strip().casefold()
    if policy not in TELEGRAM_MIRROR_POLICIES:
        raise ValueError("unsupported Agent Telegram mirror policy")
    return policy


def _agent_row(document: Mapping[str, Any], agent_id: Any) -> Mapping[str, Any]:
    requested = str(agent_id or "").strip().casefold()
    if not requested or len(requested) > 160:
        raise ValueError("Agent ID is invalid")
    rows = document.get("agents")
    if not isinstance(rows, list):
        raise ValueError("Agent configuration is invalid")
    matches = [
        row
        for row in rows
        if isinstance(row, Mapping)
        and str(row.get("name") or "").strip().casefold() == requested
    ]
    if len(matches) != 1:
        raise ValueError(
            "Agent was not found" if not matches else "Agent configuration is ambiguous"
        )
    return matches[0]


def telegram_mirror_policy_from_document(
    document: Mapping[str, Any], agent_id: Any
) -> str:
    """Resolve one Agent's explicit policy from an already-read config."""

    return normalize_telegram_mirror_policy(
        _agent_row(document, agent_id).get("telegram_mirror_policy")
    )


def load_agent_telegram_mirror_policy(
    config_path: str | Path, agent_id: Any
) -> str:
    """Read the latest Agent policy for admission-time route freezing."""

    return telegram_mirror_policy_from_document(
        read_config_json(config_path), agent_id
    )


def update_agent_telegram_mirror_policy(
    document: ConfigDocument,
    agent_id: Any,
    policy: Any,
) -> str:
    """Mutate one revision-bearing config snapshot in memory."""

    normalized = normalize_telegram_mirror_policy(policy)
    row = _agent_row(document, agent_id)
    if not isinstance(row, dict):
        raise ValueError("Agent configuration row is not mutable")
    if normalized == TELEGRAM_MIRROR_INHERIT:
        row.pop("telegram_mirror_policy", None)
    else:
        row["telegram_mirror_policy"] = normalized
    return normalized


def resolve_telegram_mirror_policy(policy: Any, *, instance_enabled: bool) -> bool:
    """Combine the Agent tri-state with the owner/instance mirror switch."""

    normalized = normalize_telegram_mirror_policy(policy)
    if normalized == TELEGRAM_MIRROR_ALWAYS_ON:
        return True
    if normalized == TELEGRAM_MIRROR_ALWAYS_OFF:
        return False
    return bool(instance_enabled)


__all__ = [
    "TELEGRAM_MIRROR_ALWAYS_OFF",
    "TELEGRAM_MIRROR_ALWAYS_ON",
    "TELEGRAM_MIRROR_INHERIT",
    "TELEGRAM_MIRROR_POLICIES",
    "load_agent_telegram_mirror_policy",
    "normalize_telegram_mirror_policy",
    "resolve_telegram_mirror_policy",
    "telegram_mirror_policy_from_document",
    "update_agent_telegram_mirror_policy",
]
