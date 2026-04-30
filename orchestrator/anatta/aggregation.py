from __future__ import annotations

from datetime import datetime, timezone

from .config import AnattaConfig
from .models import DriveContribution, EmergentTurnState


class DriveAggregator:
    def __init__(self, config: AnattaConfig):
        self.config = config

    def aggregate(
        self,
        contributions: list[DriveContribution],
        contributing_annotation_ids: list[int] | None = None,
        relationship_key: str | None = None,
    ) -> EmergentTurnState:
        registry = self.config.drive_registry()
        active = [name for name, cfg in registry.items() if cfg.get("enabled", True)]
        raw: dict[str, float] = {name: 0.0 for name in active}
        rationales: list[str] = []
        for contribution in contributions:
            rationales.append(f"{contribution.source}: {contribution.rationale}")
            for drive_name, delta in contribution.drive_delta.items():
                if drive_name not in raw:
                    continue
                raw[drive_name] += float(delta) * float(contribution.weight)
        drive_values: dict[str, float] = {}
        for name, value in raw.items():
            cfg = registry[name]
            lo = float(cfg.get("min", 0.0))
            hi = float(cfg.get("max", 100.0))
            scaled = value * 100.0
            drive_values[name] = max(lo, min(hi, round(scaled, 3)))
        ranked = sorted(drive_values.items(), key=lambda item: item[1], reverse=True)
        dominant = [name for name, value in ranked if value > 0][:3]
        return EmergentTurnState(
            drive_values=drive_values,
            dominant_drives=dominant,
            contributions=contributions,
            contributing_annotation_ids=list(contributing_annotation_ids or []),
            rationale=rationales,
            relationship_key=relationship_key,
            generated_at=datetime.now(timezone.utc),
        )
