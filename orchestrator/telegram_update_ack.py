"""Propagate swallowed PTB handler failures to the ingress acknowledgement."""
from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any


@dataclass
class _Dispatch:
    update_id: int
    errors: list[BaseException] = field(default_factory=list)


_current: ContextVar[_Dispatch | None] = ContextVar("telegram_dispatch_ack", default=None)


def record_handler_error(update: Any, error: BaseException) -> None:
    current = _current.get()
    if current is not None and getattr(update, "update_id", None) == current.update_id:
        current.errors.append(error)


async def dispatch_update(runtime: Any, update: Any) -> None:
    from orchestrator.telegram_actor_scope import scoped_access_enabled, process_telegram_update

    current = _Dispatch(update.update_id)
    token = _current.set(current)
    try:
        if scoped_access_enabled(runtime):
            # Denied actors are a completed policy decision, not a failed
            # business request. They must not jam the Bot's inbox forever.
            await process_telegram_update(runtime, update)
        else:
            await runtime.app.process_update(update)
        if current.errors:
            raise RuntimeError("Telegram handler failed; processing was not acknowledged") from current.errors[0]
    finally:
        _current.reset(token)
# HASHI-Fingerprint: hfp_4e519eefc2b4443dad84891113aae543
