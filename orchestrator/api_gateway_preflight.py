from __future__ import annotations

import shutil
from typing import Any

from adapters.xai_oauth_credentials import (
    find_hermes_auth_path,
    hermes_oauth_available,
    secrets_oauth_refresh_available,
    xai_api_credentials_available,
)
from orchestrator.flexible_backend_registry import get_secret_lookup_order


def _cli_command(global_config: Any, engine: str) -> str:
    mapping = {
        "gemini-cli": getattr(global_config, "gemini_cmd", "gemini"),
        "claude-cli": getattr(global_config, "claude_cmd", "claude"),
        "codex-cli": getattr(global_config, "codex_cmd", "codex"),
        "grok-cli": getattr(global_config, "grok_cmd", "grok"),
    }
    return str(mapping.get(engine, "") or "").strip()


def _has_secrets_xai_credentials(secrets: dict) -> bool:
    return xai_api_credentials_available(secrets=secrets)


def check_gateway_engine(global_config: Any, secrets: dict, engine: str) -> dict[str, Any]:
    if engine in {"gemini-cli", "claude-cli", "codex-cli", "grok-cli"}:
        cmd = _cli_command(global_config, engine)
        found = shutil.which(cmd) if cmd else None
        if found:
            return {"available": True, "reason": found}
        return {"available": False, "reason": f"'{cmd or engine}' not found on PATH"}

    if engine == "xai-api":
        hermes_home = str(getattr(global_config, "hermes_home", "") or "").strip() or None
        if hermes_oauth_available(hermes_home):
            auth_path = find_hermes_auth_path(hermes_home)
            return {
                "available": True,
                "reason": f"Hermes OAuth ({auth_path})" if auth_path else "Hermes OAuth",
            }
        if secrets_oauth_refresh_available(secrets):
            return {"available": True, "reason": "xai_oauth_refresh_token configured"}
        if _has_secrets_xai_credentials(secrets):
            return {"available": True, "reason": "xai_api_key configured"}
        return {
            "available": False,
            "reason": "no Hermes xai-oauth, refresh token, or xai_api_key in secrets.json",
        }

    return {"available": True, "reason": "unknown engine, assuming available"}


def check_gateway_engines(
    global_config: Any,
    secrets: dict,
    engines: set[str] | list[str],
) -> dict[str, dict[str, Any]]:
    status: dict[str, dict[str, Any]] = {}
    for engine in sorted(set(engines)):
        status[engine] = check_gateway_engine(global_config, secrets, engine)
    return status