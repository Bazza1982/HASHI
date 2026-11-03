"""Authenticated, bounded, no-store control protocol installed in Functions."""

from __future__ import annotations

import json
from pathlib import Path

from aiohttp import web

from orchestrator.frontend_call.contract import CallError
from orchestrator.frontend_call.ports import HashiPorts
from .config import StandbyProfiles
from .contract import MAX_BODY_BYTES, StandbyError
from .service import StandbyService

HEADERS = {"Cache-Control": "no-store, private", "Pragma": "no-cache",
           "X-Content-Type-Options": "nosniff"}


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def register_standby_api(api):
    profiles = StandbyProfiles(Path(api.config_path).parent / "standby_profiles.json")
    service = StandbyService(api.session_store, profiles, HashiPorts(api))
    api.standby_service = service

    async def handle(request):
        try:
            if api._is_governed_profile():
                raise StandbyError("standby_profile_unsupported", 501)
            if not api.admin_token or not api._check_admin_auth(request):
                raise StandbyError("standby_auth_required", 403)
            owner = api._v1_owner_id(request)
            if not owner:
                raise StandbyError("standby_auth_required", 403)
            if request.content_type != "application/json":
                raise StandbyError("standby_json_required", 415)
            chunks, size = [], 0
            async for chunk in request.content.iter_chunked(8192):
                size += len(chunk)
                if size > MAX_BODY_BYTES:
                    raise StandbyError("standby_body_limit", 413)
                chunks.append(chunk)
            try:
                body = json.loads(b"".join(chunks).decode("utf-8"),
                    object_pairs_hook=_object,
                    parse_constant=lambda value: (_ for _ in ()).throw(ValueError("nonfinite")))
            except (ValueError, UnicodeError, RecursionError) as error:
                raise StandbyError("standby_json_invalid") from error
            result = await service.invoke(owner, body)
            return web.json_response(result, headers=HEADERS)
        except (StandbyError, CallError) as error:
            code = error.code if isinstance(error, StandbyError) else "standby_scope_changed"
            return web.json_response({"ok": False, "error_code": code},
                                     status=error.status, headers=HEADERS)
        except Exception:
            # Default evidence never receives body/configuration/environmental
            # text. Query the original operation ID before retrying a mutation.
            return web.json_response({"ok": False, "error_code": "standby_control_unavailable",
                                      "effect_state": "unconfirmed"},
                                     status=503, headers=HEADERS)

    api.app.router.add_post("/api/v1/standby/operation", handle)
