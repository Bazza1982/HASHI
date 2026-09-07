from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

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
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


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
        loaded = json.loads(legacy_path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("Failed to migrate legacy API Gateway state %s: %s", legacy_path, exc)
        return False
    if not isinstance(loaded, dict):
        logger.warning("Failed to migrate legacy API Gateway state %s: expected an object", legacy_path)
        return False

    migrated = {
        "enabled": bool(loaded.get("enabled", False)),
        "default_model": normalize_api_model(loaded.get("default_model"), global_config) or default_api_model(),
        "updated_at": str(loaded.get("updated_at") or datetime.now(timezone.utc).isoformat()),
        "updated_by": str(loaded.get("updated_by") or "legacy-state-migration"),
    }
    _write_config_atomic(path, migrated)
    logger.info(
        "Migrated legacy API Gateway state from %s to %s; legacy file retained for rollback",
        legacy_path,
        path,
    )
    return True


def load_api_gateway_config(global_config: Any) -> dict[str, Any]:
    migrate_legacy_api_gateway_state(global_config)
    path = config_path_for(global_config)
    data: dict[str, Any] = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except Exception as exc:
            logger.warning("Failed to read %s, using defaults: %s", path, exc)
            data = {}

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


def save_api_gateway_config(
    global_config: Any,
    *,
    enabled: bool | None = None,
    default_model: str | None = None,
    updated_by: str = "",
) -> dict[str, Any]:
    current = load_api_gateway_config(global_config)
    if enabled is not None:
        current["enabled"] = bool(enabled)
    if default_model is not None:
        normalized = normalize_api_model(default_model, global_config)
        if normalized is None:
            raise ValueError(f"Unknown API model: {default_model}")
        current["default_model"] = normalized
    current["updated_at"] = datetime.now(timezone.utc).isoformat()
    current["updated_by"] = updated_by

    path = config_path_for(global_config)
    _write_config_atomic(path, current)
    return current
