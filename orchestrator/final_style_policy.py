"""Agent-scoped policy for the optional HERV3 final presentation check."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from orchestrator.config_json import (
    new_config_json,
    read_config_json,
    write_config_json,
)


FINAL_STYLE_POLICY_VERSION = 1
DEFAULT_FINAL_STYLE_ENABLED = False


@dataclass(frozen=True)
class FinalStylePolicy:
    enabled: bool
    source: str


def preferences_path(runtime: Any) -> Path:
    workspace = getattr(runtime, "workspace_dir", None)
    if workspace is None:
        workspace = getattr(getattr(runtime, "config", None), "workspace_dir", ".")
    return Path(workspace) / "state" / "final_style.json"


def _configured_enabled(runtime: Any) -> bool | None:
    extra = getattr(getattr(runtime, "config", None), "extra", {}) or {}
    if not isinstance(extra, Mapping):
        return None
    raw = extra.get("final_style")
    if isinstance(raw, Mapping) and isinstance(raw.get("enabled"), bool):
        return bool(raw["enabled"])
    if isinstance(raw, bool):
        return raw
    legacy = extra.get("final_style_enabled")
    return bool(legacy) if isinstance(legacy, bool) else None


def get_policy(
    runtime: Any,
    *,
    default: bool = DEFAULT_FINAL_STYLE_ENABLED,
    strict: bool = False,
) -> FinalStylePolicy:
    """Read the effective policy without creating or rewriting state."""

    path = preferences_path(runtime)
    if path.exists():
        try:
            payload = read_config_json(path)
        except Exception:
            if strict:
                raise
        else:
            if isinstance(payload.get("enabled"), bool):
                return FinalStylePolicy(
                    enabled=bool(payload["enabled"]),
                    source="persisted override",
                )
    configured = _configured_enabled(runtime)
    if configured is not None:
        return FinalStylePolicy(enabled=configured, source="config default")
    return FinalStylePolicy(enabled=bool(default), source="functional default")


def get_enabled(
    runtime: Any,
    *,
    default: bool = DEFAULT_FINAL_STYLE_ENABLED,
) -> bool:
    return get_policy(runtime, default=default).enabled


def set_enabled(runtime: Any, enabled: bool) -> Path:
    """Persist one revision-checked workspace override."""

    path = preferences_path(runtime)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        payload = read_config_json(path)
        payload.clear()
        payload.update(
            {"version": FINAL_STYLE_POLICY_VERSION, "enabled": bool(enabled)}
        )
    else:
        payload = new_config_json(
            path,
            {"version": FINAL_STYLE_POLICY_VERSION, "enabled": bool(enabled)},
        )
    write_config_json(path, payload)
    return path
