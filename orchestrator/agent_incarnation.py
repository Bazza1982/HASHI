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


def project_lifecycle_identity(
    agent_name: str, raw: dict, extra: dict | None
) -> dict | None:
    """Keep the canonical persisted identity in the effective Function config.

    Nested legacy identity is accepted only when valid and consistent. Never
    mint an identity or merge unrelated residual settings while loading.
    """
    values = [raw[AGENT_LIFECYCLE_FIELD]] if AGENT_LIFECYCLE_FIELD in raw else []
    if isinstance(extra, dict) and AGENT_LIFECYCLE_FIELD in extra:
        values.append(extra[AGENT_LIFECYCLE_FIELD])
    if not values:
        return extra
    if any(not valid_agent_lifecycle_id(value) for value in values):
        raise ValueError(f"Agent {agent_name!r} has a malformed lifecycle identity")
    normalized = {str(value).strip().casefold() for value in values}
    if len(normalized) != 1:
        raise ValueError(f"Agent {agent_name!r} has a conflicting lifecycle identity")
    if extra is not None and not isinstance(extra, dict):
        raise ValueError(f"Agent {agent_name!r} extra must be an object for lifecycle identity")
    projected = dict(extra or {})
    projected[AGENT_LIFECYCLE_FIELD] = normalized.pop()
    return projected


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
        row = rows[0]
        projected = project_lifecycle_identity(wanted_name, row, row.get("extra"))
        current = (projected or {}).get(AGENT_LIFECYCLE_FIELD)
        if AGENT_LIFECYCLE_FIELD in row:
            return current
        # Promote a valid legacy nested identity without changing incarnation.
        row[AGENT_LIFECYCLE_FIELD] = current or candidate
        try:
            write_config_json(target, document)
            return current or candidate
        except ConfigConflictError:
            if attempt >= max_conflicts:
                raise
    raise AssertionError("unreachable")
