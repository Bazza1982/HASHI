"""Optional aiohttp registration. Reuse the existing authenticated ingress owner.

No default authorization implementation is provided: missing ports fail closed.
This module does not bind a port or start any process.
"""
from __future__ import annotations
import json
from typing import Any, Awaitable, Callable
from .ports import LiveApplicationPort
from .protocol import LiveVoiceError

ROOT = "/api/v1/live-voice"
ROUTES = (
    ("POST", "/context", "context"), ("POST", "/calls", "start"),
    ("POST", "/attempts/{attempt_id}/cancel", "cancel_start"),
    ("GET", "/attempts/{attempt_id}", "attempt"),
    ("GET", "/calls/{call_id}", "snapshot"),
    ("POST", "/calls/{call_id}/resume", "resume"),
    ("GET", "/calls/{call_id}/events", "events"),
    ("POST", "/calls/{call_id}/observations", "observe"),
    ("POST", "/calls/{call_id}/controls", "control"),
)


def register_live_voice_routes(app: Any, service: LiveApplicationPort,
                               authorize: Callable[[Any, str, dict], Awaitable[Any]], *, qualified: bool = False):
    from aiohttp import web
    if not callable(authorize) or not callable(getattr(service, "invoke", None)):
        raise TypeError("existing authority resolver and fully implemented application port required")

    async def capability(request):
        # Even capabilities use the existing authority boundary.
        try:
            await authorize(request, "capabilities", {})
            return web.json_response({"ok": True, "capability": {"protocol_version": "1.0",
                "available": bool(qualified and service.available)}}, headers={"Cache-Control": "no-store"})
        except LiveVoiceError as exc:
            return web.json_response({"ok": False, "error_code": exc.code}, status=exc.status, headers={"Cache-Control": "no-store"})

    app.router.add_get(ROOT + "/capabilities", capability)

    def make_handler(operation):
        async def handler(request):
            try:
                if not qualified or (operation != "context" and not service.available):
                    raise LiveVoiceError("live_not_enabled", 503)
                payload = dict(request.query)
                if request.method != "GET":
                    if request.content_type != "application/json":
                        raise LiveVoiceError("live_content_type_invalid", 415)
                    raw, total = [], 0
                    async for chunk in request.content.iter_chunked(16384):
                        total += len(chunk)
                        if total > 98304:
                            raise LiveVoiceError("live_body_limit", 413)
                        raw.append(chunk)
                    value = json.loads(b"".join(raw))
                    if not isinstance(value, dict):
                        raise LiveVoiceError("live_body_invalid")
                    payload.update(value)
                # Route IDs cannot be overwritten by JSON/query parameters.
                for key, value in request.match_info.items():
                    if key in payload and payload[key] != value:
                        raise LiveVoiceError("live_route_conflict")
                    payload[key] = value
                if "owner_id" in payload or "provider_session_id" in payload:
                    raise LiveVoiceError("live_authority_field_forbidden")
                authority = await authorize(request, operation, payload)
                # Implementation MUST validate operation-specific fields, convert numeric GET
                # expectations strictly, and compare stored CallBinding with this authority.
                value = await service.invoke(operation, authority, payload)
                return web.json_response(dict(value), headers={"Cache-Control": "no-store"})
            except LiveVoiceError as exc:
                return web.json_response({"ok": False, "error_code": exc.code, "accepted": None if exc.status >= 500 and operation in {"start", "control"} else False},
                                         status=exc.status, headers={"Cache-Control": "no-store"})
            except json.JSONDecodeError:
                return web.json_response({"ok": False, "error_code": "live_body_invalid", "accepted": False}, status=400,
                                         headers={"Cache-Control": "no-store"})
        return handler

    for method, path, operation in ROUTES:
        app.router.add_route(method, ROOT + path, make_handler(operation))
