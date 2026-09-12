"""Authenticated personal-admin ingress for command UI v1.

Uses the existing command endpoint and Remote HMAC hop. This is deliberately
not a new public/anonymous endpoint, pairing scheme, or enterprise impersonation.
"""
from __future__ import annotations

from collections.abc import Mapping
from aiohttp import web
from orchestrator.command_interactions import InteractionError, VERSION, validate_operation
from orchestrator.session_store import SessionStore


async def handle_command_interaction(api, request, payload):
    def error(code, status):
        return web.json_response(InteractionError(code, status).result(), status=status,
                                 headers={"Cache-Control": "no-store"})
    if not api._check_admin_auth(request):
        return error("command_menu_forbidden", 403)
    # Ordinary enterprise identities must not become the configured personal
    # Telegram owner. Governed-profile binding is intentionally not admitted v1.
    if api._is_governed_profile():
        return error("command_menu_governed_not_supported", 501)
    if not api.admin_token:
        return error("command_menu_admin_token_required", 403)
    ui = payload.get("command_ui")
    if not isinstance(ui, Mapping):
        return error("command_menu_request_invalid", 400)
    value = {key: ui.get(key) for key in (
        "version", "op", "request_id", "client_id", "ui_locale", "menu_id", "revision", "button_id")}
    value["command"] = payload.get("command", "")
    try:
        validate_operation(value)
    except InteractionError as exc:
        return error(exc.code, exc.status)
    name = request.match_info.get("name")
    runtime = api._runtime_map().get(name)
    if runtime is None:
        return error("command_menu_agent_unavailable", 404)
    actor = getattr(api.global_config, "authorized_id", None)
    if type(actor) is not int:
        return error("command_menu_actor_unavailable", 403)
    # The connection binding is a presentation fence, NOT an authorization
    # claim. It is supplied by the authenticated gateway, never used as a role.
    connection = ui.get("connection_binding")
    if not isinstance(connection, str) or not 16 <= len(connection) <= 256:
        return error("command_menu_binding_missing", 400)
    metadata = {
        "actor_id": actor,
        "instance_id": str(getattr(api.global_config, "instance_id", "")),
        "session_surface": "workbench", "session_channel_key": "default",
        "connection_binding": connection,
    }
    if value["op"] != "catalogue":
        # Opening a picker must not create a Conversation Session. Writes bind
        # to the same canonical workbench/default Session as the chat surface.
        try:
            owner = SessionStore.owner_id_for(api.global_config)
            session = api.session_store.resolve_session(
                owner_id=owner, agent_id=name, surface="workbench", channel_key="default",
            )
            metadata.update(owner_id=owner, session_id=session["session_id"],
                            context_generation=int(session["context_generation"]))
        except Exception:
            return error("command_menu_session_unavailable", 503)
    try:
        if getattr(runtime, "is_function_worker_proxy", False):
            handler = getattr(runtime, "execute_command_interaction", None)
            if not callable(handler):
                return error("command_menu_worker_upgrade_required", 501)
            result = await handler(value, metadata)
        else:
            from orchestrator.command_interaction_bridge import dispatch_command_interaction
            result = await dispatch_command_interaction(runtime, value, metadata)
    except Exception:
        # No automatic replay after a broken IPC hop: execution may have begun.
        return error("command_menu_outcome_unknown", 502)
    if not isinstance(result, Mapping):
        return error("command_menu_response_invalid", 502)
    try:
        status = 200 if result.get("ok") else int(result.get("http_status") or 409)
    except (TypeError, ValueError):
        status = 502
    if status not in {200, 400, 403, 404, 409, 413, 422, 429, 500, 501, 502, 503}:
        status = 409
    return web.json_response({**result, "agent": name}, status=status,
                             headers={"Cache-Control": "no-store"})
