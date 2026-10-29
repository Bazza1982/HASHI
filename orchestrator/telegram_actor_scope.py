"""Telegram actor isolation for PAO Session admission.

This helper is not a publication capability: a caller must also enforce
current grants at every outbound path before exposing management.
"""
from __future__ import annotations

import hashlib
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from orchestrator.config_json import read_config_json
from orchestrator.telegram_access import TelegramAccessSnapshot, access_snapshot


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


def delivery_allowed(runtime: Any, *, owner_id: str | None, chat_id: Any) -> bool:
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
            and int(chat_id) == actor
            and owner == _delegated_owner(snapshot, actor)
        )
    except (OSError, ValueError, TypeError, AttributeError, LookupError):
        return False


async def process_telegram_update(runtime: Any, update: Any) -> bool:
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
        if not is_primary and (
            str(getattr(chat, "type", "")) != "private" or chat_id != actor
        ):
            return False
        owner = snapshot.owner_scope_id if is_primary else _delegated_owner(snapshot, actor)
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
        await runtime.app.process_update(update)
    finally:
        _current_actor.reset(token)
    return True
