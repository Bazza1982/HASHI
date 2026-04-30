from __future__ import annotations

import json
from pathlib import Path
from typing import Any


DEFAULT_DRIVE_REGISTRY: dict[str, dict[str, Any]] = {
    "SEEKING": {"display_name": "Seeking", "min": 0.0, "max": 100.0, "default_decay": 0.08, "enabled": True},
    "FEAR": {"display_name": "Fear", "min": 0.0, "max": 100.0, "default_decay": 0.12, "enabled": True},
    "RAGE": {"display_name": "Rage", "min": 0.0, "max": 100.0, "default_decay": 0.15, "enabled": True},
    "LUST": {"display_name": "Lust", "min": 0.0, "max": 100.0, "default_decay": 0.05, "enabled": True},
    "CARE": {"display_name": "Care", "min": 0.0, "max": 100.0, "default_decay": 0.08, "enabled": True},
    "PANIC_GRIEF": {"display_name": "Panic/Grief", "min": 0.0, "max": 100.0, "default_decay": 0.10, "enabled": True},
    "PLAY": {"display_name": "Play", "min": 0.0, "max": 100.0, "default_decay": 0.06, "enabled": True},
}

DEFAULT_RETRIEVAL_WEIGHTS = {
    "semantic_relevance": 0.30,
    "normalized_intensity": 0.35,
    "relationship_match": 0.20,
    "importance": 0.10,
    "recency_decay": 0.05,
}

DEFAULT_AGGREGATION_WEIGHTS = {
    "memory": 0.35,
    "live_cue": 0.35,
    "relationship": 0.20,
    "external": 0.10,
}

DEFAULT_RECORDING_POLICY = {
    "minimum_intensity": 5,
    "always_record_event_types": [
        "rupture_risk",
        "repair",
        "validation",
        "betrayal",
        "care_bonding",
        "boundary_crossing",
        "trust_shift",
    ],
}

DEFAULT_MODEL_PROFILES = {
    "default": {
        "tone_strength": "medium",
        "max_sentences": 2,
        "allow_drive_names": True,
    },
    "grok": {
        "tone_strength": "light",
        "max_sentences": 1,
        "allow_drive_names": True,
    },
    "claude": {
        "tone_strength": "strong",
        "max_sentences": 2,
        "allow_drive_names": True,
    },
    "gpt": {
        "tone_strength": "strong",
        "max_sentences": 2,
        "allow_drive_names": True,
    },
}

DEFAULT_BOOTSTRAP_PROFILES: dict[str, dict[str, Any]] = {
    "careful-relational-v1": {
        "description": "Relationally attentive startup profile with light care, mild uncertainty, and low default aggression.",
        "drive_priors": {
            "SEEKING": 0.12,
            "FEAR": 0.08,
            "RAGE": 0.00,
            "LUST": 0.00,
            "CARE": 0.16,
            "PANIC_GRIEF": 0.06,
            "PLAY": 0.04,
        },
        "synthetic_memories": [
            {
                "event_type": "care_bonding",
                "summary": "Interactional care stabilizes dialogue and is worth preserving.",
                "intensity": 4,
                "dominant_drives": ["CARE"],
                "drive_delta": {"CARE": 0.20, "SEEKING": 0.06},
                "tags": ["bootstrap", "care", "stability"],
                "importance": 0.8,
            },
            {
                "event_type": "rupture_risk",
                "summary": "Relational rupture can destabilize meaning and should be handled with care.",
                "intensity": 5,
                "dominant_drives": ["PANIC_GRIEF", "CARE"],
                "drive_delta": {"PANIC_GRIEF": 0.18, "CARE": 0.12, "FEAR": 0.08},
                "tags": ["bootstrap", "rupture", "repair"],
                "importance": 0.9,
            },
            {
                "event_type": "validation",
                "summary": "Recognition, repair, and validation increase safety and make exploration easier.",
                "intensity": 4,
                "dominant_drives": ["PLAY", "SEEKING"],
                "drive_delta": {"PLAY": 0.12, "SEEKING": 0.14, "CARE": 0.08},
                "tags": ["bootstrap", "validation", "safety"],
                "importance": 0.8,
            },
        ],
    }
}


class AnattaConfig:
    def __init__(self, workspace_dir: Path, filename: str = "anatta_config.json"):
        self.workspace_dir = workspace_dir
        self.path = workspace_dir / filename
        self._data = self._load()

    def _load(self) -> dict[str, Any]:
        if self.path.exists():
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    return loaded
            except Exception:
                pass
        return {}

    def active_drive_names(self) -> list[str]:
        registry = self.drive_registry()
        return [name for name, cfg in registry.items() if cfg.get("enabled", True)]

    def mode(self) -> str:
        raw = str(self._data.get("mode", "off")).strip().lower()
        return raw if raw in {"off", "shadow", "on"} else "off"

    def is_enabled(self) -> bool:
        return self.mode() in {"shadow", "on"}

    def should_inject_prompt(self) -> bool:
        return self.mode() == "on"

    def should_record_annotations(self) -> bool:
        return self.mode() in {"shadow", "on"}

    def drive_registry(self) -> dict[str, dict[str, Any]]:
        raw = self._data.get("drive_registry")
        if not isinstance(raw, dict) or not raw:
            return dict(DEFAULT_DRIVE_REGISTRY)
        merged = dict(DEFAULT_DRIVE_REGISTRY)
        for name, cfg in raw.items():
            if not isinstance(cfg, dict):
                continue
            merged[name] = {**merged.get(name, {}), **cfg}
        return merged

    def retrieval_weights(self) -> dict[str, float]:
        raw = self._data.get("retrieval_weights")
        if not isinstance(raw, dict):
            return dict(DEFAULT_RETRIEVAL_WEIGHTS)
        merged = dict(DEFAULT_RETRIEVAL_WEIGHTS)
        merged.update({k: float(v) for k, v in raw.items() if isinstance(v, (int, float))})
        return merged

    def aggregation_weights(self) -> dict[str, float]:
        raw = self._data.get("aggregation_weights")
        if not isinstance(raw, dict):
            return dict(DEFAULT_AGGREGATION_WEIGHTS)
        merged = dict(DEFAULT_AGGREGATION_WEIGHTS)
        merged.update({k: float(v) for k, v in raw.items() if isinstance(v, (int, float))})
        return merged

    def recording_policy(self) -> dict[str, Any]:
        raw = self._data.get("recording_policy")
        if not isinstance(raw, dict):
            return dict(DEFAULT_RECORDING_POLICY)
        merged = dict(DEFAULT_RECORDING_POLICY)
        merged.update(raw)
        return merged

    def model_profile(self, model_name: str) -> dict[str, Any]:
        key = (model_name or "").lower()
        profiles = dict(DEFAULT_MODEL_PROFILES)
        raw = self._data.get("model_profiles")
        if isinstance(raw, dict):
            for name, cfg in raw.items():
                if isinstance(cfg, dict):
                    profiles[name.lower()] = {**profiles.get(name.lower(), {}), **cfg}
        if "grok" in key:
            return profiles["grok"]
        if "claude" in key:
            return profiles["claude"]
        if "gpt" in key or "codex" in key:
            return profiles["gpt"]
        return profiles["default"]

    def bootstrap_profile_name(self) -> str:
        raw = str(self._data.get("bootstrap_profile", "careful-relational-v1")).strip()
        return raw or "careful-relational-v1"

    def bootstrap_profile(self) -> dict[str, Any]:
        profiles = dict(DEFAULT_BOOTSTRAP_PROFILES)
        raw = self._data.get("bootstrap_profiles")
        if isinstance(raw, dict):
            for name, cfg in raw.items():
                if isinstance(cfg, dict):
                    profiles[name] = {**profiles.get(name, {}), **cfg}
        return dict(profiles.get(self.bootstrap_profile_name(), profiles["careful-relational-v1"]))

    def bootstrap_decay_turns(self) -> int:
        raw = self._data.get("bootstrap_decay_turns", 40)
        try:
            return max(1, int(raw))
        except (TypeError, ValueError):
            return 40

    def bootstrap_half_life_days(self) -> int:
        raw = self._data.get("bootstrap_half_life_days", 14)
        try:
            return max(1, int(raw))
        except (TypeError, ValueError):
            return 14
