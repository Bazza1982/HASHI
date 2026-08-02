from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, MutableMapping


IDLE_TIMEOUT_KEY = "idle_timeout_sec"
HARD_TIMEOUT_KEY = "hard_timeout_sec"
LEGACY_TIMEOUT_KEY = "process_timeout"
TIMEOUT_POLICY_META_KEY = "_hashi_timeout_policy"
USER_OVERRIDE_SOURCE = "user override"
BACKEND_CONFIG_SOURCE = "backend configuration"
AGENT_CONFIG_SOURCE = "agent configuration"
DEFAULT_SOURCE = "program default"


@dataclass(frozen=True)
class TimeoutPolicySnapshot:
    engine: str
    idle_seconds: int
    hard_seconds: int
    default_idle_seconds: int
    default_hard_seconds: int
    idle_source: str
    hard_source: str


def parse_positive_timeout(value: Any, *, label: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a positive integer number of seconds") from exc
    if parsed <= 0:
        raise ValueError(f"{label} must be a positive integer number of seconds")
    return parsed


def validate_timeout_pair(idle_seconds: Any, hard_seconds: Any) -> tuple[int, int]:
    idle = parse_positive_timeout(idle_seconds, label="idle timeout")
    hard = parse_positive_timeout(hard_seconds, label="hard timeout")
    if hard < idle:
        raise ValueError("hard timeout must be greater than or equal to idle timeout")
    return idle, hard


def _layer_timeout_value(layer: Mapping[str, Any], key: str) -> tuple[bool, Any]:
    if key in layer:
        return True, layer[key]
    if key == IDLE_TIMEOUT_KEY and LEGACY_TIMEOUT_KEY in layer:
        return True, layer[LEGACY_TIMEOUT_KEY]
    return False, None


def _configured_timeout_value(
    *,
    key: str,
    agent_extra: Mapping[str, Any],
    backend_extra: Mapping[str, Any],
) -> tuple[bool, Any, str]:
    found, value = _layer_timeout_value(backend_extra, key)
    if found:
        return True, value, BACKEND_CONFIG_SOURCE
    found, value = _layer_timeout_value(agent_extra, key)
    if found:
        return True, value, AGENT_CONFIG_SOURCE
    return False, None, DEFAULT_SOURCE


def apply_timeout_layers(
    extra: Mapping[str, Any] | None,
    *,
    engine: str,
    agent_extra: Mapping[str, Any] | None = None,
    backend_extra: Mapping[str, Any] | None = None,
    persisted_override: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Canonicalize configured timeouts and apply a workspace user override.

    Timeout priority is user override, backend configuration, agent
    configuration, then adapter class default. The original configured values
    are retained only in in-memory metadata so ``/timeout reset`` can restore
    them immediately without recreating the backend.
    """
    merged = dict(extra or {})
    agent_layer = {
        key: value
        for key, value in dict(agent_extra or {}).items()
        if key != TIMEOUT_POLICY_META_KEY
    }
    backend_layer = {
        key: value
        for key, value in dict(backend_extra or {}).items()
        if key != TIMEOUT_POLICY_META_KEY
    }
    override = dict(persisted_override or {})

    for key in (IDLE_TIMEOUT_KEY, HARD_TIMEOUT_KEY, LEGACY_TIMEOUT_KEY, TIMEOUT_POLICY_META_KEY):
        merged.pop(key, None)

    configured: dict[str, Any] = {}
    configured_sources: dict[str, str] = {}
    effective_sources: dict[str, str] = {}
    for key in (IDLE_TIMEOUT_KEY, HARD_TIMEOUT_KEY):
        found, value, source = _configured_timeout_value(
            key=key,
            agent_extra=agent_layer,
            backend_extra=backend_layer,
        )
        configured_sources[key] = source
        effective_sources[key] = source
        if found:
            configured[key] = value
            merged[key] = value

        if key in override:
            merged[key] = parse_positive_timeout(override[key], label=key.replace("_", " "))
            effective_sources[key] = USER_OVERRIDE_SOURCE

    merged[TIMEOUT_POLICY_META_KEY] = {
        "engine": str(engine),
        "configured": configured,
        "configured_sources": configured_sources,
        "sources": effective_sources,
    }
    return merged


def ensure_timeout_metadata(extra: MutableMapping[str, Any], *, engine: str) -> None:
    if isinstance(extra.get(TIMEOUT_POLICY_META_KEY), dict):
        return
    rebuilt = apply_timeout_layers(
        extra,
        engine=engine,
        agent_extra=extra,
    )
    extra.clear()
    extra.update(rebuilt)


def refresh_timeout_extra(
    extra: MutableMapping[str, Any],
    *,
    engine: str,
    persisted_override: Mapping[str, Any] | None,
) -> None:
    """Reapply an override using the configured baseline stored in metadata."""
    ensure_timeout_metadata(extra, engine=engine)
    meta = dict(extra.get(TIMEOUT_POLICY_META_KEY) or {})
    configured = dict(meta.get("configured") or {})
    configured_sources = dict(meta.get("configured_sources") or {})
    override = dict(persisted_override or {})

    for key in (IDLE_TIMEOUT_KEY, HARD_TIMEOUT_KEY, LEGACY_TIMEOUT_KEY):
        extra.pop(key, None)

    sources: dict[str, str] = {}
    for key in (IDLE_TIMEOUT_KEY, HARD_TIMEOUT_KEY):
        source = str(configured_sources.get(key) or DEFAULT_SOURCE)
        sources[key] = source
        if key in configured:
            extra[key] = configured[key]
        if key in override:
            extra[key] = parse_positive_timeout(override[key], label=key.replace("_", " "))
            sources[key] = USER_OVERRIDE_SOURCE

    meta.update(
        {
            "engine": str(engine),
            "configured": configured,
            "configured_sources": configured_sources,
            "sources": sources,
        }
    )
    extra[TIMEOUT_POLICY_META_KEY] = meta


def timeout_policy_snapshot(backend: Any) -> TimeoutPolicySnapshot:
    extra = dict(getattr(getattr(backend, "config", None), "extra", None) or {})
    meta = extra.get(TIMEOUT_POLICY_META_KEY)
    sources = dict(meta.get("sources") or {}) if isinstance(meta, dict) else {}
    explicit_idle = IDLE_TIMEOUT_KEY in extra or LEGACY_TIMEOUT_KEY in extra
    explicit_hard = HARD_TIMEOUT_KEY in extra
    return TimeoutPolicySnapshot(
        engine=str(getattr(getattr(backend, "config", None), "engine", "unknown")),
        idle_seconds=int(backend.IDLE_TIMEOUT_SEC),
        hard_seconds=int(backend.HARD_TIMEOUT_SEC),
        default_idle_seconds=int(getattr(type(backend), "DEFAULT_IDLE_TIMEOUT_SEC", 1800)),
        default_hard_seconds=int(getattr(type(backend), "DEFAULT_HARD_TIMEOUT_SEC", 36000)),
        idle_source=str(
            sources.get(IDLE_TIMEOUT_KEY)
            or (AGENT_CONFIG_SOURCE if explicit_idle else DEFAULT_SOURCE)
        ),
        hard_source=str(
            sources.get(HARD_TIMEOUT_KEY)
            or (AGENT_CONFIG_SOURCE if explicit_hard else DEFAULT_SOURCE)
        ),
    )
