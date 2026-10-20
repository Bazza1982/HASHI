"""Fail-open notifications to the local AgentPresence desktop companion.

The companion (AgentPresence.exe) exposes a token-authenticated local API at
http://127.0.0.1:7740.  This module is the single HASHI-side integration
surface for FC: every call is optional, bounded in time, and must never raise
into HASHI's main call/delivery flows.

Mirroring is gated by the owner-scoped ``/agentpresence on|off`` preference
(default off), stored through the connector-neutral preference service
(``orchestrator.connector_delivery_preferences``).  A disabled switch, a
missing token, or an unreachable companion are all treated as "skip".
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

BASE_URL_ENV = "AGENT_PRESENCE_URL"
DEFAULT_BASE_URL = "http://127.0.0.1:7740"
TIMEOUT_SECONDS = 2.0
CONNECTOR_ID = "agentpresence"
PREFERENCE_KEY = "mirror"


def _base_url() -> str:
    return str(os.environ.get(BASE_URL_ENV) or DEFAULT_BASE_URL).rstrip("/")


def _token() -> str:
    app_data = os.environ.get("APPDATA")
    if not app_data:
        return ""
    token_path = Path(app_data) / "AgentPresence" / "ap-token"
    try:
        return token_path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def mirror_enabled(bridge_home: Any, owner_id: Any) -> bool:
    """Owner-scoped mirror switch; default off (fail closed for the optional sink)."""
    if bridge_home is None:
        return False
    owner = str(owner_id or "").strip()
    if not owner:
        return False
    try:
        from orchestrator.connector_delivery_preferences import (
            get_connector_preference,
        )

        return bool(
            get_connector_preference(
                bridge_home,
                owner,
                CONNECTOR_ID,
                PREFERENCE_KEY,
                default=False,
            )
        )
    except Exception:
        return False


def _post(path: str, *, data: bytes | None = None, content_type: str = "") -> bool:
    """POST to the companion; True only on an accepted response.  Never raises."""
    token = _token()
    if not token:
        return False
    headers = {
        "X-AP-Token": token,
        "User-Agent": "hashi-agent-presence/1",
    }
    if content_type:
        headers["Content-Type"] = content_type
    request = urllib.request.Request(
        _base_url() + path, data=data, headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            return 200 <= int(response.status) < 300
    except (OSError, ValueError, urllib.error.HTTPError, urllib.error.URLError):
        return False


def _post_json(path: str, payload: dict[str, Any]) -> bool:
    try:
        body = json.dumps(payload).encode("utf-8")
    except (TypeError, ValueError):
        return False
    return _post(path, data=body, content_type="application/json")


def call_started(bridge_home: Any, owner_id: Any) -> bool:
    """Companion: an incoming call began (character leaves the desk to answer)."""
    if not mirror_enabled(bridge_home, owner_id):
        return False
    return _post_json("/v1/call/incoming", {})


def call_ended(bridge_home: Any, owner_id: Any) -> bool:
    """Companion: the call ended (character returns to the desk)."""
    if not mirror_enabled(bridge_home, owner_id):
        return False
    return _post_json("/v1/call/ended", {})


def voice_bytes(bridge_home: Any, owner_id: Any, data: bytes, mime_type: str) -> bool:
    """Mirror one reply audio asset so the companion plays it as speech."""
    if not mirror_enabled(bridge_home, owner_id):
        return False
    if not isinstance(data, (bytes, bytearray)) or not data:
        return False
    content_type = str(mime_type or "audio/ogg").strip() or "audio/ogg"
    return _post("/v1/voice/speak", data=bytes(data), content_type=content_type)


__all__ = [
    "BASE_URL_ENV",
    "CONNECTOR_ID",
    "DEFAULT_BASE_URL",
    "PREFERENCE_KEY",
    "TIMEOUT_SECONDS",
    "call_ended",
    "call_started",
    "mirror_enabled",
    "voice_bytes",
]
