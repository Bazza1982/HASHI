"""AP quick controls projected from PAO owners over the existing Worker RPC.

This is a local Frontend Connector adapter, not a model/Agent configuration store.
"""
from __future__ import annotations

import base64
import hashlib
import html
import json
import re
from typing import Any

from orchestrator.flexible_backend_registry import (
    HER_V2_ENGINE, canonical_backend_engine, public_backend_engine, public_model_provider,
)
from orchestrator.her_v2.v3_config import HER_V3_CONFIGURATION_STATE_KEY

PREFIX = "__hashi_ap_controls_v1__:"
SOURCE = "agentpresence_controls"
SETTINGS = frozenset({"backend", "provider", "model", "effort"})


class ControlError(ValueError):
    def __init__(self, code: str, status: int = 409, message: str = ""):
        super().__init__(message or code)
        self.code, self.status = code, status


def configuration(runtime) -> dict:
    manager = runtime.backend_manager
    return {
        "engine": public_backend_engine(runtime.config.active_backend),
        "provider": public_model_provider(runtime.get_current_provider()) or "",
        "model": runtime.get_current_model(),
        "effort": runtime._get_current_effort(),
        "mode": manager.agent_mode,
    }


def configuration_version(config: dict) -> str:
    return hashlib.sha256(json.dumps(config, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def snapshot(runtime) -> dict:
    from orchestrator.agent_creation_policy import runtime_backend_catalogue
    config = configuration(runtime)
    return {
        "ok": True, "controls_version": 1, "agent_id": runtime.name,
        "configuration": config, "configuration_version": configuration_version(config),
        "backend_catalogue": runtime_backend_catalogue(runtime),
        "busy": bool(runtime._backend_busy()), "online": bool(runtime.backend_ready),
        "commands": {key: bool(runtime._is_command_allowed(key)) and not (
            key == "model" and runtime.backend_manager.agent_mode in {"wrapper", "audit", "dual-brain"}
        ) for key in SETTINGS},
    }


def persisted_configuration_matches(runtime, actual: dict, expected: dict) -> bool:
    """Read strictly through the existing owner, never a frontend file reader."""
    manager = runtime.backend_manager
    state = manager.state_store.read_strict()
    engine = runtime.config.active_backend
    if canonical_backend_engine(state.get("active_backend")) != canonical_backend_engine(engine):
        return False
    if engine == HER_V2_ENGINE and any(k in expected for k in ("provider", "model")):
        saved = state.get(HER_V3_CONFIGURATION_STATE_KEY) or {}
        if (public_model_provider(saved.get("provider")) != actual["provider"]
                or saved.get("model") != actual["model"]):
            return False
    elif engine != HER_V2_ENGINE and "model" in expected and state.get("active_model") != actual["model"]:
        return False
    saved_effort = (state.get("backend_efforts") or {}).get(engine)
    normalize = lambda v: {"none": "off", "zero": "off"}.get(str(v).casefold(), v) if v else None
    return normalize(saved_effort) == normalize(actual["effort"])


def _notice(result) -> str:
    for message in (result or {}).get("messages", []):
        text = message.get("text") if isinstance(message, dict) else ""
        if text:
            return html.unescape(re.sub("<[^>]*>", "", str(text)))[:1200]
    return ""


async def apply_setting(runtime, body: dict) -> dict:
    from orchestrator.admin_local_testing import execute_local_command
    setting = body.get("setting")
    if setting not in SETTINGS:
        raise ControlError("control_setting_invalid", 400)
    value = body.get("value")
    if not isinstance(value, str) or not value or len(value) > 512 or any(c.isspace() for c in value):
        raise ControlError("control_value_invalid", 400)
    before = configuration(runtime)
    if body.get("configuration_version") != configuration_version(before):
        raise ControlError("control_configuration_changed")
    if not runtime._is_command_allowed(setting):
        raise ControlError("command_disabled", 403)
    if not runtime.backend_ready:
        raise ControlError("agent_unavailable", 503)
    if setting == "backend" and runtime._backend_busy():
        raise ControlError("backend_busy")
    catalogue = snapshot(runtime)["backend_catalogue"]
    if not catalogue.get("ok"):
        raise ControlError("backend_catalogue_unavailable", 503)
    steps: list[tuple[str, str, dict]] = []
    if setting == "backend":
        row = catalogue["backends"].get(value)
        if not row or not row.get("available"):
            raise ControlError("backend_unavailable")
        if value == "her-v3":
            steps.append(("backend", value, {"engine": value}))
            provider = body.get("provider") or ""
            if provider:
                option = (row.get("providers") or {}).get(provider)
                if not option or not option.get("available"):
                    raise ControlError("provider_unavailable")
                steps.append(("provider", provider, {"engine": value, "provider": provider}))
        else:
            # Selection policy and default belong to HASHI's effective catalogue.
            model = row.get("default_model")
            if not model or model not in row.get("models", []):
                raise ControlError("model_unavailable")
            steps.append(("backend", value + " " + model, {"engine": value, "model": model}))
    else:
        steps.append((setting, value, {setting: value}))
    last_notice = ""
    with runtime.backend_manager.strict_settings_save():
        for command, arguments, expected in steps:
            if not runtime._is_command_allowed(command):
                raise ControlError("command_disabled", 403)
            result = await execute_local_command(
                runtime, "/" + command + " " + arguments, source_channel=SOURCE,
                session_metadata={"connector_id": "agentpresence", "ui_locale": body.get("ui_locale", "en")},
            )
            last_notice = _notice(result)
            if not result.get("ok"):
                raise ControlError(result.get("error_code") or "control_update_failed", 409, last_notice)
            actual = configuration(runtime)
            if any(actual.get(key) != value for key, value in expected.items()):
                raise ControlError("control_not_applied", 409, last_notice)
            if not persisted_configuration_matches(runtime, actual, expected):
                raise ControlError("control_persistence_unconfirmed", 503)
    return {**snapshot(runtime), "persisted": True, "message": last_notice}


def encode(body: dict) -> str:
    return PREFIX + base64.urlsafe_b64encode(json.dumps(body, separators=(",", ":")).encode()).decode()


async def try_dispatch(runtime: Any, text: str, *, source_channel: str):
    if not str(text).startswith(PREFIX):
        return None
    if source_channel != SOURCE:
        return {"ok": False, "error_code": "control_transport_forbidden", "http_status": 403}
    if getattr(runtime, "is_function_worker_proxy", False):
        return await runtime.execute_slash_command(text, source_channel=SOURCE)
    try:
        if len(text) > 8192:
            raise ControlError("control_request_invalid", 400)
        body = json.loads(base64.b64decode(text[len(PREFIX):], altchars=b"-_", validate=True))
        if not isinstance(body, dict) or body.get("op") not in {"read", "apply"}:
            raise ControlError("control_request_invalid", 400)
        if body.get("agent_id") != runtime.name:
            raise ControlError("control_agent_changed")
        if body["op"] == "read":
            return snapshot(runtime)
        return await apply_setting(runtime, body)
    except ControlError as exc:
        return {"ok": False, "error_code": exc.code, "http_status": exc.status, "message": str(exc)}
    except (OSError, ValueError, TypeError, KeyError):
        return {"ok": False, "error_code": "control_update_unconfirmed", "http_status": 503}
