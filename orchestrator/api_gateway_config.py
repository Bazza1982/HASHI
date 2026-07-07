from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from orchestrator.model_catalog import (
    AVAILABLE_CLAUDE_MODELS,
    AVAILABLE_CODEX_MODELS,
    AVAILABLE_GEMINI_MODELS,
    AVAILABLE_XAI_API_MODELS,
)


API_GATEWAY_CONFIG_NAME = "api_gateway_config.json"
DEFAULT_API_MODEL = "gpt-5.4"
logger = logging.getLogger("BridgeU.ApiGatewayConfig")


def available_api_models() -> list[str]:
    return [
        *AVAILABLE_GEMINI_MODELS,
        *AVAILABLE_CLAUDE_MODELS,
        *AVAILABLE_CODEX_MODELS,
        *AVAILABLE_XAI_API_MODELS,
    ]


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
    return DEFAULT_API_MODEL if DEFAULT_API_MODEL in available_api_models() else available_api_models()[0]


def config_path_for(global_config: Any) -> Path:
    bridge_home = Path(getattr(global_config, "bridge_home", "") or getattr(global_config, "project_root", "."))
    return bridge_home / "state" / API_GATEWAY_CONFIG_NAME


def load_api_gateway_config(global_config: Any) -> dict[str, Any]:
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
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    tmp.write_text(json.dumps(current, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)
    return current
