"""Read-only discovery for pending permission prompts in one current Session."""
from __future__ import annotations

import asyncio
from aiohttp import web

from orchestrator.permissions.authority import PermissionDenied
from orchestrator.permissions.models import PermissionSubject
from orchestrator.permissions.wire import PermissionRemoteError
from orchestrator.session_store import SessionNotFound, SessionConflict


async def read(api, request):
    headers = {"Cache-Control": "no-store"}
    owner = api._strict_authenticated_owner(request)
    if owner is None:
        return web.json_response({"ok": False, "error_code": "not_authenticated"}, status=401, headers=headers)
    try:
        session = api.session_store.get_session(
            str(request.match_info["session_id"]), owner_id=owner, include_deleted=False,
        )
        if (session["status"] != "active" or request.query.get("agent_id") != session["agent_id"]
                or request.query.get("context_generation") != str(session["context_generation"])):
            return web.json_response({"ok": False, "error_code": "permission_session_changed"}, status=409, headers=headers)
        subject = PermissionSubject(api.session_store.instance_id, owner, session["agent_id"],
                                    session["session_id"], str(session["context_generation"]))
        control = getattr(api, "permission_control", None)
        if control is None:
            return web.json_response({"ok": True, "requests": [], "next_before": None}, headers=headers)
        page = await asyncio.to_thread(
            control.list_session_requests, subject, before=request.query.get("before"), limit=100,
        )
        rows = [{"request_id": view.request.request_id, "revision": view.request.revision,
                 "expires_at": view.request.expires_at}
                for view in page.requests if view.request.state == "pending"
                and view.task_state in {"active", "paused"} and view.binding_state == "active"]
        return web.json_response({
            "ok": True, "instance_id": subject.instance_id, "agent_id": subject.agent_id,
            "session_id": subject.session_id, "context_generation": int(subject.context_generation),
            "requests": rows, "next_before": page.next_before,
        }, headers=headers)
    except (SessionNotFound, SessionConflict) as error:
        return api._v1_error(error)
    except (PermissionDenied, PermissionRemoteError, OSError, ValueError):
        return web.json_response({"ok": False, "error_code": "permission_requests_unavailable"}, status=503, headers=headers)
