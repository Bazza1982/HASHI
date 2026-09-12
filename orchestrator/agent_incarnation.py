"""Stable per-incarnation Agent identities owned by PAO Functions.

An Agent name is reusable. This identifier is persisted beside the Agent
configuration so stateful Function services can distinguish a moved incarnation
from a deleted-and-recreated Agent with the same display name.
"""
from __future__ import annotations

import re
from pathlib import Path
from uuid import uuid4

from orchestrator.config_json import (
    ConfigConflictError,
    read_config_json,
    write_config_json,
)

AGENT_LIFECYCLE_FIELD = "agent_lifecycle_id"
_LIFECYCLE_RE = re.compile(r"^[0-9a-f]{32}$")


def new_agent_lifecycle_id() -> str:
    return uuid4().hex


def valid_agent_lifecycle_id(value: object) -> bool:
    return bool(_LIFECYCLE_RE.fullmatch(str(value or "").strip().casefold()))


def lifecycle_id_from_config(config: object) -> str | None:
    extra = getattr(config, "extra", None)
    value = extra.get(AGENT_LIFECYCLE_FIELD) if isinstance(extra, dict) else None
    normalized = str(value or "").strip().casefold()
    return normalized if valid_agent_lifecycle_id(normalized) else None


def ensure_agent_lifecycle_id(
    config_path: str | Path,
    agent_name: str,
    *,
    max_conflicts: int = 4,
) -> str:
    """Return the stable ID, assigning one with a revision-safe publication.

    Missing IDs are upgraded lazily. A malformed existing value is rejected
    rather than silently changing ownership of persisted state.
    """

    target = Path(config_path).expanduser().resolve()
    wanted_name = str(agent_name or "").strip()
    if not wanted_name:
        raise ValueError("Agent name is required for lifecycle identity")
    candidate = new_agent_lifecycle_id()
    for attempt in range(max_conflicts + 1):
        document = read_config_json(target)
        rows = [
            row
            for row in document.get("agents", [])
            if isinstance(row, dict) and str(row.get("name") or "") == wanted_name
        ]
        if len(rows) != 1:
            raise ValueError(
                f"Agent {wanted_name!r} must have exactly one configuration row"
            )
        current = rows[0].get(AGENT_LIFECYCLE_FIELD)
        if current is not None:
            if not valid_agent_lifecycle_id(current):
                raise ValueError(
                    f"Agent {wanted_name!r} has a malformed lifecycle identity"
                )
            return str(current).strip().casefold()
        rows[0][AGENT_LIFECYCLE_FIELD] = candidate
        try:
            write_config_json(target, document)
            return candidate
        except ConfigConflictError:
            if attempt >= max_conflicts:
                raise
    raise AssertionError("unreachable")
