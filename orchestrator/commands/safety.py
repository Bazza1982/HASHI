"""Permission status and non-granting controls through the shared registry.

Permission decisions, policy changes and actual-control observations require a
separate trusted service. This entry does not construct one or issue authority.
"""
from __future__ import annotations

import asyncio
from html import escape
from typing import Any

from orchestrator import ui_language
from orchestrator.command_registry import RuntimeCallback, RuntimeCommand
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


def _subject_for_update(runtime: Any, update: Any):
    from orchestrator import runtime_session
    from orchestrator.permissions.frontend_control import PermissionViewPort
    from orchestrator.permissions.models import PermissionSubject
    from orchestrator.session_store import SessionStore

    # Use the existing owner. Viewing must never open a second authority,
    # create a ledger, fence an executor or initialize a missing Session store.
    authority = getattr(runtime, "permission_authority", None)
    store = getattr(runtime, "session_store", None)
    if not isinstance(authority, PermissionViewPort) or not isinstance(store, SessionStore):
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
    return authority, PermissionSubject(
        store.instance_id, session["owner_id"], session["agent_id"],
        session["session_id"], str(generation),
    )


def _request_command(runtime: Any, update: Any, args: list[str]) -> str:
    from orchestrator.permissions.presentation import request_detail_card, request_list_card

    authority, subject = _subject_for_update(runtime, update)
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


def _decision_markup(runtime, update, request_id):
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    from orchestrator.permissions.frontend_control import current_permission_ingress

    authority, subject = _subject_for_update(runtime, update)
    view = authority.get_session_request(subject, request_id)
    if view.request.state != "pending" or view.task_state not in {"active", "paused"} or view.binding_state != "active":
        return None, ""
    actions = current_permission_ingress().issue_actions(
        subject, request_id, view.request.revision, presentation_id=str(update.update_id),
    )
    rows = []
    for action in actions:
        label = ui_language.tr("safety.choose_" + (action.lifetime if action.choice == "approve" else "deny"))
        rows.append([InlineKeyboardButton(label, callback_data="safety:d:" + action.action_id)])
    seconds = actions[0].duration_seconds
    divisor, unit = (3600, "hours") if seconds % 3600 == 0 else (
        (60, "minutes") if seconds % 60 == 0 else (1, "seconds")
    )
    duration = ui_language.tr("safety.maximum_" + unit, value=seconds // divisor)
    return InlineKeyboardMarkup(rows), duration


def _outcome_text(outcome):
    from datetime import datetime

    if outcome.choice == "deny":
        return ui_language.tr("safety.decision_denied")
    expiry = datetime.fromtimestamp(outcome.lease.expires_at).astimezone().isoformat(timespec="seconds")
    return ui_language.tr("safety.decision_approved", expiry=expiry)


async def safety_decision_callback(runtime, update, context):
    import re
    from orchestrator.permissions.frontend_control import current_permission_ingress

    if not _authorized(runtime, update):
        return
    query = update.callback_query
    await query.answer()
    try:
        match = re.fullmatch(r"safety:d:([a-f0-9]{32})", query.data)
        if match is None:
            raise ValueError("invalid permission action")
        _, subject = _subject_for_update(runtime, update)
        outcome = await asyncio.to_thread(
            current_permission_ingress().submit_action, subject, match[1],
        )
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        await runtime._reply_text(update, ui_language.tr("safety.decision_rejected"))
        return {"ok": False, "error_code": "permission_decision_rejected"}
    await runtime._reply_text(update, _outcome_text(outcome))
    return {"ok": True, "request_id": outcome.request_id, "choice": outcome.choice}


async def safety_command(runtime: Any, update: Any, context: Any):
    if not _authorized(runtime, update):
        return
    args = [str(item).strip() for item in (getattr(context, "args", None) or [])]
    if args and args[0].casefold() in {"approve", "deny"}:
        from orchestrator.permissions.frontend_control import current_permission_ingress

        try:
            _, subject = _subject_for_update(runtime, update)
            ingress = current_permission_ingress()
            outcome = await asyncio.to_thread(
                ingress.decide, subject, args, invocation_id=str(update.update_id),
            )
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            text = ui_language.tr("safety.decision_rejected")
            await runtime._reply_text(update, text)
            return {"ok": False, "error_code": "permission_decision_rejected",
                    "error_message": text, "request_outcome": "rejected"}
        await runtime._reply_text(update, _outcome_text(outcome))
        return {"ok": True, "request_id": outcome.request_id, "choice": outcome.choice,
                "replayed": outcome.replayed}
    if args and args[0].casefold() in {"requests", "request", "withdraw"}:
        try:
            text = await asyncio.to_thread(_request_command, runtime, update, args)
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            await runtime._reply_text(update, ui_language.tr("safety.requests_unavailable"))
            return
        markup = None
        if args[0].casefold() == "request" and len(args) == 2:
            try:
                markup, duration = await asyncio.to_thread(_decision_markup, runtime, update, args[1])
                if markup is not None:
                    text += "\n\n" + escape(duration)
            except (OSError, ValueError, TypeError, KeyError, AttributeError):
                pass
        await runtime._reply_text(update, text, parse_mode="HTML",
                                  **({"reply_markup": markup} if markup is not None else {}))
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
CALLBACKS = [RuntimeCallback(r"^safety:d:[a-f0-9]{32}$", safety_decision_callback)]
