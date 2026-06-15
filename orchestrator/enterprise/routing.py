from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


def agent_project_ids(agent_row: Mapping[str, Any] | None) -> set[str]:
    """Return normalized project ids assigned to an agent row."""
    if not agent_row:
        return set()

    values: list[Any] = []
    if agent_row.get("project_id"):
        values.append(agent_row.get("project_id"))

    project_ids = agent_row.get("project_ids") or []
    if isinstance(project_ids, str):
        values.append(project_ids)
    else:
        values.extend(project_ids)

    return {str(value).strip() for value in values if value is not None and str(value).strip()}


@dataclass(frozen=True)
class ProjectRouteDecision:
    allowed: bool
    reason: str


def evaluate_project_route(
    *,
    project_id: str | None,
    from_agent: str,
    to_agent: str,
    sender_row: Mapping[str, Any] | None,
    target_row: Mapping[str, Any] | None,
) -> ProjectRouteDecision:
    """Fail closed when an explicit project route targets agents outside that project."""
    normalized_project_id = str(project_id or "").strip()
    if not normalized_project_id:
        return ProjectRouteDecision(True, "no project route requested")

    sender_projects = agent_project_ids(sender_row)
    if normalized_project_id not in sender_projects:
        return ProjectRouteDecision(
            False,
            f"sender {from_agent} is not available in project {normalized_project_id}",
        )

    target_projects = agent_project_ids(target_row)
    if normalized_project_id not in target_projects:
        return ProjectRouteDecision(
            False,
            f"target {to_agent} is not available in project {normalized_project_id}",
        )

    return ProjectRouteDecision(True, f"project route allowed: {normalized_project_id}")
