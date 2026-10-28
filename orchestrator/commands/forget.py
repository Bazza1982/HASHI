"""Owner-confirmed removal of a bounded interval from Agent PCM."""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from typing import Any

from orchestrator import pcm_forget_coordinator, runtime_session, ui_language
from orchestrator.command_registry import RuntimeCommand
from orchestrator.session_store import SessionConflict

logger = logging.getLogger("HASHI.PCMForget")
_DURATION = re.compile(r"^([1-9][0-9]{0,3})(h|d)$", re.IGNORECASE)


def _authorized(runtime: Any, update: Any) -> bool:
    checker = getattr(runtime, "_is_authorized_user", None)
    actor = getattr(getattr(update, "effective_user", None), "id", None)
    if not callable(checker) or not checker(actor):
        return False
    # Compatibility transports synthesize a Telegram-like actor; only the
    # authenticated Backend API may assert this internal marker.
    from orchestrator.admin_local_testing import _FakeUpdate

    if isinstance(update, _FakeUpdate):
        return getattr(update, "_hashi_forget_authenticated", False) is True
    return True


def _duration(value: str) -> timedelta:
    match = _DURATION.fullmatch(value.strip())
    if not match:
        raise ValueError("invalid duration")
    number = int(match.group(1))
    result = timedelta(hours=number) if match.group(2).lower() == "h" else timedelta(days=number)
    if result > timedelta(days=3650):
        raise ValueError("duration exceeds ten years")
    return result


async def forget_command(runtime: Any, update: Any, context: Any) -> dict[str, Any] | None:
    if not _authorized(runtime, update):
        await runtime._reply_text(update, ui_language.tr("forget.unauthorized"))
        return None
    args = [str(item).strip() for item in (getattr(context, "args", None) or [])]
    if runtime_session.runtime_busy(runtime) or pcm_forget_coordinator.background_work_active(runtime):
        await runtime._reply_text(update, ui_language.tr("forget.busy"))
        return None
    from orchestrator.pcm_forget_audit import audit_scope

    sink = getattr(runtime, "canonical_audit", None)
    if (sink is None or not callable(getattr(sink, "record", None))
            or str(getattr(sink, "agent_id", "")).casefold() != str(runtime.name).casefold()
            or str(getattr(sink, "instance_id", "")).casefold()
               != str(getattr(runtime.global_config, "instance_id", "")).casefold()):
        await runtime._reply_text(update, ui_language.tr("forget.audit_unavailable"))
        return None
    store = runtime_session.ensure_store(runtime)
    session = runtime_session.current_session_for_update(runtime, update)
    owner = str(session["owner_id"])
    agent = str(runtime.name).lower()

    if len(args) == 1 and args[0].lower() == "resume":
        if store.pending_pcm_forget(owner_id=owner, agent_id=agent) is None:
            await runtime._reply_text(update, ui_language.tr("forget.no_pending"))
            return None
        try:
            with audit_scope(sink):
                result = await pcm_forget_coordinator.finish_pending(runtime, update, owner_id=owner)
        except Exception as exc:
            logger.warning("PCM forget resume did not complete: %s", type(exc).__name__)
            await runtime._reply_text(update, ui_language.tr("forget.incomplete"))
            return None
        await runtime._reply_text(
            update, ui_language.tr("forget.completed", session_id=result["new_session_id"]),
        )
        return result

    if len(args) == 2 and args[0].lower() == "confirm":
        try:
            with audit_scope(sink):
                confirmed = store.confirm_pcm_forget(
                    owner_id=owner, agent_id=agent, session_id=str(session["session_id"]), token=args[1],
                )
        except (SessionConflict, ValueError) as exc:
            detail = str(exc).casefold()
            if "another owner" in detail:
                key = "forget.shared_owner"
            elif any(word in detail for word in ("active", "queued", "phone", "unfinished command")):
                key = "forget.busy"
            else:
                key = "forget.changed"
            await runtime._reply_text(update, ui_language.tr(key))
            return None
        if store.pending_pcm_forget(owner_id=owner, agent_id=agent) is None:
            # A previously completed confirmation can be delivered twice.
            new_session = confirmed.get("new_session_id")
            if not new_session:
                await runtime._reply_text(update, ui_language.tr("forget.incomplete"))
                return None
            result = {"new_session_id": new_session, "replayed": True}
        else:
            try:
                with audit_scope(sink):
                    result = await pcm_forget_coordinator.finish_pending(runtime, update, owner_id=owner)
            except Exception as exc:
                logger.warning("PCM forget did not complete: %s", type(exc).__name__)
                await runtime._reply_text(update, ui_language.tr("forget.incomplete"))
                return None
        await runtime._reply_text(
            update, ui_language.tr("forget.completed", session_id=result["new_session_id"]),
        )
        return result

    if len(args) != 1:
        await runtime._reply_text(update, ui_language.tr("forget.usage"))
        return None
    try:
        duration = _duration(args[0])
        preview = store.preview_pcm_forget(
            owner_id=owner, agent_id=agent, session_id=str(session["session_id"]),
            duration=duration,
        )
    except ValueError:
        await runtime._reply_text(update, ui_language.tr("forget.usage"))
        return None
    except SessionConflict:
        await runtime._reply_text(update, ui_language.tr("forget.changed"))
        return None
    start = datetime.fromisoformat(preview["start"]).astimezone().strftime("%Y-%m-%d %H:%M %Z")
    end = datetime.fromisoformat(preview["end"]).astimezone().strftime("%Y-%m-%d %H:%M %Z")
    await runtime._reply_text(
        update, ui_language.tr("forget.preview", agent=runtime.name, start=start, end=end,
                               token=preview["token"]),
    )
    return {"previewed": True, "expires": preview["expires"]}


COMMANDS = [RuntimeCommand("forget", "Forget a bounded period of Agent memory", forget_command)]
CALLBACKS = []
