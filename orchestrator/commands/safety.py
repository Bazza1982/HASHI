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
from orchestrator.permissions.frontend_native import native_command


class SafetyInputError(ValueError):
    """Locally proved syntax error, without protected-object information."""


def _failure(runtime, error, *, phase, mutation_started=False):
    from dataclasses import replace
    import logging
    from orchestrator.operation_failures import OperationFailed, operation_failure
    from orchestrator.permissions.authority import PermissionErrorBase, RevisionConflict, OperationConflict
    from orchestrator.permissions.enterprise_decisions import SessionDecisionDenied
    from orchestrator.permissions.wire import PermissionRemoteError

    if isinstance(error, OperationFailed):
        return error.failure
    rejected = isinstance(error, (PermissionErrorBase, SessionDecisionDenied, SafetyInputError))
    failure = operation_failure(
        error, operation="safety", phase=phase, unconfirmed=mutation_started and not rejected,
    )
    reason = failure.reason
    if isinstance(error, SafetyInputError) or (phase == "validate" and isinstance(error, (ValueError, TypeError, KeyError, AttributeError))):
        reason = "invalid_input"
    elif isinstance(error, (RevisionConflict, OperationConflict)):
        reason = "state_changed"
    elif isinstance(error, (PermissionErrorBase, SessionDecisionDenied)):
        reason = "scope_unavailable"
    elif phase == "presentation" and not mutation_started and isinstance(error, PermissionRemoteError):
        # A read-only menu can report a typed service refusal. The same remote
        # error after a mutation must still leave its outcome unconfirmed.
        # Never render arbitrary remote codes or interpret them as local I/O.
        reason = {
            "SessionDecisionDenied": "scope_unavailable",
            "PermissionDenied": "scope_unavailable",
            "InvalidDecision": "scope_unavailable",
            "StaleFencingToken": "scope_unavailable",
            "RevisionConflict": "state_changed",
            "OperationConflict": "state_changed",
        }.get(error.code, "service_unavailable")
    elif phase == "scope" and isinstance(error, (ValueError, TypeError, KeyError, AttributeError)):
        reason = "scope_unavailable"
    elif phase == "configuration" and isinstance(error, (ValueError, TypeError, KeyError, AttributeError)):
        reason = "configuration_invalid"
    failure = replace(failure, reason=reason, error_code=f"safety_{phase}_{reason}")
    logger = getattr(runtime, "error_logger", None) or getattr(runtime, "logger", None)
    write = getattr(logger, "error", None)
    if not callable(write):
        write = logging.getLogger("HASHI.Safety").error
    write("Permission command failure: %s", failure.diagnostic())
    return failure


def _failure_text(key, failure):
    return ui_language.tr("safety.control_unconfirmed" if failure.unconfirmed else key) + "\n" + failure.render()


def _control_failure(operation, failure, text):
    state = "unconfirmed" if failure.unconfirmed else "rejected"
    return {
        "ok": False, "error_code": f"permission_{operation}_{state}",
        "error_message": text, "request_outcome": state,
        "diagnostic_reference": failure.reference, "failure_phase": failure.phase,
    }


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
    from orchestrator.permissions.presentation import request_detail_card, request_list_card, request_prompt_card
    from orchestrator.operation_failures import OperationFailed

    phase = "validate"
    try:
        action = args[0].casefold()
        if action == "withdraw" and len(args) == 4 and args[2] == "--revision":
            raw_revision = args[3]
            if (not raw_revision.isascii() or not raw_revision.isdecimal()
                    or len(raw_revision) > 9 or int(raw_revision) < 1):
                raise SafetyInputError("invalid permission request version")
            revision, before = int(raw_revision), None
        elif action in {"request", "prompt"} and len(args) == 2:
            revision, before = None, None
        elif action == "requests":
            if len(args) == 1:
                before = None
            elif len(args) == 3 and args[1].casefold() == "before":
                before = args[2]
            else:
                raise SafetyInputError("invalid permission request page")
            revision = None
        else:
            raise SafetyInputError("invalid permission view")
        phase = "scope"
        authority, subject = _subject_for_update(runtime, update)
        if action == "withdraw":
            phase = "withdraw"
            return request_detail_card(authority.withdraw_session_request(subject, args[1], revision))
        phase = "read"
        if action in {"request", "prompt"}:
            render = request_prompt_card if action == "prompt" else request_detail_card
            return render(authority.get_session_request(subject, args[1]))
        return request_list_card(authority.list_session_requests(subject, before=before, limit=10))
    except Exception as exc:
        raise OperationFailed(_failure(runtime, exc, phase=phase, mutation_started=phase == "withdraw")) from None


def _decision_markup(runtime, update, request_id, *, compact=False):
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    from orchestrator.permissions.frontend_control import current_permission_ingress

    authority, subject = _subject_for_update(runtime, update)
    view = authority.get_session_request(subject, request_id)
    if view.request.state != "pending" or view.task_state not in {"active", "paused"} or view.binding_state != "active":
        return None, ""
    actions = current_permission_ingress().issue_actions(
        subject, request_id, view.request.revision, presentation_id=str(update.update_id),
    )
    if compact:
        actions = tuple(sorted(
            (action for action in actions if action.lifetime != "run"),
            key=lambda action: ({"once": 0, "task": 1, "ttl": 2}.get(action.lifetime, 3)
                                if action.choice == "approve" else 4, action.duration_seconds),
        ))
    rows = []
    for action in actions:
        label = ui_language.tr("safety.choose_" + (action.lifetime if action.choice == "approve" else "deny"))
        if action.choice == "approve" and action.lifetime == "ttl":
            seconds = action.duration_seconds
            divisor, unit = (3600, "hours") if seconds % 3600 == 0 else (
                (60, "minutes") if seconds % 60 == 0 else (1, "seconds")
            )
            label = ui_language.tr("safety.approve_" + unit, value=seconds // divisor)
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
    phase, mutation_started = "validate", False
    try:
        match = re.fullmatch(r"safety:d:([a-f0-9]{32})", query.data)
        if match is None:
            raise SafetyInputError("invalid permission action")
        phase = "scope"
        _, subject = _subject_for_update(runtime, update)
        ingress = current_permission_ingress()
        phase, mutation_started = "decision", True
        outcome = await asyncio.to_thread(ingress.submit_action, subject, match[1])
        phase = "presentation"
        text = _outcome_text(outcome)
    except Exception as exc:
        failure = _failure(runtime, exc, phase=phase, mutation_started=mutation_started)
        text = _failure_text("safety.decision_rejected", failure)
        await runtime._reply_text(update, text)
        return _control_failure("decision", failure, text)
    await runtime._reply_text(update, text)
    return {"ok": True, "request_id": outcome.request_id, "choice": outcome.choice}


async def safety_command(runtime: Any, update: Any, context: Any):
    if not _authorized(runtime, update):
        return
    args = [str(item).strip() for item in (getattr(context, "args", None) or [])]
    if args and args[0].casefold() in {"narrow", "revoke", "revoke-task", "set-level"}:
        if getattr(runtime, "permission_authority", None) is None:
            await runtime._reply_text(update, ui_language.tr("safety.action_unavailable"))
            return {"ok": False, "error_code": "permission_service_unavailable"}
        from orchestrator.permissions.frontend_control import current_permission_ingress
        from orchestrator.permissions.presentation import management_card

        phase, mutation_started = "scope", False
        try:
            _, subject = _subject_for_update(runtime, update)
            ingress = current_permission_ingress()
            phase, mutation_started = "management", True
            outcome = await ingress.manage(subject, args, invocation_id=str(update.update_id))
            phase = "presentation"
            text = management_card(outcome)
        except Exception as exc:
            failure = _failure(runtime, exc, phase=phase, mutation_started=mutation_started)
            text = _failure_text("safety.management_failed", failure)
            await runtime._reply_text(update, text)
            return _control_failure("management", failure, text)
        await runtime._reply_text(update, text, parse_mode="HTML")
        return {"ok": True}
    if args and args[0].casefold() in {"tasks", "leases"}:
        from orchestrator.permissions.presentation import session_summary_card

        phase = "scope"
        try:
            authority, subject = _subject_for_update(runtime, update)
            phase = "read"
            summary = await asyncio.to_thread(authority.session_summary, subject=subject)
            phase = "presentation"
            text = session_summary_card(summary)
        except Exception as exc:
            failure = _failure(runtime, exc, phase=phase)
            await runtime._reply_text(update, _failure_text("safety.requests_unavailable", failure))
            return
        await runtime._reply_text(update, text, parse_mode="HTML")
        return
    if args and args[0].casefold() in {"approve", "deny"}:
        from orchestrator.permissions.frontend_control import current_permission_ingress

        phase, mutation_started = "scope", False
        try:
            _, subject = _subject_for_update(runtime, update)
            ingress = current_permission_ingress()
            phase, mutation_started = "decision", True
            outcome = await asyncio.to_thread(
                ingress.decide, subject, args, invocation_id=str(update.update_id),
            )
            phase = "presentation"
            text = _outcome_text(outcome)
        except Exception as exc:
            failure = _failure(runtime, exc, phase=phase, mutation_started=mutation_started)
            text = _failure_text("safety.decision_rejected", failure)
            await runtime._reply_text(update, text)
            return _control_failure("decision", failure, text)
        await runtime._reply_text(update, text)
        return {"ok": True, "request_id": outcome.request_id, "choice": outcome.choice,
                "replayed": outcome.replayed}
    if args and args[0].casefold() in {"requests", "request", "prompt", "withdraw"}:
        try:
            text = await asyncio.to_thread(_request_command, runtime, update, args)
        except Exception as exc:
            withdrawing = args[0].casefold() == "withdraw"
            failure = _failure(runtime, exc, phase="withdraw" if withdrawing else "read",
                               mutation_started=withdrawing)
            text = _failure_text("safety.requests_unavailable", failure)
            await runtime._reply_text(update, text)
            if withdrawing:
                return _control_failure("withdraw", failure, text)
            return
        markup = None
        if args[0].casefold() in {"request", "prompt"} and len(args) == 2:
            try:
                markup, duration = await asyncio.to_thread(
                    _decision_markup, runtime, update, args[1], compact=args[0].casefold() == "prompt",
                )
                if markup is not None:
                    text += "\n\n" + escape(duration)
            except Exception as exc:
                failure = _failure(runtime, exc, phase="presentation")
                text += "\n\n" + escape(_failure_text("safety.requests_unavailable", failure))
        await runtime._reply_text(update, text, parse_mode="HTML",
                                  **({"reply_markup": markup} if markup is not None else {}))
        return
    if args and [item.casefold() for item in args] != ["status"]:
        await runtime._reply_text(update, ui_language.tr("safety.action_unavailable"))
        return
    try:
        level, control_text = await asyncio.to_thread(_read_status, runtime)
    except Exception as exc:
        failure = _failure(runtime, exc, phase="configuration")
        await runtime._reply_text(update, _failure_text("safety.config_unavailable", failure))
        return
    text = "\n".join((
        card_title("🛡️", ui_language.tr("safety.title")),
        "",
        f"<b>{escape(ui_language.tr('safety.configured'))}</b> · <code>{escape(level)}</code>",
        escape(control_text),
    ))
    await runtime._reply_text(update, text, parse_mode="HTML")


COMMANDS = [RuntimeCommand("safety", "View permission status", native_command(safety_command))]
CALLBACKS = [RuntimeCallback(r"^safety:d:[a-f0-9]{32}$", native_command(safety_decision_callback))]
# HASHI-Fingerprint: hfp_084f0dbfcdb3436faa9857c381a2ef7a
