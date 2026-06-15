from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from orchestrator.enterprise.routing import agent_project_ids


@dataclass(frozen=True)
class AgentCapabilitySummary:
    name: str
    display_name: str
    agent_type: str
    project_ids: tuple[str, ...]
    active_backend: str | None
    allowed_backends: tuple[str, ...]
    allowed_tools: tuple[str, ...]
    can_talk_to: tuple[str, ...]
    can_receive_from: tuple[str, ...]
    allowed_incoming_intents: tuple[str, ...]
    granted_scopes: tuple[str, ...]
    tags: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "display_name": self.display_name,
            "agent_type": self.agent_type,
            "project_ids": list(self.project_ids),
            "active_backend": self.active_backend,
            "allowed_backends": list(self.allowed_backends),
            "allowed_tools": list(self.allowed_tools),
            "bridge": {
                "can_talk_to": list(self.can_talk_to),
                "can_receive_from": list(self.can_receive_from),
                "allowed_incoming_intents": list(self.allowed_incoming_intents),
                "granted_scopes": list(self.granted_scopes),
            },
            "tags": list(self.tags),
        }


class AgentCapabilityRegistry:
    def __init__(
        self,
        *,
        agent_rows: Iterable[Mapping[str, Any]],
        capability_rows: Iterable[Mapping[str, Any]] | Mapping[str, Mapping[str, Any]] | None = None,
    ):
        self._capability_rows = _capability_map(capability_rows)
        self._summaries = {
            summary.name: summary
            for summary in (
                _build_summary(row, self._capability_rows.get(str(row.get("name") or "").strip()))
                for row in agent_rows
                if str(row.get("name") or "").strip()
            )
        }

    def list_agents(self, *, project_id: str | None = None) -> list[AgentCapabilitySummary]:
        normalized_project_id = str(project_id or "").strip()
        summaries = sorted(self._summaries.values(), key=lambda item: item.name)
        if not normalized_project_id:
            return summaries
        return [summary for summary in summaries if normalized_project_id in summary.project_ids]

    def get_agent(self, name: str) -> AgentCapabilitySummary | None:
        return self._summaries.get(str(name or "").strip())


def _build_summary(agent_row: Mapping[str, Any], capability_row: Mapping[str, Any] | None) -> AgentCapabilitySummary:
    name = str(agent_row.get("name") or "").strip()
    backends = _allowed_backends(agent_row)
    return AgentCapabilitySummary(
        name=name,
        display_name=str(agent_row.get("display_name") or name),
        agent_type=str(agent_row.get("type") or "unknown"),
        project_ids=tuple(sorted(agent_project_ids(agent_row))),
        active_backend=_optional_text(agent_row.get("active_backend") or agent_row.get("engine")),
        allowed_backends=tuple(backends),
        allowed_tools=tuple(_allowed_tools(agent_row)),
        can_talk_to=tuple(_list_text((capability_row or {}).get("can_talk_to"))),
        can_receive_from=tuple(_list_text((capability_row or {}).get("can_receive_from"))),
        allowed_incoming_intents=tuple(_list_text((capability_row or {}).get("allowed_incoming_intents"))),
        granted_scopes=tuple(_list_text((capability_row or {}).get("granted_scopes"))),
        tags=tuple(_list_text((capability_row or {}).get("tags"))),
    )


def _capability_map(
    capability_rows: Iterable[Mapping[str, Any]] | Mapping[str, Mapping[str, Any]] | None,
) -> dict[str, Mapping[str, Any]]:
    if capability_rows is None:
        return {}
    if isinstance(capability_rows, Mapping):
        return {
            str(name).strip(): row
            for name, row in capability_rows.items()
            if str(name).strip() and isinstance(row, Mapping)
        }
    return {
        str(row.get("name") or "").strip(): row
        for row in capability_rows
        if str(row.get("name") or "").strip()
    }


def _allowed_backends(agent_row: Mapping[str, Any]) -> list[str]:
    values = []
    for backend in agent_row.get("allowed_backends") or []:
        if isinstance(backend, Mapping):
            values.append(backend.get("engine"))
        else:
            values.append(backend)
    return _list_text(values)


def _allowed_tools(agent_row: Mapping[str, Any]) -> list[str]:
    values = []
    for backend in agent_row.get("allowed_backends") or []:
        if not isinstance(backend, Mapping):
            continue
        tools = backend.get("tools") or {}
        if isinstance(tools, Mapping):
            values.extend(tools.get("allowed") or [])
        values.extend(backend.get("allowed_tools") or [])
    values.extend(agent_row.get("allowed_tools") or [])
    return sorted(set(_list_text(values)))


def _list_text(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        values = [value]
    else:
        values = list(value)
    return [str(item).strip() for item in values if item is not None and str(item).strip()]


def _optional_text(value: Any) -> str | None:
    normalized = str(value or "").strip()
    return normalized or None
