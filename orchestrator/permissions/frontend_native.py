"""Shared original-command FC ingress; synthetic API actors never log in."""
from __future__ import annotations

import asyncio
from functools import wraps
from hashlib import sha256

from orchestrator.permissions.authority import PermissionDenied


def original_subject(runtime, update, context):
    # Compatibility/API command contexts carry their Connector ID. Their actor
    # is presentation data and cannot become a human login. The existing shared
    # resolver independently requires an original native transport Update.
    if getattr(context, "frontend_connector_id", None) is not None:
        raise PermissionDenied("synthetic command actors do not authenticate humans")
    checker = getattr(runtime, "_is_authorized_user", None)
    actor = getattr(getattr(update, "effective_user", None), "id", None)
    if type(actor) is not int or not callable(checker) or not checker(actor):
        raise PermissionDenied("the original command user is not authenticated")
    from orchestrator.command_request_context import command_request_context
    from orchestrator.permissions.models import PermissionSubject

    command = command_request_context(runtime, update, context, purpose="permission-control")
    meta = command.metadata
    return (PermissionSubject(runtime.session_store.instance_id, meta["owner_id"], runtime.name,
                              meta["session_id"], str(meta["session_context_generation"])),
            meta["ingress_transport"], str(actor))


def native_command(callback):
    @wraps(callback)
    async def invoke(runtime, update, context):
        from orchestrator.permissions.frontend_control import (
            PermissionIngress, _CURRENT, bind_permission_ingress,
        )

        control = getattr(runtime, "_permission_native_control", None)
        if _CURRENT.get() is not None or control is None:
            return await callback(runtime, update, context)
        token = None
        try:
            subject, connector, actor = original_subject(runtime, update, context)
            result = await asyncio.to_thread(control.native_login, subject=subject, connector_id=connector,
                                              actor_id=actor, invocation_id=str(update.update_id))
            token = result["session_token"]
            connection = sha256((subject.instance_id + ":" + connector + ":" + actor).encode()).hexdigest()
            ingress = PermissionIngress(subject, control, token, connection)
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            # The ordinary command still reports unavailability; it cannot fall
            # through to a model answer or treat a view as an approval.
            return await callback(runtime, update, context)
        try:
            with bind_permission_ingress(ingress):
                return await callback(runtime, update, context)
        finally:
            if token:
                try:
                    await asyncio.to_thread(control.logout, session_token=token)
                except OSError:
                    # The service-minted native login also has a fixed 60s TTL.
                    runtime.error_logger.warning("Short-lived native permission session retirement is unverified")
    return invoke
