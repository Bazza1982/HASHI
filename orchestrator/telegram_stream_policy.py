from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4


DEFAULT_STREAM_ENABLED = False
DEFAULT_COMPONENTS = {
    "placeholder": True,
    "typing": True,
    "progress": True,
    "preview": True,
    "promote": True,
}
DEFAULT_EDIT_INTERVAL_S = 10.0
DEFAULT_HEARTBEAT_INTERVAL_S = 60.0
DEFAULT_MAX_EDITS_PER_REQUEST = 20
COMPONENT_NAMES = frozenset(DEFAULT_COMPONENTS)


@dataclass(frozen=True)
class TelegramStreamPolicy:
    enabled: bool
    placeholder: bool
    typing: bool
    progress: bool
    preview: bool
    promote: bool
    edit_interval_s: float
    heartbeat_interval_s: float
    max_edits_per_request: int
    source: str
    component_sources: dict[str, str]

    @property
    def placeholder_enabled(self) -> bool:
        return self.enabled and self.placeholder

    @property
    def typing_enabled(self) -> bool:
        return self.enabled and self.typing

    @property
    def progress_enabled(self) -> bool:
        return self.enabled and self.progress and self.placeholder_enabled

    @property
    def preview_enabled(self) -> bool:
        return self.enabled and self.preview and self.placeholder_enabled

    @property
    def promote_enabled(self) -> bool:
        return self.enabled and self.promote and self.preview_enabled

    @property
    def final_only(self) -> bool:
        return not self.enabled


def preferences_path(runtime: Any) -> Path:
    return Path(runtime.workspace_dir) / "state" / "runtime_preferences.json"


def load_preferences(runtime: Any) -> dict[str, Any]:
    path = preferences_path(runtime)
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _stream_config(runtime: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    extra = getattr(getattr(runtime, "config", None), "extra", {}) or {}
    raw = extra.get("telegram_stream")
    if isinstance(raw, dict):
        stream = dict(raw)
    elif isinstance(raw, bool):
        stream = {"enabled": raw}
    else:
        stream = {}
    if "enabled" not in stream and isinstance(extra.get("telegram_stream_enabled"), bool):
        stream["enabled"] = extra["telegram_stream_enabled"]
    return stream, extra


def _resolve_bool(
    persisted: Any,
    configured: Any,
    default: bool,
) -> tuple[bool, str]:
    if isinstance(persisted, bool):
        return persisted, "persisted override"
    if isinstance(configured, bool):
        return configured, "config default"
    return default, "functional default"


def _bounded_float(value: Any, default: float, *, minimum: float, maximum: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def _bounded_int(value: Any, default: int, *, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def get_policy(runtime: Any) -> TelegramStreamPolicy:
    payload = load_preferences(runtime)
    persisted = payload.get("telegram_stream")
    if not isinstance(persisted, dict):
        persisted = {}
    configured, extra = _stream_config(runtime)

    enabled, source = _resolve_bool(
        persisted.get("enabled"),
        configured.get("enabled"),
        DEFAULT_STREAM_ENABLED,
    )

    values: dict[str, bool] = {}
    sources: dict[str, str] = {}
    for name, default in DEFAULT_COMPONENTS.items():
        persisted_value = persisted.get(name)
        configured_value = configured.get(name)
        if name == "preview":
            if not isinstance(persisted_value, bool):
                persisted_value = payload.get("answer_stream_preview")
            if not isinstance(configured_value, bool):
                configured_value = extra.get("answer_stream_preview")
        elif name == "promote" and not isinstance(configured_value, bool):
            configured_value = extra.get("answer_stream_final_delivery")
        values[name], sources[name] = _resolve_bool(
            persisted_value,
            configured_value,
            default,
        )

    edit_interval_s = _bounded_float(
        persisted.get(
            "edit_interval_s",
            configured.get("edit_interval_s", extra.get("answer_stream_edit_interval_s")),
        ),
        DEFAULT_EDIT_INTERVAL_S,
        minimum=0.01,
        maximum=300.0,
    )
    heartbeat_interval_s = _bounded_float(
        persisted.get(
            "heartbeat_interval_s",
            configured.get("heartbeat_interval_s", extra.get("answer_stream_heartbeat_interval_s")),
        ),
        DEFAULT_HEARTBEAT_INTERVAL_S,
        minimum=1.0,
        maximum=3600.0,
    )
    max_edits_per_request = _bounded_int(
        persisted.get(
            "max_edits_per_request",
            configured.get("max_edits_per_request", extra.get("answer_stream_max_edits")),
        ),
        DEFAULT_MAX_EDITS_PER_REQUEST,
        minimum=0,
        maximum=1000,
    )

    return TelegramStreamPolicy(
        enabled=enabled,
        placeholder=values["placeholder"],
        typing=values["typing"],
        progress=values["progress"],
        preview=values["preview"],
        promote=values["promote"],
        edit_interval_s=edit_interval_s,
        heartbeat_interval_s=heartbeat_interval_s,
        max_edits_per_request=max_edits_per_request,
        source=source,
        component_sources=sources,
    )


def _write_preferences(runtime: Any, payload: dict[str, Any]) -> Path:
    path = preferences_path(runtime)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        current_version = int(payload.get("version") or 0)
    except (TypeError, ValueError):
        current_version = 0
    payload["version"] = max(2, current_version)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    return path


def set_policy_value(runtime: Any, name: str, enabled: bool) -> Path:
    if name != "enabled" and name not in COMPONENT_NAMES:
        raise ValueError(f"Unknown Telegram stream switch: {name}")
    payload = load_preferences(runtime)
    stream = payload.get("telegram_stream")
    if not isinstance(stream, dict):
        stream = {}
    stream[name] = bool(enabled)
    payload["telegram_stream"] = stream
    return _write_preferences(runtime, payload)


def reset_policy(runtime: Any) -> Path:
    payload = load_preferences(runtime)
    payload.pop("telegram_stream", None)
    payload.pop("answer_stream_preview", None)
    return _write_preferences(runtime, payload)


def component_status(runtime: Any, name: str) -> tuple[bool, bool, str]:
    if name not in COMPONENT_NAMES:
        raise ValueError(f"Unknown Telegram stream switch: {name}")
    policy = get_policy(runtime)
    configured = bool(getattr(policy, name))
    effective = bool(getattr(policy, f"{name}_enabled"))
    return configured, effective, policy.component_sources[name]
