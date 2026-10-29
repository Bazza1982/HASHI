"""Personal permission login using the existing shared FC auth routes."""
from __future__ import annotations

import asyncio
from aiohttp import web

from orchestrator.permissions.authority import PermissionDenied
from orchestrator.permissions.wire import PermissionRemoteError


def session_token(request):
    # Keep the app/API credential separate from the human session. Both fields
    # originate in the HTTP request; neither is copied from public turn metadata.
    token = request.headers.get("X-Workbench-Session", "").strip()
    if token:
        return token
    header = request.headers.get("Authorization", "")
    return header[7:].strip() if header.lower().startswith("bearer ") else ""


async def handle(api, request, operation):
    control = getattr(api, "permission_control", None)
    if control is None:
        return None
    try:
        if operation == "login":
            try:
                body = await request.json()
            except Exception:
                return web.json_response({"ok": False, "error": "invalid JSON body"}, status=400)
            if (not isinstance(body, dict) or set(body) - {"org_id", "email", "password"}
                    or any(not isinstance(body.get(key), str) or not body[key]
                           for key in ("org_id", "email", "password"))):
                return web.json_response({"ok": False, "error": "org_id, email, and password are required"}, status=400)
            result = await asyncio.to_thread(control.login, **body)
        elif operation == "logout":
            result = await asyncio.to_thread(control.logout, session_token=session_token(request))
        elif operation == "me":
            user = await asyncio.to_thread(control.session_user, session_token=session_token(request))
            result = {"ok": True, "user": user}
        else:
            raise ValueError("unknown permission authentication action")
    except (PermissionDenied, PermissionRemoteError):
        return web.json_response({"ok": False, "error": "human session authentication failed"}, status=401)
    except (OSError, TimeoutError):
        return web.json_response({"ok": False, "error": "permission authentication is unavailable"}, status=503)
    return web.json_response(result)
