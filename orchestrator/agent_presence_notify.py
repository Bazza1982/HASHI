"""AgentPresence Frontend Connector adapter and bounded local transport.

The companion (AgentPresence.exe) exposes a token-authenticated local API at
http://127.0.0.1:7740.  This module is the single HASHI-side integration
surface for FC.  The adapter consumes standard ``hashi.frontend-event``
envelopes; call and voice producers never import this module directly.

Mirroring is admitted by the owner-scoped ``/agentpresence on|off`` preference
(default off), stored through the connector-neutral preference service
(``orchestrator.connector_delivery_preferences``).  The standard FC route or
call subscriber list freezes that decision before this adapter runs.  A missing
token or unreachable companion is fail-open for the originating operation.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from collections.abc import Callable, Mapping
from typing import Any

BASE_URL_ENV = "AGENT_PRESENCE_URL"
DEFAULT_BASE_URL = "http://127.0.0.1:7740"
TIMEOUT_SECONDS = 2.0
CONNECTOR_ID = "agentpresence"
PREFERENCE_KEY = "mirror"
VOICE_MAX_BYTES = 25 * 1024 * 1024


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


def token_matches(candidate: Any) -> bool:
    """Authenticate an AP-labelled ingress without exposing the local token."""
    expected = _token()
    supplied = str(candidate or "").strip()
    return bool(expected and supplied and hmac.compare_digest(expected, supplied))


def _json_request(
    method: str,
    path: str,
    payload: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """One bounded token-authenticated AP settings request; never raises."""
    token = _token()
    if not token:
        return None
    data = None
    headers = {
        "X-AP-Token": token,
        "User-Agent": "hashi-agent-presence/1",
        "Accept": "application/json",
    }
    if payload is not None:
        try:
            data = json.dumps(dict(payload)).encode("utf-8")
        except (TypeError, ValueError):
            return None
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        _base_url() + path,
        data=data,
        headers=headers,
        method=str(method or "GET").upper(),
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            if not 200 <= int(response.status) < 300:
                return None
            decoded = json.loads(response.read().decode("utf-8"))
            return dict(decoded) if isinstance(decoded, Mapping) else None
    except (
        OSError,
        ValueError,
        json.JSONDecodeError,
        urllib.error.HTTPError,
        urllib.error.URLError,
    ):
        return None


def get_hashi_binding() -> dict[str, Any] | None:
    """Read AP's singular HASHI instance/default-Agent binding."""
    return _json_request("GET", "/v1/hashi")


def set_hashi_binding(
    *, instance_id: str = "", base_url: str = "", default_agent: str = ""
) -> dict[str, Any] | None:
    """Atomically set or clear AP's singular HASHI binding."""
    return _json_request(
        "PUT",
        "/v1/hashi",
        {
            "instance_id": str(instance_id or ""),
            "base_url": str(base_url or ""),
            "default_agent": str(default_agent or ""),
        },
    )


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


def _receipt(
    event: Mapping[str, Any],
    endpoint_id: str,
    status: str,
) -> dict[str, Any]:
    from orchestrator.frontend_contracts import normalize_delivery_receipt

    return normalize_delivery_receipt(
        {
            "type": "hashi.delivery-receipt",
            "version": 1,
            "event_id": str(event.get("event_id") or ""),
            "endpoint_id": str(endpoint_id),
            "status": status,
            "proof": None,
        }
    )


class AgentPresenceConnectorAdapter:
    """Render relevant standard FC events to the local companion API."""

    connector_id = CONNECTOR_ID

    def __init__(
        self,
        bridge_home: Any,
        owner_id: Any,
        *,
        enabled: bool,
        media_resolver: Callable[
            [Mapping[str, Any], Mapping[str, Any]], Mapping[str, Any]
        ]
        | None = None,
    ) -> None:
        self.bridge_home = bridge_home
        self.owner_id = str(owner_id or "").strip()
        self.enabled = bool(enabled)
        self.media_resolver = media_resolver

    async def dispatch(
        self,
        event: Mapping[str, Any],
        *,
        endpoint_id: str,
    ) -> dict[str, Any]:
        from orchestrator.frontend_connector_registry import require_connector_event

        require_connector_event(self.connector_id, event)
        if not self.enabled or not _token():
            return _receipt(event, endpoint_id, "skipped")

        semantic_kind = str(event.get("semantic_kind") or "").strip().casefold()
        if semantic_kind == "call.started":
            accepted = await asyncio.to_thread(_post_json, "/v1/call/incoming", {})
            return _receipt(event, endpoint_id, "accepted" if accepted else "unknown")
        if semantic_kind == "call.ended":
            accepted = await asyncio.to_thread(_post_json, "/v1/call/ended", {})
            return _receipt(event, endpoint_id, "accepted" if accepted else "unknown")

        # The active call Connector owns call audio.  AgentPresence consumes
        # only the call lifecycle so its away/hold animation cannot be
        # displaced by the ordinary phone-reply animation, and one machine
        # never plays the same synthesized turn through two local frontends.
        if str(event.get("presentation_channel") or "").strip().casefold() == "call":
            return _receipt(event, endpoint_id, "skipped")

        media_refs = [
            block
            for block in event.get("content_blocks") or ()
            if isinstance(block, Mapping)
            and str(block.get("type") or "").casefold() == "media_ref"
            and str(block.get("role") or "").casefold() in {"voice", "voice_message"}
        ]
        if not media_refs or not callable(self.media_resolver):
            return _receipt(event, endpoint_id, "skipped")

        outcomes: list[str] = []
        for media_ref in media_refs:
            try:
                resolved = self.media_resolver(event, media_ref)
                declared_size = (
                    int(resolved.get("size_bytes") or 0)
                    if isinstance(resolved, Mapping)
                    else 0
                )
                if declared_size > VOICE_MAX_BYTES:
                    outcomes.append("skipped")
                    continue
                payload = (
                    resolved.get("data") if isinstance(resolved, Mapping) else None
                )
                mime_type = (
                    str(resolved.get("mime_type") or "audio/ogg")
                    if isinstance(resolved, Mapping)
                    else "audio/ogg"
                )
            except Exception:
                outcomes.append("failed")
                continue
            if not isinstance(payload, (bytes, bytearray)) or not payload:
                outcomes.append("skipped")
                continue
            if len(payload) > VOICE_MAX_BYTES:
                outcomes.append("skipped")
                continue
            accepted = await asyncio.to_thread(
                _post,
                "/v1/voice/speak",
                data=bytes(payload),
                content_type=mime_type,
            )
            outcomes.append("accepted" if accepted else "unknown")

        if "unknown" in outcomes:
            status = "unknown"
        elif "failed" in outcomes:
            status = "failed"
        elif "accepted" in outcomes:
            status = "accepted"
        else:
            status = "skipped"
        return _receipt(event, endpoint_id, status)


__all__ = [
    "BASE_URL_ENV",
    "AgentPresenceConnectorAdapter",
    "CONNECTOR_ID",
    "DEFAULT_BASE_URL",
    "PREFERENCE_KEY",
    "TIMEOUT_SECONDS",
    "VOICE_MAX_BYTES",
    "call_ended",
    "call_started",
    "get_hashi_binding",
    "mirror_enabled",
    "set_hashi_binding",
    "token_matches",
    "voice_bytes",
]
