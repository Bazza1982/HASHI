"""Original-request binding for existing Backend API command entrypoints.

These helpers run in the trusted FC process. Tokens never enter public command
metadata or generic Worker RPC. Absent service configuration keeps the original
path; a Worker without private ingress cannot manufacture an approval.
"""
from __future__ import annotations

import hashlib
from uuid import uuid4

from orchestrator.permissions.frontend_control import (
    PermissionCommandProjection, PermissionIngress, bind_permission_ingress,
)
from orchestrator.permissions.models import PermissionSubject


def _projection(api, runtime):
    control = getattr(api, "permission_control", None)
    if control is None:
        return None
    projections = getattr(api, "_permission_command_projections", None)
    if projections is None:
        projections = {}
        api._permission_command_projections = projections
    existing = projections.get(runtime.name)
    key = (id(runtime), id(control), id(api.session_store))
    if existing is None or existing[0] != key:
        existing = (key, PermissionCommandProjection(
            runtime, session_store=api.session_store, views=control,
        ))
        projections[runtime.name] = existing
    return existing[1]


def _is_permission_operation(projection, operation):
    if operation.get("op") == "open":
        from orchestrator.admin_local_testing import _split_command
        command, _ = _split_command(operation.get("command", ""))
        return command == "safety"
    store = getattr(projection, "_command_interaction_store", None)
    return store is not None and operation.get("menu_id") in store.menus


async def dispatch_permission_operation(api, request, runtime, operation, metadata):
    from orchestrator.command_interaction_bridge import dispatch_command_interaction

    projection = _projection(api, runtime)
    if projection is None or not _is_permission_operation(projection, operation):
        return None
    session = api.session_store.get_session(
        metadata["session_id"], owner_id=metadata["owner_id"], include_deleted=False,
    )
    if (session["agent_id"] != runtime.name or session["status"] != "active"
            or type(metadata["context_generation"]) is not int
            or session["context_generation"] != metadata["context_generation"]
            or api.session_store.instance_id != str(metadata["instance_id"]).strip().upper()):
        raise ValueError("permission command Session binding changed")
    subject = PermissionSubject(
        api.session_store.instance_id, session["owner_id"], runtime.name,
        session["session_id"], str(session["context_generation"]),
    )
    from orchestrator.permissions.frontend_auth import session_token

    token = session_token(request)
    temporary = False
    # Personal L1 assumes the frontend action is a user gesture. The transport
    # remains authenticated; governed deployments and L2 keep separate auth.
    if (not request.headers.get("X-Workbench-Session", "").strip()
            and not token.startswith("hs_sess_") and not api._is_governed_profile()
            and api._strict_authenticated_owner(request) == subject.owner_id
            and callable(getattr(api.permission_control, "personal_l1_login", None))):
        import asyncio

        try:
            login = await asyncio.to_thread(api.permission_control.personal_l1_login, subject=subject)
            token, temporary = login["session_token"], True
        except (OSError, ValueError, TypeError, KeyError):
            pass
    ingress = PermissionIngress(
        subject, api.permission_control, token, metadata["connection_binding"],
    )
    try:
        with bind_permission_ingress(ingress):
            return await dispatch_command_interaction(projection, operation, metadata)
    finally:
        if temporary:
            await asyncio.to_thread(api.permission_control.logout, session_token=token)


async def _try_permission_command(api, request, runtime, text, *, owner_id):
    """Recognize shared textual and encoded commands before forwarding to a Worker."""
    from orchestrator import runtime_session
    from orchestrator.admin_local_testing import _split_command
    from orchestrator.command_interaction_transport import TRANSPORT_PREFIX, _decode_transport

    projection = _projection(api, runtime)
    if projection is None:
        return None
    if text.startswith(TRANSPORT_PREFIX):
        operation = _decode_transport(text)
        if not _is_permission_operation(projection, operation):
            return None
        selected = operation.pop("session_id", None)
        expected_generation = operation.pop("context_generation", None)
        operation.pop("connection_binding", None)
    else:
        command, _ = _split_command(text)
        if command != "safety":
            return None
        selected, expected_generation = None, None
        operation = {
            "version": 1, "op": "open", "command": text,
            "client_id": "backend-permission-command",
            "request_id": "permission_" + uuid4().hex,
        }
    session = runtime_session.current_session(
        projection, surface="workbench", channel_key="default",
        explicit_owner_id=owner_id,
        **({"explicit_session_id": selected} if selected else {}),
    )
    if expected_generation is not None and expected_generation != session["context_generation"]:
        raise ValueError("permission command Session changed")
    connection = hashlib.sha256(
        f"permission-fc-v1\0{api.session_store.instance_id}\0{owner_id}\0{operation['client_id']}".encode(),
    ).hexdigest()
    metadata = {
        "actor_id": api.global_config.authorized_id,
        "instance_id": api.session_store.instance_id, "session_surface": "workbench",
        "session_channel_key": "default", "connection_binding": connection,
        "owner_id": owner_id, "session_id": session["session_id"],
        "context_generation": session["context_generation"], "connector_id": "backend_api",
        "ingress_transport": "workbench-command-ui", "source_channel": "workbench_api",
        "_durable_command_invocation": True,
    }
    return await dispatch_permission_operation(api, request, runtime, operation, metadata)

async def try_permission_command(api, request, runtime, text, *, owner_id):
    from orchestrator.command_interactions import InteractionError

    try:
        return await _try_permission_command(api, request, runtime, text, owner_id=owner_id)
    except InteractionError as exc:
        return exc.result()
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return {"ok": False, "error_code": "permission_command_unavailable",
                "request_outcome": "unknown"}
