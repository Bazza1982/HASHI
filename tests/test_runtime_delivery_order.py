from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from orchestrator import runtime_delivery_order


def _item(request_id: str, *, source: str = "text", chat_id: int = 123):
    return SimpleNamespace(
        request_id=request_id,
        source=source,
        chat_id=chat_id,
        silent=False,
        deliver_to_telegram=True,
    )


@pytest.mark.asyncio
async def test_adjacent_direct_turns_wait_for_delivery_sequence():
    runtime = SimpleNamespace()
    first = _item("req-first")
    second = _item("req-second")
    runtime_delivery_order.register_turn(runtime, first)
    runtime_delivery_order.register_turn(runtime, second)

    await runtime_delivery_order.wait_for_turn(runtime, first.request_id)
    second_wait = asyncio.create_task(
        runtime_delivery_order.wait_for_turn(runtime, second.request_id)
    )
    await asyncio.sleep(0)
    assert second_wait.done() is False

    await runtime_delivery_order.complete_turn(runtime, first.request_id)
    await asyncio.wait_for(second_wait, timeout=1)
    await runtime_delivery_order.complete_turn(runtime, second.request_id)


@pytest.mark.asyncio
async def test_early_later_completion_does_not_skip_older_direct_turn():
    runtime = SimpleNamespace()
    first = _item("req-first")
    second = _item("req-second")
    runtime_delivery_order.register_turn(runtime, first)
    runtime_delivery_order.register_turn(runtime, second)

    await runtime_delivery_order.complete_turn(runtime, second.request_id)
    assert runtime_delivery_order.pending_sequences(runtime, first.chat_id) == [1, 2]

    await runtime_delivery_order.complete_turn(runtime, first.request_id)
    assert runtime_delivery_order.pending_sequences(runtime, first.chat_id) == []


@pytest.mark.asyncio
async def test_scheduler_turn_is_not_part_of_direct_delivery_sequence():
    runtime = SimpleNamespace()
    scheduler = _item("req-scheduler", source="scheduler")

    assert runtime_delivery_order.register_turn(runtime, scheduler) is None
    await runtime_delivery_order.wait_for_turn(runtime, scheduler.request_id)
    await runtime_delivery_order.complete_turn(runtime, scheduler.request_id)
    assert runtime_delivery_order.pending_sequences(runtime, scheduler.chat_id) == []


@pytest.mark.parametrize(
    "source",
    [
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
    ],
)
def test_all_direct_telegram_sources_receive_a_delivery_sequence(source):
    runtime = SimpleNamespace()
    item = _item(f"req-{source}", source=source)

    assert runtime_delivery_order.register_turn(runtime, item) == 1
    assert runtime_delivery_order.pending_sequences(runtime, item.chat_id) == [1]
