"""Telegram actor isolation for PAO Session admission.

This helper is not a publication capability: a caller must also enforce
current grants at every outbound path before exposing management.
"""
from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from orchestrator.config_json import read_config_json
from orchestrator.telegram_access import (
    POLICY_VERSION_KEY, TelegramAccessSnapshot, access_snapshot,
)


@dataclass(frozen=True)
class TelegramActorScope:
    runtime: Any
    user_id: int
    owner_id: str
    is_primary: bool
    is_global: bool


_current_actor: ContextVar[TelegramActorScope | None] = ContextVar(
    "hashi_telegram_actor_scope", default=None
)


def current_actor(runtime: Any) -> TelegramActorScope | None:
    actor = _current_actor.get()
    return actor if actor is not None and actor.runtime is runtime else None


def scoped_access_enabled(runtime: Any) -> bool:
    """Use the persisted policy marker; unreadable configuration fails closed."""
    override = getattr(runtime, "_telegram_scoped_access_enabled", None)
    if override is not None:
        return bool(override)
    config = getattr(runtime, "global_config", None)
    path = getattr(config, "config_path", None)
    if not path:
        return False
    try:
        document = read_config_json(path)
        global_config = document.get("global")
        if not isinstance(global_config, Mapping):
            return True
        if POLICY_VERSION_KEY in global_config:
            return True
        rows = document.get("agents")
        name = str(getattr(runtime, "name", "") or "").strip().casefold()
        if not isinstance(rows, list) or not name:
            return True
        matches = [row for row in rows if isinstance(row, Mapping)
                   and str(row.get("name") or "").strip().casefold() == name]
        if len(matches) != 1:
            return True
        row = matches[0]
        extra = row.get("extra")
        if extra is not None and not isinstance(extra, Mapping):
            return True
        return POLICY_VERSION_KEY in (extra if extra else row)
    except (OSError, ValueError, TypeError):
        return True


def _delegated_owner(snapshot: TelegramAccessSnapshot, user_id: int) -> str:
    # The stable owner-scope fingerprint prevents historical conversations of
    # a previous instance owner from being adopted by a replacement owner.
    digest = hashlib.sha256(snapshot.owner_scope_id.encode("utf-8")).hexdigest()[:24]
    result = f"telegram-user:{user_id}:{digest}"
    if result == snapshot.owner_scope_id:
        raise ValueError("Telegram actor scope collides with primary owner")
    return result


def runtime_snapshot(runtime: Any) -> TelegramAccessSnapshot:
    config = getattr(runtime, "global_config", None)
    config_path = getattr(config, "config_path", None)
    if not config_path:
        raise ValueError("Telegram access configuration unavailable")
    config_doc = read_config_json(config_path)
    secrets_path = getattr(config, "secrets_path", None)
    if secrets_path:
        secrets_doc = read_config_json(secrets_path)
    else:
        secrets_doc = getattr(runtime, "secrets", None) or {}
    return access_snapshot(config_doc, secrets_doc, str(getattr(runtime, "name", "")))


def explicit_telegram_purpose(purpose: str) -> bool:
    """Recognize the explicit notification tools, not ordinary replies."""
    name = str(purpose or "")
    return (
        name == "explicit-media-notification"
        or name == "explicit-notification"
        or name.startswith("explicit-notification:")
    )


def delivery_allowed(
    runtime: Any, *, owner_id: str | None, chat_id: Any,
    explicit_recipient: bool = False,
) -> bool:
    """Check the current grant for a delegated Session at delivery time."""
    owner = str(owner_id or "")
    if not owner.startswith("telegram-user:"):
        return True
    try:
        _, actor_text, _fingerprint = owner.split(":")
        actor = int(actor_text)
        snapshot = runtime_snapshot(runtime)
        return (
            actor in snapshot.effective_user_ids
            and actor != snapshot.primary_user_id
            and (
                int(chat_id) == actor
                or (explicit_recipient and actor in snapshot.global_user_ids)
            )
            and owner == _delegated_owner(snapshot, actor)
        )
    except (OSError, ValueError, TypeError, AttributeError, LookupError):
        return False


def workzone_owner(
    runtime: Any, owner_id: str, *, require_global: bool = False
) -> str | None:
    """Resolve an authorized Telegram user's selected Agent Workzone owner."""
    owner = str(owner_id or "").strip()
    if not owner.startswith("telegram-user:"):
        return owner or None
    try:
        _, actor_text, _fingerprint = owner.split(":")
        actor = int(actor_text)
        snapshot = runtime_snapshot(runtime)
        scope = current_actor(runtime)
        if (
            actor not in snapshot.effective_user_ids
            or actor == snapshot.primary_user_id
            or owner != _delegated_owner(snapshot, actor)
            or (scope is not None and scope.owner_id != owner)
            or (require_global and actor not in snapshot.global_user_ids)
        ):
            return None
        return snapshot.owner_scope_id
    except (OSError, ValueError, TypeError, AttributeError, LookupError):
        return None


def _safevoice_callback_matches_chat(runtime: Any, data: str, chat_id: int) -> bool:
    parts = data.split(":", 2)
    if len(parts) != 3 or parts[0] != "safevoice" or parts[1] not in {"yes", "no"}:
        return True
    pending = getattr(runtime, "_pending_voice", None)
    record = pending.get(parts[2]) if isinstance(pending, dict) else None
    if record is None:
        return parts[2] == str(chat_id)
    try:
        return int(record.get("chat_id")) == chat_id
    except (TypeError, ValueError, AttributeError):
        return False


async def _handle_scoped_interaction(
    runtime: Any, update: Any, scope: TelegramActorScope, chat_id: int
) -> bool:
    """Consume management interactions for Agent-only chat grants."""
    from orchestrator import ui_language

    query = getattr(update, "callback_query", None)
    data = str(getattr(query, "data", "") or "")
    if query is not None and not scope.is_primary:
        if not _safevoice_callback_matches_chat(runtime, data, chat_id):
            await query.answer()
            return True
    if scope.is_global:
        return False
    locale = ui_language.preferred_locale(runtime, update)
    if query is not None:
        from orchestrator.runtime_command_binding import CALLBACK_BINDINGS

        conversation_callback = any(
            not binding.command and re.match(binding.pattern, data)
            for binding in CALLBACK_BINDINGS
        )
        safevoice_callback = (
            data.startswith(("safevoice:yes:", "safevoice:no:"))
            and _safevoice_callback_matches_chat(runtime, data, chat_id)
        )
        if conversation_callback or safevoice_callback:
            return False
        if delivery_allowed(runtime, owner_id=scope.owner_id, chat_id=chat_id):
            await query.answer(ui_language.tr("telegram.scoped.chat_only", locale=locale))
        return True
    message = getattr(update, "effective_message", None) or getattr(update, "message", None)
    raw_text = str(getattr(message, "text", "") or "").strip()
    if not raw_text.startswith("/"):
        return False
    command = raw_text.split(None, 1)[0].split("@", 1)[0].casefold()
    welcome = command == "/start" and len(raw_text.split(None, 1)) == 1
    reply = getattr(runtime, "_reply_text", None)
    if callable(reply):
        key = "telegram.scoped.welcome" if welcome else "telegram.scoped.chat_only"
        await reply(update, ui_language.tr(key, locale=locale))
    return True


async def process_telegram_update(
    runtime: Any, update: Any, *, expected_owner_id: str | None = None
) -> bool:
    """Process a real Telegram Update inside its authorized actor scope."""
    try:
        snapshot = runtime_snapshot(runtime)
        user = getattr(update, "effective_user", None)
        chat = getattr(update, "effective_chat", None)
        actor = int(getattr(user, "id"))
        chat_id = int(getattr(chat, "id"))
        if actor not in snapshot.effective_user_ids:
            return False
        is_primary = actor == snapshot.primary_user_id
        is_legacy = not is_primary and actor in snapshot.legacy_user_ids
        if not is_primary and not is_legacy and (
            str(getattr(chat, "type", "")) != "private" or chat_id != actor
        ):
            return False
        owner = (snapshot.owner_scope_id if is_primary or is_legacy
                 else _delegated_owner(snapshot, actor))
        if expected_owner_id is not None and owner != expected_owner_id:
            return False
        if is_legacy:
            # Existing Agent allowlists used the owner's Session and command
            # authority. Preserve those grants until explicitly removed.
            token = _current_actor.set(None)
            try:
                await runtime.app.process_update(update)
            finally:
                _current_actor.reset(token)
            return True
        scope = TelegramActorScope(
            runtime=runtime,
            user_id=actor,
            owner_id=owner,
            is_primary=is_primary,
            is_global=actor in snapshot.global_user_ids,
        )
    except (OSError, ValueError, TypeError, AttributeError, LookupError):
        return False
    token = _current_actor.set(scope)
    try:
        if await _handle_scoped_interaction(runtime, update, scope, chat_id):
            return True
        await runtime.app.process_update(update)
    finally:
        _current_actor.reset(token)
    return True
