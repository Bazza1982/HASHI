"""Job ownership helpers for scheduler-managed prompts."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any


_WORKSPACE_REF_RE = re.compile(
    r"(?:^|[\"'`\s:=,(])(?:[/\\][^\"'`\s]*)?workspaces[/\\]([A-Za-z0-9_.-]+)(?:[/\\]|\b)"
)


def referenced_workspace_owners(text: str) -> set[str]:
    """Return agent names referenced through workspaces/<agent>/ paths."""
    if not text:
        return set()
    return {match.group(1) for match in _WORKSPACE_REF_RE.finditer(text)}


def job_text_for_ownership(job: Mapping[str, Any]) -> str:
    """Collect user-authored job text fields that may contain workspace paths."""
    parts = []
    for key in ("prompt", "args", "note", "exit_condition"):
        value = job.get(key)
        if isinstance(value, str) and value:
            parts.append(value)
    return "\n".join(parts)


def resource_owner_mismatches(job: Mapping[str, Any]) -> list[str]:
    """Return referenced workspace owners that do not match job['agent']."""
    agent = str(job.get("agent") or "").strip()
    owners = referenced_workspace_owners(job_text_for_ownership(job))
    if not agent:
        return sorted(owners)
    return sorted(owner for owner in owners if owner != agent)


def ownership_mismatch_label(job: Mapping[str, Any]) -> str | None:
    mismatches = resource_owner_mismatches(job)
    if not mismatches:
        return None
    owners = ", ".join(f"workspaces/{owner}" for owner in mismatches)
    return f"resource owner mismatch: {owners}"
