"""FIFO completion ordering for adjacent direct user turns.

The queue may detach a long backend generation while the persistent HER
session continues in the background.  This module assigns direct turns a
per-chat receipt order and prevents a later turn from committing or delivering
its result before every earlier direct turn reaches a terminal path.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

_DIRECT_SOURCES = frozenset(
    {
        "audio",
        "document",
        "multimodal",
        "photo",
        "sticker",
        "telegram",
        "text",
        "video",
        "voice",
        "voice_transcript",
    }
)


@dataclass
class _TurnOrder:
    request_id: str
    chat_key: str
    sequence: int
    completed: bool = False


@dataclass
class _ChatOrder:
    next_assign: int = 1
    next_delivery: int = 1
    turns: dict[int, _TurnOrder] = field(default_factory=dict)
    condition: asyncio.Condition = field(default_factory=asyncio.Condition)


@dataclass
class _DeliveryOrderState:
    chats: dict[str, _ChatOrder] = field(default_factory=dict)
    requests: dict[str, _TurnOrder] = field(default_factory=dict)


def _state(runtime: Any) -> _DeliveryOrderState:
    state = getattr(runtime, "_direct_delivery_order", None)
    if not isinstance(state, _DeliveryOrderState):
        state = _DeliveryOrderState()
        runtime._direct_delivery_order = state
    return state


def register_turn(runtime: Any, item: Any) -> int | None:
    """Assign a stable per-chat sequence to an interactive direct turn."""
    source = str(getattr(item, "source", "") or "").strip().lower()
    if source not in _DIRECT_SOURCES:
        return None
    request_id = str(getattr(item, "request_id", "") or "").strip()
    if not request_id:
        return None
    state = _state(runtime)
    existing = state.requests.get(request_id)
    if existing is not None:
        return existing.sequence
    chat_key = str(getattr(item, "chat_id", ""))
    chat = state.chats.setdefault(chat_key, _ChatOrder())
    sequence = chat.next_assign
    chat.next_assign += 1
    turn = _TurnOrder(
        request_id=request_id,
        chat_key=chat_key,
        sequence=sequence,
    )
    chat.turns[sequence] = turn
    state.requests[request_id] = turn
    return sequence


async def wait_for_turn(runtime: Any, request_id: str | None) -> None:
    """Wait until ``request_id`` owns the direct completion lane."""
    state = getattr(runtime, "_direct_delivery_order", None)
    if not isinstance(state, _DeliveryOrderState):
        return
    turn = state.requests.get(str(request_id or ""))
    if turn is None:
        return
    chat = state.chats.get(turn.chat_key)
    if chat is None:
        return
    async with chat.condition:
        await chat.condition.wait_for(
            lambda: turn.sequence <= chat.next_delivery
        )


async def complete_turn(runtime: Any, request_id: str | None) -> None:
    """Mark a direct turn terminal and release every contiguous successor."""
    state = getattr(runtime, "_direct_delivery_order", None)
    if not isinstance(state, _DeliveryOrderState):
        return
    turn = state.requests.get(str(request_id or ""))
    if turn is None:
        return
    chat = state.chats.get(turn.chat_key)
    if chat is None:
        state.requests.pop(turn.request_id, None)
        return
    async with chat.condition:
        turn.completed = True
        while True:
            current = chat.turns.get(chat.next_delivery)
            if current is None or not current.completed:
                break
            chat.turns.pop(chat.next_delivery, None)
            state.requests.pop(current.request_id, None)
            chat.next_delivery += 1
        chat.condition.notify_all()
        if not chat.turns:
            state.chats.pop(turn.chat_key, None)


def pending_sequences(runtime: Any, chat_id: Any) -> list[int]:
    """Return pending sequence numbers for diagnostics and deterministic tests."""
    state = getattr(runtime, "_direct_delivery_order", None)
    if not isinstance(state, _DeliveryOrderState):
        return []
    chat = state.chats.get(str(chat_id))
    return sorted(chat.turns) if chat is not None else []
