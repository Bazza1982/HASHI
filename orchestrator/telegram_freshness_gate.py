"""Persistent Telegram freshness gate and delayed-message confirmation UI.

The gate runs before commands, text handling, downloads, transcription, or Run
admission. Confirmed work is replayed from the immutable Telegram Update saved
by the server; incoming payloads cannot request a freshness bypass.
"""
from __future__ import annotations

import asyncio
from contextvars import ContextVar
from datetime import datetime, timezone
import hashlib
import json
import logging
from typing import Any, Mapping

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update

from orchestrator import ui_language
from orchestrator.ingress_confirmation import (
    CONFIRMATION_EXECUTION_LEASE_SECONDS,
    ConfirmationConflictError,
    ConfirmationExpiredError,
    ConfirmationNotFoundError,
    ConfirmationSupersededError,
    IngressConfirmationService,
)
from orchestrator.message_freshness import classify_freshness, parse_rfc3339_utc

logger = logging.getLogger("HASHI.TelegramFreshnessGate")
TELEGRAM_CONFIRM_CALLBACK_PREFIX = "ingress_confirm:"

# Only this module sets this value while replaying a persisted Update.
_CONFIRMED_RELEASE: ContextVar[dict[str, Any] | None] = ContextVar(
    "hashi_confirmed_telegram_release", default=None
)


def current_telegram_release() -> dict[str, Any] | None:
    value = _CONFIRMED_RELEASE.get()
    return dict(value) if isinstance(value, dict) else None


def format_delay_display(delay_seconds: float, *, locale: str = "zh-CN") -> str:
    seconds = max(0, int(round(float(delay_seconds or 0.0))))
    if seconds < 60:
        return ui_language.tr(
            "ingress_confirmation.delay_seconds", locale=locale, seconds=seconds
        )
    minutes, remainder = divmod(seconds, 60)
    if remainder:
        return ui_language.tr(
            "ingress_confirmation.delay_minutes_seconds",
            locale=locale,
            minutes=minutes,
            seconds=remainder,
        )
    return ui_language.tr(
        "ingress_confirmation.delay_minutes", locale=locale, minutes=minutes
    )


def _bot(runtime: Any) -> Any | None:
    return getattr(runtime, "bot", None) or getattr(
        getattr(runtime, "app", None), "bot", None
    )


def _message(update: Any) -> Any | None:
    return getattr(update, "effective_message", None) or getattr(update, "message", None)


def _chat_id(update: Any, message: Any | None = None) -> Any | None:
    value = getattr(getattr(update, "effective_chat", None), "id", None)
    if value is None:
        value = getattr(message or _message(update), "chat_id", None)
    return value


def _user_id(update: Any) -> Any | None:
    value = getattr(getattr(update, "effective_user", None), "id", None)
    if value is None:
        value = getattr(
            getattr(getattr(update, "callback_query", None), "from_user", None),
            "id",
            None,
        )
    return value


def _source_message_id(runtime: Any, update: Any, message: Any) -> str:
    endpoint = str(getattr(runtime, "name", None) or "telegram")
    update_id = getattr(update, "update_id", None)
    suffix = f":{update_id}" if update_id is not None else ""
    return (
        f"tg:{endpoint}:{_chat_id(update, message)}:"
        f"{getattr(message, 'message_id', None)}{suffix}"
    )


def _json_compatible_update(update: Any, message: Any) -> dict[str, Any]:
    converter = getattr(update, "to_dict", None)
    if callable(converter):
        return json.loads(
            json.dumps(converter(), ensure_ascii=False, default=str)
        )
    date = getattr(message, "date", None)
    date_value = int(date.timestamp()) if isinstance(date, datetime) else date
    raw_message: dict[str, Any] = {
        "message_id": getattr(message, "message_id", None),
        "date": date_value,
        "chat": {"id": _chat_id(update, message), "type": "private"},
        "from": {
            "id": _user_id(update),
            "is_bot": False,
            "first_name": "HASHI user",
        },
    }
    for field in ("text", "caption"):
        value = getattr(message, field, None)
        if value is not None:
            raw_message[field] = value
    return {
        "update_id": getattr(update, "update_id", 0) or 0,
        "message": raw_message,
    }


def _payload_digest(payload: Mapping[str, Any]) -> str:
    raw = json.dumps(
        payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _preview(
    message: Any,
    *,
    handler_kind: str,
    command_name: str | None,
    args: list[str] | None,
    media_kind: str | None,
    filename: str | None,
) -> str:
    if handler_kind == "command":
        return f"/{command_name or ''} {' '.join(args or [])}".strip()
    text = str(getattr(message, "text", None) or "").strip()
    caption = str(getattr(message, "caption", None) or "").strip()
    if text:
        return text[:240]
    return f"[{filename or media_kind or 'attachment'}] {caption[:180]}".strip()


def _reason_text(reason: str, *, locale: str) -> str:
    key = {
        "stale": "ingress_confirmation.reason_stale",
        "timestamp_missing": "ingress_confirmation.reason_timestamp_missing",
        "timestamp_invalid": "ingress_confirmation.reason_timestamp_invalid",
        "timestamp_in_future": "ingress_confirmation.reason_timestamp_future",
    }.get(str(reason), "ingress_confirmation.reason_invalid")
    return ui_language.tr(key, locale=locale)


def _card_text(record: Mapping[str, Any]) -> str:
    metadata = dict(record.get("display_metadata") or {})
    locale = str(metadata.get("locale") or ui_language.DEFAULT_LOCALE)
    originated = str(
        record.get("originated_at")
        or ui_language.tr("ingress_confirmation.time_unknown", locale=locale)
    )
    return ui_language.tr(
        "ingress_confirmation.card",
        locale=locale,
        delay=format_delay_display(record.get("delay_seconds") or 0.0, locale=locale),
        preview=str(metadata.get("preview") or "")[:240],
        originated_at=originated,
        received_at=str(record.get("received_at") or ""),
        expires_at=str(record.get("expires_at") or ""),
        reason=_reason_text(str(record.get("reason") or ""), locale=locale),
    )


def _keyboard(record: Mapping[str, Any]) -> InlineKeyboardMarkup:
    locale = str(
        (record.get("display_metadata") or {}).get("locale")
        or ui_language.DEFAULT_LOCALE
    )
    confirmation_id = str(record["confirmation_id"])
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton(
                ui_language.tr("ingress_confirmation.confirm", locale=locale),
                callback_data=(
                    f"{TELEGRAM_CONFIRM_CALLBACK_PREFIX}{confirmation_id}:confirm"
                ),
            ),
            InlineKeyboardButton(
                ui_language.tr("ingress_confirmation.discard", locale=locale),
                callback_data=(
                    f"{TELEGRAM_CONFIRM_CALLBACK_PREFIX}{confirmation_id}:discard"
                ),
            ),
        ]]
    )


def _terminal_text(record: Mapping[str, Any]) -> str:
    metadata = dict(record.get("display_metadata") or {})
    locale = str(metadata.get("locale") or ui_language.DEFAULT_LOCALE)
    key = {
        "confirming": "ingress_confirmation.state_confirming",
        "confirmed": "ingress_confirmation.state_confirmed",
        "discarded": "ingress_confirmation.state_discarded",
        "expired": "ingress_confirmation.state_expired",
        "superseded": "ingress_confirmation.state_superseded",
    }.get(str(record.get("state") or ""), "ingress_confirmation.state_pending")
    return ui_language.tr(
        key,
        locale=locale,
        preview=str(metadata.get("preview") or "")[:240],
        reason=_reason_text(str(record.get("reason") or ""), locale=locale),
    )


async def _edit_or_fallback(
    runtime: Any, record: Mapping[str, Any], text: str
) -> bool:
    metadata = dict(record.get("display_metadata") or {})
    chat_id = metadata.get("telegram_chat_id")
    card_message_id = metadata.get("telegram_card_message_id")
    bot = _bot(runtime)
    if bot is None or chat_id is None:
        return False
    if card_message_id is not None:
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=card_message_id,
                text=text,
                reply_markup=None,
            )
            return True
        except Exception as exc:
            logger.warning(
                "Telegram delayed-message card edit failed; using fallback: %s",
                type(exc).__name__,
            )
    try:
        await bot.send_message(chat_id=chat_id, text=text)
        return True
    except Exception as exc:
        logger.warning(
            "Telegram delayed-message terminal projection failed: %s",
            type(exc).__name__,
        )
        return False


async def _project_terminal(runtime: Any, record: dict[str, Any]) -> None:
    state = str(record.get("state") or "")
    if state not in {"confirmed", "discarded", "expired", "superseded"}:
        return
    metadata = dict(record.get("display_metadata") or {})
    if metadata.get("telegram_terminal_state") == state:
        return
    if not await _edit_or_fallback(runtime, record, _terminal_text(record)):
        return
    try:
        IngressConfirmationService(runtime.session_store).update_display_metadata(
            confirmation_id=str(record["confirmation_id"]),
            owner_id=str(record["owner_id"]),
            updates={
                "telegram_terminal_state": state,
                "telegram_terminal_projected_at": datetime.now(timezone.utc)
                .isoformat()
                .replace("+00:00", "Z"),
            },
        )
    except Exception as exc:
        logger.warning(
            "Could not persist delayed-message projection receipt: %s",
            type(exc).__name__,
        )


def _track_task(runtime: Any, confirmation_id: str, task: asyncio.Task) -> None:
    registry = getattr(runtime, "_telegram_confirmation_tasks", None)
    if not isinstance(registry, dict):
        registry = {}
        setattr(runtime, "_telegram_confirmation_tasks", registry)
    registry[confirmation_id] = task
    background = getattr(runtime, "_background_tasks", None)
    if isinstance(background, set):
        background.add(task)

    def _done(completed: asyncio.Task) -> None:
        if registry.get(confirmation_id) is completed:
            registry.pop(confirmation_id, None)
        if isinstance(background, set):
            background.discard(completed)
        try:
            completed.result()
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.warning(
                "Telegram delayed-message timer failed: %s", type(exc).__name__
            )

    task.add_done_callback(_done)


def schedule_telegram_confirmation_expiry(
    runtime: Any, record: Mapping[str, Any]
) -> None:
    confirmation_id = str(record.get("confirmation_id") or "")
    if not confirmation_id:
        return
    registry = getattr(runtime, "_telegram_confirmation_tasks", None)
    existing = registry.get(confirmation_id) if isinstance(registry, dict) else None
    if isinstance(existing, asyncio.Task) and not existing.done():
        return

    async def _expire_and_project() -> None:
        expires = parse_rfc3339_utc(record.get("expires_at"))
        if expires is not None:
            delay = max(0.0, (expires - datetime.now(timezone.utc)).total_seconds())
            if delay:
                await asyncio.sleep(delay)
        service = IngressConfirmationService(runtime.session_store)
        try:
            current = service.get(
                confirmation_id, owner_id=str(record["owner_id"])
            )
        except ConfirmationNotFoundError:
            return
        await _project_terminal(runtime, current)

    _track_task(
        runtime,
        confirmation_id,
        asyncio.create_task(
            _expire_and_project(),
            name=f"telegram-ingress-expiry:{confirmation_id}",
        ),
    )


def schedule_telegram_confirmation_recovery(
    runtime: Any, record: Mapping[str, Any]
) -> None:
    """Resume a user-approved replay after a worker died mid-confirmation."""
    confirmation_id = str(record.get("confirmation_id") or "")
    if not confirmation_id:
        return
    registry = getattr(runtime, "_telegram_confirmation_tasks", None)
    existing = registry.get(confirmation_id) if isinstance(registry, dict) else None
    if isinstance(existing, asyncio.Task) and not existing.done():
        return

    async def _recover() -> None:
        started = parse_rfc3339_utc(record.get("execution_started_at"))
        if started is not None:
            age = (datetime.now(timezone.utc) - started).total_seconds()
            delay = max(0.0, CONFIRMATION_EXECUTION_LEASE_SECONDS - age + 0.05)
            if delay:
                await asyncio.sleep(delay)
        service = IngressConfirmationService(runtime.session_store)
        try:
            current = await service.decide_async(
                confirmation_id=confirmation_id,
                decision="confirm",
                owner_id=str(record["owner_id"]),
                execute_fn=lambda claimed: _replay_persisted_update(runtime, claimed),
            )
        except (ConfirmationNotFoundError, ConfirmationExpiredError):
            return
        except Exception as exc:
            logger.warning(
                "Telegram delayed-message recovery remains retryable: %s",
                type(exc).__name__,
            )
            return
        await _project_terminal(runtime, current)

    _track_task(
        runtime,
        confirmation_id,
        asyncio.create_task(
            _recover(), name=f"telegram-ingress-recovery:{confirmation_id}"
        ),
    )


async def _replay_persisted_update(runtime: Any, record: Mapping[str, Any]) -> None:
    payload = record.get("payload")
    if not isinstance(payload, Mapping) or _payload_digest(payload) != record.get(
        "payload_digest"
    ):
        raise ConfirmationConflictError("Persisted Telegram payload digest mismatch")
    raw_update = payload.get("raw_update")
    if not isinstance(raw_update, Mapping):
        raise ConfirmationConflictError("Persisted Telegram Update is unavailable")
    app = getattr(runtime, "app", None)
    bot = _bot(runtime)
    process_update = getattr(app, "process_update", None)
    if app is None or bot is None or not callable(process_update):
        raise RuntimeError("Telegram application is not ready for confirmed replay")
    replayed = Update.de_json(dict(raw_update), bot)
    release = {
        "confirmation_id": str(record["confirmation_id"]),
        "source_message_id": str(record["source_message_id"]),
        "idempotency_key": str(record["idempotency_key"]),
        "originated_at": record.get("originated_at"),
        "received_at": record.get("received_at"),
        "owner_id": str(record["owner_id"]),
    }
    token = _CONFIRMED_RELEASE.set(release)
    try:
        from orchestrator.telegram_actor_scope import scoped_access_enabled

        if scoped_access_enabled(runtime):
            from orchestrator.telegram_actor_scope import process_telegram_update

            if not await process_telegram_update(
                runtime, replayed, expected_owner_id=release["owner_id"]
            ):
                raise ConfirmationConflictError("Telegram actor scope changed")
        else:
            await process_update(replayed)
    finally:
        _CONFIRMED_RELEASE.reset(token)


async def restore_telegram_confirmation_state(runtime: Any) -> None:
    """Restore expiry timers and overdue visible states after worker restart."""
    store = getattr(runtime, "session_store", None)
    if store is None:
        return
    records = IngressConfirmationService(store).list_for_connector(
        agent_id=str(runtime.name), connector_id="telegram"
    )
    for record in records:
        state = str(record.get("state") or "")
        if state == "awaiting_confirmation":
            schedule_telegram_confirmation_expiry(runtime, record)
        elif state == "confirming":
            schedule_telegram_confirmation_recovery(runtime, record)
        elif state in {"expired", "superseded", "confirmed", "discarded"}:
            await _project_terminal(runtime, record)


async def check_telegram_message_freshness(
    runtime: Any,
    update: Any,
    *,
    handler_kind: str = "text",
    command_name: str | None = None,
    args: list[str] | None = None,
    media_kind: str | None = None,
    filename: str | None = None,
    file_id: str | None = None,
    prompt: str | None = None,
    summary: str | None = None,
    **_ignored: Any,
) -> bool:
    """Return True when the Update is intercepted before normal handling."""
    message = _message(update)
    if message is None:
        logger.warning("Telegram ingress without an effective message was blocked")
        return True
    source_message_id = _source_message_id(runtime, update, message)
    release = _CONFIRMED_RELEASE.get()
    if isinstance(release, dict) and release.get("source_message_id") == source_message_id:
        return False

    now_dt = datetime.now(timezone.utc)
    freshness = classify_freshness(
        ingress_type="telegram",
        originated_at=getattr(message, "date", None),
        received_at=now_dt,
    )
    if freshness.is_fresh:
        return False
    chat_id = _chat_id(update, message)
    user_id = _user_id(update)
    if chat_id is None or user_id is None:
        logger.warning("Telegram ingress without stable chat/user identity was blocked")
        return True
    store = getattr(runtime, "session_store", None)
    if store is None:
        logger.error("Telegram delayed message blocked because confirmation store is unavailable")
        bot = _bot(runtime)
        if bot is not None:
            try:
                await bot.send_message(
                    chat_id=chat_id,
                    text=ui_language.tr(
                        "ingress_confirmation.store_unavailable",
                        locale=ui_language.preferred_locale(runtime, update),
                    ),
                )
            except Exception:
                pass
        return True

    owner_id = f"user:{user_id}"
    session = store.resolve_primary_session(
        owner_id=owner_id, agent_id=runtime.name, establish=True
    )
    locale = ui_language.preferred_locale(runtime, update)
    payload = {
        "version": 1,
        "raw_update": _json_compatible_update(update, message),
        "handler_kind": str(handler_kind),
        "source_message_id": source_message_id,
        "endpoint_agent_id": str(runtime.name),
    }
    service = IngressConfirmationService(store)
    record = service.register_pending(
        session_id=str(session["session_id"]),
        owner_id=owner_id,
        agent_id=str(runtime.name),
        connector_id="telegram",
        transport="telegram",
        source_message_id=source_message_id,
        idempotency_key=f"telegram-ingress:{source_message_id}",
        payload_digest=_payload_digest(payload),
        message_kind="frontend_message",
        context_generation=int(session.get("context_generation", 1)),
        originated_at=freshness.originated_at_iso,
        received_at=freshness.received_at_iso,
        threshold_seconds=freshness.threshold_seconds,
        reason=freshness.reason,
        delay_seconds=freshness.delay_seconds,
        expires_at=freshness.expires_at,
        payload=payload,
        display_metadata={
            "preview": _preview(
                message,
                handler_kind=handler_kind,
                command_name=command_name,
                args=args,
                media_kind=media_kind,
                filename=filename,
            ),
            "locale": locale,
            "telegram_chat_id": chat_id,
            "telegram_user_id": user_id,
            "attachment_name": filename,
            "media_kind": media_kind,
        },
    )

    metadata = dict(record.get("display_metadata") or {})
    if metadata.get("telegram_card_message_id") is not None:
        if record.get("state") == "awaiting_confirmation":
            schedule_telegram_confirmation_expiry(runtime, record)
        else:
            await _project_terminal(runtime, record)
        return True
    if str(record.get("state") or "") != "awaiting_confirmation":
        await _project_terminal(runtime, record)
        return True

    bot = _bot(runtime)
    if bot is None:
        logger.error("Telegram delayed message persisted but bot projection is unavailable")
        schedule_telegram_confirmation_expiry(runtime, record)
        return True
    try:
        sent = await bot.send_message(
            chat_id=chat_id,
            text=_card_text(record),
            reply_markup=_keyboard(record),
        )
        record = service.update_display_metadata(
            confirmation_id=str(record["confirmation_id"]),
            owner_id=owner_id,
            updates={
                "telegram_card_message_id": getattr(sent, "message_id", None),
                "telegram_card_created_at": now_dt.isoformat().replace("+00:00", "Z"),
            },
        )
    except Exception as exc:
        logger.warning(
            "Telegram delayed-message card could not be sent: %s", type(exc).__name__
        )
    schedule_telegram_confirmation_expiry(runtime, record)
    return True


async def handle_telegram_confirmation_callback(runtime: Any, update: Any) -> bool:
    """Apply an owner/chat-bound inline decision and project its terminal state."""
    query = getattr(update, "callback_query", None)
    data = str(getattr(query, "data", "") or "")
    if query is None or not data.startswith(TELEGRAM_CONFIRM_CALLBACK_PREFIX):
        return False
    parts = data.split(":")
    if len(parts) != 3 or parts[2] not in {"confirm", "discard"}:
        await query.answer()
        return True
    confirmation_id, action = parts[1], parts[2]
    user_id = _user_id(update)
    owner_id = f"user:{user_id}" if user_id is not None else ""
    store = getattr(runtime, "session_store", None)
    if not owner_id or store is None:
        await query.answer()
        return True
    service = IngressConfirmationService(store)
    try:
        record = service.get(confirmation_id, owner_id=owner_id)
    except ConfirmationNotFoundError:
        await query.answer(
            ui_language.tr("ingress_confirmation.not_found"), show_alert=True
        )
        return True

    metadata = dict(record.get("display_metadata") or {})
    query_message = getattr(query, "message", None)
    callback_chat_id = getattr(query_message, "chat_id", None)
    if callback_chat_id is None:
        callback_chat_id = getattr(getattr(query_message, "chat", None), "id", None)
    expected_card_id = metadata.get("telegram_card_message_id")
    callback_card_id = getattr(query_message, "message_id", None)
    identity_ok = (
        str(record.get("agent_id")) == str(runtime.name)
        and str(record.get("connector_id")) == "telegram"
        and str(metadata.get("telegram_chat_id")) == str(callback_chat_id)
        and (expected_card_id is None or str(expected_card_id) == str(callback_card_id))
    )
    locale = str(metadata.get("locale") or ui_language.DEFAULT_LOCALE)
    if not identity_ok:
        logger.warning("Rejected Telegram delayed-message callback identity mismatch")
        await query.answer(
            ui_language.tr("ingress_confirmation.identity_mismatch", locale=locale),
            show_alert=True,
        )
        return True
    payload = record.get("payload")
    if not isinstance(payload, Mapping) or _payload_digest(payload) != record.get(
        "payload_digest"
    ):
        logger.error("Rejected Telegram delayed-message callback with digest mismatch")
        await query.answer(
            ui_language.tr("ingress_confirmation.identity_mismatch", locale=locale),
            show_alert=True,
        )
        return True
    await query.answer()
    try:
        if action == "discard":
            record = service.decide(
                confirmation_id=confirmation_id,
                decision="discard",
                owner_id=owner_id,
            )
        else:
            record = await service.decide_async(
                confirmation_id=confirmation_id,
                decision="confirm",
                owner_id=owner_id,
                execute_fn=lambda claimed: _replay_persisted_update(runtime, claimed),
            )
    except (ConfirmationExpiredError, ConfirmationSupersededError, ConfirmationConflictError):
        record = service.get(confirmation_id, owner_id=owner_id)
    except Exception as exc:
        logger.warning(
            "Telegram delayed-message confirmation remains retryable: %s",
            type(exc).__name__,
        )
        record = service.get(confirmation_id, owner_id=owner_id)
        bot = _bot(runtime)
        if bot is not None:
            try:
                await bot.send_message(
                    chat_id=metadata.get("telegram_chat_id"),
                    text=ui_language.tr(
                        "ingress_confirmation.state_retryable", locale=locale
                    ),
                )
            except Exception:
                pass
        return True

    if str(record.get("state") or "") == "confirming":
        await _edit_or_fallback(runtime, record, _terminal_text(record))
    else:
        await _project_terminal(runtime, record)
    return True
