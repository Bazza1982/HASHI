"""Opt-in, client-neutral manual desktop API. No Agent runs or model inference."""
from __future__ import annotations

import asyncio
import base64
import hmac
import json
import logging
import time
from dataclasses import dataclass, field
from uuid import uuid4

from aiohttp import web
from orchestrator.desktop_contract import SESSION_TTL, MAX_FRAME_BYTES, DesktopError, fields, identifier, preview_options, view_options, validate_input

logger = logging.getLogger(__name__)


@dataclass
class DesktopSession:
    owner: str
    client: str
    binding: dict
    expires: float
    last_touch: float
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


def _is_loopback(request) -> bool:
    return (getattr(request, "remote", None) or "") in {"127.0.0.1", "::1", "localhost"}


def _admit_and_resolve_owner(api, request) -> str:
    """Admit a desktop caller and resolve its authenticated owner.

    Trust boundary (host-level model, reviewed 2026-11-19):

    * Loopback callers (the local Workbench on 127.0.0.1/::1) are exempt from
      the HASHI admin token. Loopback reachability is a host-level boundary,
      not a Windows-session boundary: on a multi-user Windows host, a process
      in ANY OS user session can reach a service bound to 127.0.0.1, so
      reachability alone does not prove the caller belongs to the interactive
      user's own session. The supported deployment assumes a single trusted
      OS user on this workstation; under that assumption the token only binds
      the local entry to a rotatable HASHI secret and the exemption removes a
      false gate, not a real one. Multi-user hosts need a per-session boundary
      (see docs/HASHI_MANUAL_DESKTOP.md, "Multi-user hosts").
    * Remote callers keep the existing admin-token requirement. A remote hop
      with no configured admin token is always rejected.
    * A loopback caller whose owner cannot be resolved falls back to the
      reserved "workbench-local" identity; the resolved owner is pinned for
      the lifetime of the session and is never derived from the client.
    """
    if not _is_loopback(request) and (not api.admin_token or not api._check_admin_auth(request)):
        raise DesktopError("desktop_auth_required", 403)
    owner = api._v1_owner_id(request)
    if owner:
        return owner
    if _is_loopback(request):
        return "workbench-local"
    raise DesktopError("desktop_auth_required", 403)


class DesktopSessionService:
    def __init__(self, broker_getter, *, clock=time.monotonic):
        self.broker_getter, self.clock = broker_getter, clock
        self.sessions = {}
        self.open_lock = asyncio.Lock()

    def broker(self):
        broker = self.broker_getter()
        if broker is None: raise DesktopError("desktop_worker_unavailable", 503)
        return broker

    async def _evict_for_capacity(self):
        """Never refuse the current user: drop the least recently touched session."""
        while len(self.sessions) >= 8:
            sid = min(self.sessions, key=lambda key: self.sessions[key].last_touch)
            record = self.sessions.pop(sid)
            try:
                await self.broker().invoke_desktop(record.binding, actor_id=record.owner,
                                                   session_id=sid, operation="desktop_close", args={})
            except Exception:
                pass  # Worker-side expiry remains authoritative on lost links.

    @staticmethod
    def _refresh_target(requested, targets):
        """Resolve a requested target without ever substituting another session.

        The stable identity of a desktop target is
        (instance_id, device_id, user_session_id): one physical computer can
        run several Windows interactive sessions, each with its own worker,
        and matching by device alone can select the wrong session. A
        same-device refresh is therefore only safe when the request pins the
        same HASHI instance, device and Windows user session. A restarted
        worker may advance worker_generation and re-register under a new
        capability_id; those fields are allowed to change and are only used
        to prefer the exact re-registration. Optional physical_host_id and
        interactive_session_id identifiers are verified when both the request
        and the live target provide them.

        * An explicit request resolves to the requested session, or raises
          desktop_target_changed when it no longer exists. It never falls
          back to another user session or another HASHI instance that shares
          device_id.
        * A device-scoped request without a user session resolves only when
          the device maps to exactly one live target; an ambiguous device is
          reported as desktop_target_changed so the client re-discovers and
          sends the full identity instead of guessing a session.
        * A capability-only request resolves by unique capability identity.
        * An absent/empty request keeps the no-target discovery behaviour and
          selects the first registered target.
        """
        if not targets:
            raise DesktopError("desktop_worker_unavailable", 503)
        if not isinstance(requested, dict) or not requested:
            return targets[0]

        instance_id = requested.get("instance_id")
        device_id = requested.get("device_id")
        user_session_id = requested.get("user_session_id")
        capability_id = requested.get("capability_id")
        physical_host_id = requested.get("physical_host_id")
        interactive_session_id = requested.get("interactive_session_id")
        if not any((instance_id, device_id, user_session_id, capability_id,
                    physical_host_id, interactive_session_id)):
            return targets[0]

        if capability_id and not any((instance_id, device_id, user_session_id,
                                      physical_host_id, interactive_session_id)):
            matches = [t for t in targets if t.get("capability_id") == capability_id]
            if len(matches) == 1:
                return matches[0]
            raise DesktopError("desktop_target_changed", 409)

        def _pins_session(target):
            return (
                (not instance_id or target.get("instance_id") == instance_id)
                and (not device_id or target.get("device_id") == device_id)
                and (not user_session_id or target.get("user_session_id") == user_session_id)
                and (not physical_host_id or not target.get("physical_host_id")
                     or target.get("physical_host_id") == physical_host_id)
                and (not interactive_session_id or not target.get("interactive_session_id")
                     or target.get("interactive_session_id") == interactive_session_id)
            )

        pinned = [t for t in targets if _pins_session(t)]
        if not pinned:
            raise DesktopError("desktop_target_changed", 409)
        if capability_id:
            for target in pinned:
                if target.get("capability_id") == capability_id:
                    return target
        if len(pinned) == 1:
            return pinned[0]
        # Multiple live targets satisfy the request identity: guessing a user
        # session or an instance is exactly the substitution this guard exists
        # to prevent. The client re-discovers and sends the full identity.
        raise DesktopError("desktop_target_changed", 409)

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
                targets = self.broker().desktop_targets()
                target = self._refresh_target(b.get("target"), targets)
                sid = "desktop-"+uuid4().hex
                # Validate the target and the open operation BEFORE evicting
                # any existing session: a failed open must never disrupt an
                # active viewer. Capacity eviction only runs after the worker
                # has confirmed the new session.
                info = await self.broker().invoke_desktop(target, actor_id=owner, session_id=sid,
                                                         operation="desktop_info", args={})
                await self._evict_for_capacity()
                now = self.clock()
                record = DesktopSession(owner, client, dict(target), now+SESSION_TTL, now)
                self.sessions[sid] = record
                return {"session_id": sid, "binding": target, **info}
        sid = identifier(b.get("session_id"), "session")
        record = self.sessions.get(sid)
        if record is None: raise DesktopError("desktop_session_expired", 410)
        if not hmac.compare_digest(owner, record.owner) or not hmac.compare_digest(client, record.client):
            raise DesktopError("desktop_session_forbidden", 403)
        now = self.clock()
        record.expires = now+SESSION_TTL
        record.last_touch = now
        mapping = {"previews": "desktop_previews", "frame": "desktop_frame", "control": "desktop_control", "input": "desktop_input", "view": "desktop_view", "close": "desktop_close"}
        if op not in mapping: raise DesktopError("desktop_invalid_operation")
        if op == "frame":
            args = {"after_frame": str(b.get("after_frame", ""))[:160]}
            if "refresh_profile" in b: args["refresh_profile"] = b["refresh_profile"]
        elif op == "previews": args = preview_options(b.get("options"))
        elif op == "view": args = view_options(b.get("options"))
        elif op == "input": args = {"lease_id": identifier(b.get("lease_id"), "lease"), "event": validate_input(b.get("event"))}
        elif op == "control":
            if b.get("mode") not in {"acquire", "heartbeat", "release"}: raise DesktopError("desktop_invalid_control")
            args = {"mode": b["mode"]}
            if args["mode"] != "acquire": args["lease_id"] = identifier(b.get("lease_id"), "lease")
        else: args = {}
        async def call():
            result = await self.broker().invoke_desktop(record.binding, actor_id=owner, session_id=sid,
                                                        operation=mapping[op], args=args)
            if op == "previews" and (self.sessions.get(sid) is not record or record.expires <= self.clock()):
                raise DesktopError("desktop_session_expired", 410)
            return result
        # Input is ordered separately from the frame request. Frames never hold
        # the session's mutation lock or wait behind slow keyboard rendering.
        if op in {"frame", "previews"}: return await call()
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

    def unavailable(error, *, phase, dispatched, readonly, headers):
        from orchestrator import ui_language
        from orchestrator.api_failures import operation_failure_payload
        from orchestrator.operation_failures import operation_failure

        failure = operation_failure(error, operation="desktop", phase=phase,
                                    unconfirmed=dispatched and not readonly)
        try:
            logger.error("Desktop operation failure: %s", failure.diagnostic())
        except Exception:
            pass  # A failed diagnostic sink must not replace the response.
        effect = "not_requested" if readonly else "unconfirmed" if dispatched else "not_started"
        return web.json_response(operation_failure_payload(
            failure, error_code="desktop_unavailable", effect_state=effect,
            locale=ui_language.preferred_locale(api),
        ), status=503, headers=headers)

    async def handle(request):
        headers = {"Cache-Control": "no-store, private", "Pragma": "no-cache", "X-Content-Type-Options": "nosniff"}
        authenticated, dispatched, readonly, phase = False, False, False, "scope"
        try:
            owner = _admit_and_resolve_owner(api, request)
            authenticated, phase = True, "parse"
            if request.content_type != "application/json": raise DesktopError("desktop_json_required", 415)
            raw = bytearray()
            async for chunk in request.content.iter_chunked(8192):
                raw.extend(chunk)
                if len(raw) > 32768: raise DesktopError("desktop_request_too_large", 413)
            body = json.loads(raw)
            readonly = isinstance(body, dict) and body.get("operation") in {"targets", "frame", "previews"}
            phase, dispatched = "read" if readonly else "dispatch", True
            result = await service.run(owner, body)
            phase = "presentation"
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
            return web.json_response({"ok": False, **exc.projection()}, status=exc.status, headers=headers)
        except (ValueError, TypeError, KeyError) as exc:
            if not dispatched:
                return web.json_response({"ok": False, "error_code": "desktop_invalid_request"}, status=400, headers=headers)
            return unavailable(exc, phase=phase, dispatched=dispatched, readonly=readonly, headers=headers)
        except Exception as exc:
            if not authenticated:
                return web.json_response({"ok": False, "error_code": "desktop_unavailable"}, status=503, headers=headers)
            return unavailable(exc, phase=phase, dispatched=dispatched, readonly=readonly, headers=headers)
    async def cleanup(_app):
        await service.close()
    api.app.router.add_post("/api/v1/desktop/operation", handle)
    api.app.on_cleanup.append(cleanup)
# HASHI-Fingerprint: hfp_a8af9118e2034e5782459ff2ccb89843
