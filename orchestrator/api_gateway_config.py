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


def available_api_models() -> list[str]:
    return available_gateway_models()


def normalize_api_model(value: str | None) -> str | None:
    requested = str(value or "").strip()
    if not requested:
        return None
    for model in available_api_models():
        if requested == model:
            return model
    lower = requested.lower()
    for model in available_api_models():
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
        "default_model": normalize_api_model(loaded.get("default_model")) or default_api_model(),
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
    model = normalize_api_model(data.get("default_model")) or default_api_model()
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
        normalized = normalize_api_model(default_model)
        if normalized is None:
            raise ValueError(f"Unknown API model: {default_model}")
        current["default_model"] = normalized
    current["updated_at"] = datetime.now(timezone.utc).isoformat()
    current["updated_by"] = updated_by

    path = config_path_for(global_config)
    _write_config_atomic(path, current)
    return current
