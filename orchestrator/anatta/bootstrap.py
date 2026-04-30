from __future__ import annotations

from datetime import datetime, timezone

from .config import AnattaConfig
from .models import DriveContribution, EmotionalAnnotation


class BootstrapManager:
    def __init__(self, config: AnattaConfig):
        self.config = config

    def drive_prior_contributions(self) -> list[DriveContribution]:
        profile = self.config.bootstrap_profile()
        priors = dict(profile.get("drive_priors") or {})
        if not priors:
            return []
        return [
            DriveContribution(
                source="bootstrap_drive_priors",
                drive_delta={str(k): float(v) for k, v in priors.items()},
                weight=1.0,
                rationale=f"Bootstrap profile {self.config.bootstrap_profile_name()} provides initial low-amplitude drive priors.",
                metadata={
                    "synthetic_seed": True,
                    "bootstrap_profile": self.config.bootstrap_profile_name(),
                    "bootstrap_decay_turns": self.config.bootstrap_decay_turns(),
                    "bootstrap_half_life_days": self.config.bootstrap_half_life_days(),
                },
            )
        ]

    def build_seed_annotations(self) -> list[EmotionalAnnotation]:
        profile = self.config.bootstrap_profile()
        synthetic = list(profile.get("synthetic_memories") or [])
        created_at = datetime.now(timezone.utc)
        annotations: list[EmotionalAnnotation] = []
        for item in synthetic:
            contribution = DriveContribution(
                source="bootstrap_synthetic_memory",
                drive_delta={str(k): float(v) for k, v in dict(item.get("drive_delta") or {}).items()},
                weight=1.0,
                rationale=f"Synthetic formative memory from bootstrap profile {self.config.bootstrap_profile_name()}.",
                metadata={
                    "synthetic_seed": True,
                    "bootstrap_profile": self.config.bootstrap_profile_name(),
                    "bootstrap_decay_turns": self.config.bootstrap_decay_turns(),
                    "bootstrap_half_life_days": self.config.bootstrap_half_life_days(),
                },
            )
            annotations.append(
                EmotionalAnnotation(
                    annotation_id=None,
                    bridge_row_type="bootstrap",
                    bridge_row_id=0,
                    event_ts=created_at,
                    created_at=created_at,
                    source="bootstrap",
                    actor_role="system",
                    event_type=str(item.get("event_type", "bootstrap_seed")),
                    summary=str(item.get("summary", "Bootstrap synthetic formative memory.")),
                    intensity=int(item.get("intensity", 4)),
                    dominant_drives=[str(x) for x in item.get("dominant_drives", [])],
                    contributions=[contribution],
                    relationship_key=None,
                    tags=[str(x) for x in item.get("tags", [])],
                    metadata={
                        "synthetic_seed": True,
                        "bootstrap_profile": self.config.bootstrap_profile_name(),
                        "bootstrap_decay_turns": self.config.bootstrap_decay_turns(),
                        "bootstrap_half_life_days": self.config.bootstrap_half_life_days(),
                    },
                    importance=float(item.get("importance", 0.8)),
                )
            )
        return annotations
