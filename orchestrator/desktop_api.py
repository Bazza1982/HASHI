"""Opt-in, client-neutral manual desktop API. No Agent runs or model inference."""
from __future__ import annotations

import asyncio
import base64
import hmac
import json
import os
import time
from dataclasses import dataclass, field
from uuid import uuid4

from aiohttp import web
from orchestrator.desktop_contract import SESSION_TTL, MAX_FRAME_BYTES, DesktopError, fields, identifier, view_options, validate_input


@dataclass
class DesktopSession:
    owner: str
    client: str
    binding: dict
    expires: float
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class DesktopSessionService:
    def __init__(self, broker_getter, *, clock=time.monotonic):
        self.broker_getter, self.clock = broker_getter, clock
        self.sessions = {}
        self.open_lock = asyncio.Lock()

    def broker(self):
        broker = self.broker_getter()
        if broker is None: raise DesktopError("desktop_worker_unavailable", 503)
        return broker

    async def run(self, owner, body):
        b = fields(body, {"operation", "client_id", "session_id", "target", "options", "after_frame", "refresh_profile", "mode", "lease_id", "event"}, {"operation", "client_id"})
        client = identifier(b["client_id"], "client")
        op = b["operation"]
        for key, record in list(self.sessions.items()):
            if record.expires <= self.clock(): self.sessions.pop(key, None)
        if op == "targets":
            return {"targets": self.broker().desktop_targets(), "protocol_version": 1}
        if op == "open":
            async with self.open_lock:
                if len(self.sessions) >= 8: raise DesktopError("desktop_session_limit", 429)
                target = b.get("target")
                targets = self.broker().desktop_targets()
                if not isinstance(target, dict) or target not in targets:
                    raise DesktopError("desktop_target_changed", 409)
                sid = "desktop-"+uuid4().hex
                info = await self.broker().invoke_desktop(target, actor_id=owner, session_id=sid,
                                                         operation="desktop_info", args={})
                record = DesktopSession(owner, client, dict(target), self.clock()+SESSION_TTL)
                self.sessions[sid] = record
                return {"session_id": sid, "binding": target, **info}
        sid = identifier(b.get("session_id"), "session")
        record = self.sessions.get(sid)
        if record is None: raise DesktopError("desktop_session_expired", 410)
        if not hmac.compare_digest(owner, record.owner) or not hmac.compare_digest(client, record.client):
            raise DesktopError("desktop_session_forbidden", 403)
        record.expires = self.clock()+SESSION_TTL
        mapping = {"frame": "desktop_frame", "control": "desktop_control", "input": "desktop_input", "view": "desktop_view", "close": "desktop_close"}
        if op not in mapping: raise DesktopError("desktop_invalid_operation")
        if op == "frame":
            args = {"after_frame": str(b.get("after_frame", ""))[:160]}
            if "refresh_profile" in b: args["refresh_profile"] = b["refresh_profile"]
        elif op == "view": args = view_options(b.get("options"))
        elif op == "input": args = {"lease_id": identifier(b.get("lease_id"), "lease"), "event": validate_input(b.get("event"))}
        elif op == "control":
            if b.get("mode") not in {"acquire", "heartbeat", "release"}: raise DesktopError("desktop_invalid_control")
            args = {"mode": b["mode"]}
            if args["mode"] != "acquire": args["lease_id"] = identifier(b.get("lease_id"), "lease")
        else: args = {}
        async def call():
            return await self.broker().invoke_desktop(record.binding, actor_id=owner, session_id=sid,
                                                      operation=mapping[op], args=args)
        # Input is ordered separately from the frame request. Frames never hold
        # the session's mutation lock or wait behind slow keyboard rendering.
        if op == "frame": return await call()
        async with record.lock:
            try:
                return await call()
            finally:
                if op == "close": self.sessions.pop(sid, None)

    async def close(self):
        records, self.sessions = self.sessions, {}
        for sid, record in records.items():
            try:
                await self.broker().invoke_desktop(record.binding, actor_id=record.owner, session_id=sid,
                                                  operation="desktop_close", args={})
            except Exception:
                pass  # Worker-side expiry remains authoritative on lost links.


def register_desktop_api(api):
    service = DesktopSessionService(lambda: getattr(api.orchestrator, "capability_broker", None))
    async def handle(request):
        headers = {"Cache-Control": "no-store, private", "Pragma": "no-cache", "X-Content-Type-Options": "nosniff"}
        try:
            if not getattr(getattr(api, "global_config", None), "desktop_enabled", False) and os.environ.get("HASHI_DESKTOP_ENABLED") != "1":
                raise DesktopError("desktop_disabled", 404)
            # The personal local API must have its EXISTING admin token configured.
            # HASHI Remote injects it on the authenticated loopback hop.
            if api._is_governed_profile():
                raise DesktopError("desktop_profile_unsupported", 501)
            if not api.admin_token or not api._check_admin_auth(request):
                raise DesktopError("desktop_auth_required", 403)
            owner = api._v1_owner_id(request)
            if not owner: raise DesktopError("desktop_auth_required", 403)
            if request.content_type != "application/json": raise DesktopError("desktop_json_required", 415)
            raw = bytearray()
            async for chunk in request.content.iter_chunked(8192):
                raw.extend(chunk)
                if len(raw) > 32768: raise DesktopError("desktop_request_too_large", 413)
            body = json.loads(raw)
            result = await service.run(owner, body)
            if body["operation"] == "frame":
                meta = result.get("meta")
                headers["X-Desktop-Meta"] = json.dumps(meta, ensure_ascii=True, separators=(",", ":"))
                if len(headers["X-Desktop-Meta"]) > 8192: raise DesktopError("desktop_invalid_frame", 502)
                jpeg = result.get("jpeg")
                if jpeg is None: return web.Response(status=204, headers=headers)
                if not isinstance(jpeg, str) or len(jpeg) > MAX_FRAME_BYTES*4//3+8:
                    raise DesktopError("desktop_frame_too_large", 502)
                data = base64.b64decode(jpeg, validate=True)
                if len(data) > MAX_FRAME_BYTES or not data.startswith(b"\xff\xd8"):
                    raise DesktopError("desktop_invalid_frame", 502)
                return web.Response(body=data, content_type="image/jpeg", headers=headers)
            return web.json_response({"ok": True, **result}, headers=headers)
        except DesktopError as exc:
            return web.json_response({"ok": False, "error_code": exc.code}, status=exc.status, headers=headers)
        except (ValueError, TypeError, KeyError):
            return web.json_response({"ok": False, "error_code": "desktop_invalid_request"}, status=400, headers=headers)
        except Exception:
            return web.json_response({"ok": False, "error_code": "desktop_unavailable"}, status=503, headers=headers)
    async def cleanup(_app):
        await service.close()
    api.app.router.add_post("/api/v1/desktop/operation", handle)
    api.app.on_cleanup.append(cleanup)
