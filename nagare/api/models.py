from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


SNAPSHOT_VERSION = 1


@dataclass(frozen=True)
class RunSnapshotModel:
    run_id: str
    workflow_id: str | None
    workflow_version: str | None
    workflow_path: str | None
    status: str
    created_at: str | None
    updated_at: str | None
    current_steps: list[str]
    completed_steps: list[str]
    failed_steps: list[str]
    waiting_human_steps: list[str]
    step_status: dict[str, dict[str, Any]]
    error_count: int
    human_intervention_count: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class EventStreamModel:
    run_id: str
    events: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["count"] = len(self.events)
        return payload


@dataclass(frozen=True)
class ArtifactEntryModel:
    key: str
    path: str
    original_path: str | None
    step_id: str | None
    size_bytes: int
    registered_at: str | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ArtifactIndexModel:
    run_id: str
    artifacts: list[ArtifactEntryModel]

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "count": len(self.artifacts),
            "artifacts": [artifact.to_dict() for artifact in self.artifacts],
        }


def envelope(*, request_id: str, retrieved_at: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "snapshot_version": SNAPSHOT_VERSION,
        "request_id": request_id,
        "retrieved_at": retrieved_at,
        **payload,
    }
