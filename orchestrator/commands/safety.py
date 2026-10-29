"""Permission status and non-granting controls through the shared registry.

Permission decisions, policy changes and actual-control observations require a
separate trusted service. This entry does not construct one or issue authority.
"""
from __future__ import annotations

import asyncio
from html import escape
from typing import Any

from orchestrator import ui_language
from orchestrator.command_registry import RuntimeCommand
from orchestrator.command_ui import card_title


def _authorized(runtime: Any, update: Any) -> bool:
    checker = getattr(runtime, "_is_authorized_user", None)
    actor = getattr(getattr(update, "effective_user", None), "id", None)
    return bool(callable(checker) and checker(actor))


def _read_level(runtime: Any) -> str:
    # Only the existing configuration owner interprets and reads this setting.
    from orchestrator.config_admin import ConfigAdmin

    config = getattr(runtime, "global_config", None)
    if config is None or getattr(config, "config_path", None) is None:
        raise ValueError("agent configuration is unavailable")
    snapshot = ConfigAdmin(config).read_agent_permission_settings(runtime.name)
    return snapshot.settings.requested_level.value


def _read_status(runtime: Any) -> tuple[str, str]:
    level = _read_level(runtime)
    getter = getattr(getattr(runtime, "backend_manager", None), "permission_status", None)
    status = getter() if callable(getter) else None
    if status is not None:
        from orchestrator.permissions.backend_capabilities import BackendPermissionStatus
        from orchestrator.permissions.presentation import backend_permission_notice

        # Disk configuration and active Function state may not yet agree.
        # Such a mismatch is unknown, never a report of enforcement.
        if isinstance(status, BackendPermissionStatus) and status.requested_level.value == level:
            return level, backend_permission_notice(status)
    return level, ui_language.tr("safety.control_unknown")


def _request_command(runtime: Any, update: Any, args: list[str]) -> str:
    from orchestrator import runtime_session
    from orchestrator.permissions.authority import PermissionAuthority
    from orchestrator.permissions.models import PermissionSubject
    from orchestrator.permissions.presentation import request_detail_card, request_list_card
    from orchestrator.session_store import SessionStore

    # Use the existing owner. Viewing must never open a second authority,
    # create a ledger, fence an executor or initialize a missing Session store.
    authority = getattr(runtime, "permission_authority", None)
    store = getattr(runtime, "session_store", None)
    if not isinstance(authority, PermissionAuthority) or not isinstance(store, SessionStore):
        raise ValueError("permission service is unavailable")
    session = runtime_session.current_session_for_update(runtime, update)
    generation = session["context_generation"]
    requested_generation = getattr(update, "_hashi_session_context_generation", None)
    if requested_generation is not None and (
        type(requested_generation) is not int or requested_generation != generation
    ):
        raise ValueError("session changed")
    if (session["agent_id"] != runtime.name or session["status"] != "active"
            or store.instance_id != str(runtime.global_config.instance_id).strip().upper()):
        raise ValueError("session binding is unavailable")
    subject = PermissionSubject(
        store.instance_id, session["owner_id"], session["agent_id"],
        session["session_id"], str(generation),
    )
    if args[0].casefold() == "withdraw" and len(args) == 4 and args[2] == "--revision":
        raw_revision = args[3]
        if (not raw_revision.isascii() or not raw_revision.isdecimal()
                or len(raw_revision) > 9 or int(raw_revision) < 1):
            raise ValueError("invalid permission request version")
        return request_detail_card(
            authority.withdraw_session_request(subject, args[1], int(raw_revision)),
        )
    if args[0].casefold() == "request" and len(args) == 2:
        return request_detail_card(authority.get_session_request(subject, args[1]))
    if args[0].casefold() == "requests":
        if len(args) == 1:
            before = None
        elif len(args) == 3 and args[1].casefold() == "before":
            before = args[2]
        else:
            raise ValueError("invalid permission request page")
        return request_list_card(authority.list_session_requests(subject, before=before, limit=10))
    raise ValueError("invalid permission view")


async def safety_command(runtime: Any, update: Any, context: Any) -> None:
    if not _authorized(runtime, update):
        return
    args = [str(item).strip() for item in (getattr(context, "args", None) or [])]
    if args and args[0].casefold() in {"requests", "request", "withdraw"}:
        try:
            text = await asyncio.to_thread(_request_command, runtime, update, args)
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            await runtime._reply_text(update, ui_language.tr("safety.requests_unavailable"))
            return
        await runtime._reply_text(update, text, parse_mode="HTML")
        return
    if args and [item.casefold() for item in args] != ["status"]:
        await runtime._reply_text(update, ui_language.tr("safety.action_unavailable"))
        return
    try:
        level, control_text = await asyncio.to_thread(_read_status, runtime)
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        await runtime._reply_text(update, ui_language.tr("safety.config_unavailable"))
        return
    text = "\n".join((
        card_title("🛡️", ui_language.tr("safety.title")),
        "",
        f"<b>{escape(ui_language.tr('safety.configured'))}</b> · <code>{escape(level)}</code>",
        escape(control_text),
    ))
    await runtime._reply_text(update, text, parse_mode="HTML")


COMMANDS = [RuntimeCommand("safety", "View permission status", safety_command)]
CALLBACKS = []
