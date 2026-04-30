from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping


DriveMap = Mapping[str, float]


@dataclass(frozen=True)
class DriveContribution:
    source: str
    drive_delta: dict[str, float]
    weight: float
    rationale: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class EmotionalAnnotation:
    annotation_id: int | None
    bridge_row_type: str
    bridge_row_id: int
    event_ts: datetime
    created_at: datetime
    source: str
    actor_role: str
    event_type: str
    summary: str
    intensity: int
    dominant_drives: list[str]
    contributions: list[DriveContribution]
    relationship_key: str | None
    tags: list[str]
    metadata: dict[str, Any]
    importance: float = 1.0


@dataclass
class TurnContext:
    user_text: str
    source: str
    request_id: str | None
    relationship_key: str | None
    bridge_recent_turns: list[dict[str, Any]] = field(default_factory=list)
    bridge_memories: list[dict[str, Any]] = field(default_factory=list)
    resolved_social_context: dict[str, Any] = field(default_factory=dict)
    external_factors: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class EmergentTurnState:
    drive_values: dict[str, float]
    dominant_drives: list[str]
    contributions: list[DriveContribution]
    contributing_annotation_ids: list[int]
    rationale: list[str]
    relationship_key: str | None
    generated_at: datetime


@dataclass
class PromptInjection:
    title: str
    body: str
    metadata: dict[str, Any] = field(default_factory=dict)
