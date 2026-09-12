from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from orchestrator.config_json import (
    ConfigConflictError,
    ConfigDocument,
    read_config_json,
    write_config_json,
)
from orchestrator.model_catalog import available_gateway_models, default_gateway_model


API_GATEWAY_CONFIG_NAME = "api_gateway_config.json"
LEGACY_API_GATEWAY_STATE_NAME = "api_gateway_state.json"
logger = logging.getLogger("BridgeU.ApiGatewayConfig")


def configured_gateway_model_overrides(
    agent_configs,
) -> tuple[dict[str, str], dict[str, tuple[str, ...]]]:
    """Return instance-authorised Gateway models without widening Core catalogues."""

    from orchestrator.flexible_backend_registry import (
        canonical_backend_engine,
        get_backend_entry,
    )
    from orchestrator.runtime_effort_options import configured_model_efforts

    model_engines: dict[str, str] = {}
    model_efforts: dict[str, tuple[str, ...]] = {}
    for agent in agent_configs or ():
        backends = (
            agent.get("allowed_backends", ())
            if isinstance(agent, dict)
            else getattr(agent, "allowed_backends", ())
        )
        for raw_backend in backends or ():
            backend = {"engine": raw_backend} if isinstance(raw_backend, str) else dict(raw_backend)
            engine = canonical_backend_engine(backend.get("engine"))
            if not engine or not get_backend_entry(engine).get("gateway_enabled"):
                continue

            candidates: list[str] = []
            for key in ("model", "default_model", "fast_model", "pro_model"):
                value = str(backend.get(key) or "").strip()
                if value and value not in candidates:
                    candidates.append(value)
            raw_models = backend.get("models") or ()
            if isinstance(raw_models, str):
                raw_models = (raw_models,)
            for raw_model in raw_models:
                value = str(raw_model or "").strip()
                if value and value not in candidates:
                    candidates.append(value)

            for model in candidates:
                existing = model_engines.get(model)
                if existing is not None and existing != engine:
                    raise ValueError(
                        f"configured API gateway model '{model}' maps to both "
                        f"'{existing}' and '{engine}'"
                    )
                model_engines[model] = engine

            configured_efforts = backend.get("model_efforts") or {}
            if not isinstance(configured_efforts, dict):
                continue
            for raw_model in configured_efforts:
                model = str(raw_model or "").strip()
                if model_engines.get(model) != engine:
                    continue
                efforts = tuple(configured_model_efforts(backend, raw_model) or ())
                existing = model_efforts.get(model)
                if existing is not None and set(existing) != set(efforts):
                    raise ValueError(f"conflicting model_efforts for '{model}'")
                model_efforts[model] = efforts
    return model_engines, model_efforts


def instance_gateway_model_overrides(
    global_config: Any,
) -> tuple[dict[str, str], dict[str, tuple[str, ...]]]:
    """Read opt-ins from live configuration without loading PCM or runtime state."""
    if global_config is None:
        return {}, {}
    config_path = getattr(global_config, "config_path", None)
    path = Path(config_path) if config_path else _bridge_home_for(global_config) / "agents.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return {}, {}
    if not isinstance(data, dict) or not isinstance(data.get("agents", []), list):
        raise ValueError("Invalid instance Agent configuration")
    agents = data.get("agents", [])
    if any(not isinstance(agent, dict) for agent in agents):
        raise ValueError("Invalid instance Agent entry")
    return configured_gateway_model_overrides(
        agent for agent in agents if agent.get("is_active", True)
    )


def available_api_models(global_config: Any = None) -> list[str]:
    models = available_gateway_models()
    configured, _efforts = instance_gateway_model_overrides(global_config)
    return list(dict.fromkeys([*models, *configured]))


def normalize_api_model(value: str | None, global_config: Any = None) -> str | None:
    requested = str(value or "").strip()
    if not requested:
        return None
    models = available_api_models(global_config)
    for model in models:
        if requested == model:
            return model
    lower = requested.lower()
    for model in models:
        if lower == model.lower():
            return model
    return None


def default_api_model() -> str:
    configured_default = default_gateway_model()
    models = available_api_models()
    return configured_default if configured_default in models else (models[0] if models else "")


def _bridge_home_for(global_config: Any) -> Path:
    bridge_home = Path(getattr(global_config, "bridge_home", "") or getattr(global_config, "project_root", "."))
    return bridge_home


def config_path_for(global_config: Any) -> Path:
    return _bridge_home_for(global_config) / "state" / API_GATEWAY_CONFIG_NAME


def legacy_state_path_for(global_config: Any) -> Path:
    return _bridge_home_for(global_config) / LEGACY_API_GATEWAY_STATE_NAME


def _write_config_atomic(path: Path, data: dict[str, Any]) -> None:
    """Publish a read revision, or require absence when seeding a new config."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, ConfigDocument):
        write_config_json(path, data)
    else:
        write_config_json(path, data, expected_revision=None)


def _validate_gateway_config_for_update(data: dict[str, Any]) -> None:
    if "enabled" in data and not isinstance(data["enabled"], bool):
        raise ValueError("API Gateway enabled must be a boolean")
    if "default_model" in data and not isinstance(data["default_model"], str):
        raise ValueError("API Gateway default_model must be a string")


def _gateway_config_view(data: dict[str, Any], global_config: Any) -> dict[str, Any]:
    """Effective public values; never use this projection as a writable snapshot."""
    enabled = data.get("enabled")
    if not isinstance(enabled, bool):
        enabled = False
    model = normalize_api_model(data.get("default_model"), global_config) or default_api_model()
    return {
        "enabled": enabled,
        "default_model": model,
        "updated_at": str(data.get("updated_at") or ""),
        "updated_by": str(data.get("updated_by") or ""),
    }


def migrate_legacy_api_gateway_state(global_config: Any) -> bool:
    """Seed the canonical config from the legacy root-level state once.

    The legacy file is intentionally retained as a rollback artifact, but it
    stops being a runtime source after the canonical config exists.
    """
    path = config_path_for(global_config)
    legacy_path = legacy_state_path_for(global_config)
    if path.exists() or not legacy_path.exists():
        return False

    try:
        loaded = read_config_json(legacy_path)
        _validate_gateway_config_for_update(loaded)
    except (OSError, ValueError) as exc:
        logger.warning("Failed to migrate legacy API Gateway state %s: %s", legacy_path, exc)
        return False

    # This is a new destination, not a read/modify/write of the legacy file.
    migrated = dict(loaded)
    migrated.update(_gateway_config_view(loaded, global_config))
    migrated["updated_at"] = migrated["updated_at"] or datetime.now(timezone.utc).isoformat()
    migrated["updated_by"] = migrated["updated_by"] or "legacy-state-migration"
    try:
        _write_config_atomic(path, migrated)
    except ConfigConflictError:
        # Another initializer/save created the canonical source. Do not replay
        # migration; the caller will read that source instead of the legacy one.
        return False
    logger.info(
        "Migrated legacy API Gateway state from %s to %s; legacy file retained for rollback",
        legacy_path,
        path,
    )
    return True


def load_api_gateway_config(global_config: Any) -> dict[str, Any]:
    migrate_legacy_api_gateway_state(global_config)
    path = config_path_for(global_config)
    try:
        data = read_config_json(path)
    except FileNotFoundError:
        data = {}
    except (OSError, ValueError) as exc:
        logger.warning("Failed to read %s, using defaults: %s", path, exc)
        data = {}
    return _gateway_config_view(data, global_config)


def save_api_gateway_config(
    global_config: Any,
    *,
    enabled: bool | None = None,
    default_model: str | None = None,
    updated_by: str = "",
) -> dict[str, Any]:
    # Validate the requested model before any migration/publication. In
    # particular, a rejected update must not first create canonical defaults.
    normalized = None
    if default_model is not None:
        normalized = normalize_api_model(default_model, global_config)
        if normalized is None:
            raise ValueError(f"Unknown API model: {default_model}")

    path = config_path_for(global_config)
    try:
        current = read_config_json(path)
    except FileNotFoundError:
        # Seed from legacy only when canonical is absent. Retain the legacy
        # bytes; the requested update and first creation publish together.
        try:
            current = dict(read_config_json(legacy_state_path_for(global_config)))
        except FileNotFoundError:
            current = {}
    _validate_gateway_config_for_update(current)
    current.setdefault("enabled", False)
    current.setdefault("default_model", default_api_model())
    if enabled is not None:
        current["enabled"] = bool(enabled)
    if normalized is not None:
        current["default_model"] = normalized
    current["updated_at"] = datetime.now(timezone.utc).isoformat()
    current["updated_by"] = updated_by

    # Resolve the public return view before publication as this may itself
    # reject invalid instance model configuration. Preserve unowned raw fields.
    result = _gateway_config_view(current, global_config)
    _write_config_atomic(path, current)
    return result
