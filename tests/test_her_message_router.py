from __future__ import annotations

from types import SimpleNamespace

import pytest

from adapters.stream_events import (
    DELIVERY_CONTROL,
    DELIVERY_FINAL,
    DELIVERY_REASONING,
    DELIVERY_TECHNICAL,
    DELIVERY_USER_COMMENTARY,
    KIND_ACKNOWLEDGEMENT,
    KIND_COMMENTARY,
    KIND_PROGRESS,
    KIND_THINKING,
    StreamEvent,
)
from orchestrator.her_message_router import HERMessageRouter


def _event(
    delivery_class: str,
    event_id: str,
    *,
    kind: str = KIND_PROGRESS,
    summary: str = "event",
    required: bool = False,
) -> StreamEvent:
    return StreamEvent(
        kind=kind,
        summary=summary,
        event_id=event_id,
        delivery_class=delivery_class,
        origin="test",
        phase="execution",
        required=required,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("commentary", "verbose", "think"),
    [
        (False, False, False),
        (False, False, True),
        (False, True, False),
        (False, True, True),
        (True, False, False),
        (True, False, True),
        (True, True, False),
        (True, True, True),
    ],
)
async def test_all_toggle_combinations_route_directly_to_the_enabled_owner(
    commentary, verbose, think
):
    presented = []
    persisted = []

    async def presenter(owner):
        async def present(event):
            presented.append((owner, event.event_id))

        return present

    async def persist(event):
        persisted.append(event.event_id)

    router = HERMessageRouter(
        request_id="req-matrix",
        logger=SimpleNamespace(
            info=lambda _message: None, warning=lambda _message: None
        ),
        technical_presenter=await presenter("technical"),
        reasoning_presenter=await presenter("reasoning"),
        commentary_presenter=await presenter("commentary"),
        verbose_enabled=lambda: verbose,
        think_enabled=lambda: think,
        commentary_enabled=lambda: commentary,
        persist_event=persist,
    )
    events = [
        _event(DELIVERY_TECHNICAL, "req-matrix:technical:1"),
        _event(
            DELIVERY_USER_COMMENTARY,
            "req-matrix:commentary:1",
            kind=KIND_COMMENTARY,
        ),
        _event(
            DELIVERY_REASONING,
            "req-matrix:reasoning:1",
            kind=KIND_THINKING,
        ),
    ]

    for event in events:
        await router.route(event)
        await router.route(event)

    expected = []
    if verbose:
        expected.extend([("technical", "req-matrix:technical:1")] * 2)
    if commentary:
        expected.extend([("commentary", "req-matrix:commentary:1")] * 2)
    if think:
        expected.extend([("reasoning", "req-matrix:reasoning:1")] * 2)
    assert presented == expected
    assert persisted == [event.event_id for event in events for _ in range(2)]


@pytest.mark.asyncio
async def test_required_control_bypasses_all_optional_toggles():
    presented = []
    router = HERMessageRouter(
        request_id="req-control",
        logger=SimpleNamespace(
            info=lambda _message: None, warning=lambda _message: None
        ),
        control_presenter=lambda event: presented.append(event.event_id),
        verbose_enabled=lambda: False,
        think_enabled=lambda: False,
        commentary_enabled=lambda: False,
        delivery_blocked=True,
    )
    event = _event(
        DELIVERY_CONTROL,
        "req-control:permission:1",
        summary="permission required",
        required=True,
    )

    await router.route(event)
    await router.route(event)

    assert presented == ["req-control:permission:1"] * 2


@pytest.mark.asyncio
async def test_direct_response_is_deferred_to_mandatory_final_lane():
    presented = []
    router = HERMessageRouter(
        request_id="req-direct",
        logger=SimpleNamespace(
            info=lambda _message: None, warning=lambda _message: None
        ),
        commentary_presenter=lambda event: presented.append(event.event_id),
        commentary_enabled=lambda: True,
    )
    direct = _event(
        DELIVERY_FINAL,
        "req-direct:final",
        kind=KIND_ACKNOWLEDGEMENT,
        summary="complete direct answer",
        required=True,
    )

    await router.route(direct)
    await router.route(direct)

    assert presented == []
    assert router.deferred_final is direct


@pytest.mark.asyncio
async def test_presenter_failure_does_not_create_a_second_retry_system():
    attempts = []

    async def flaky(event):
        attempts.append(event.event_id)
        if len(attempts) == 1:
            raise RuntimeError("temporary send failure")

    router = HERMessageRouter(
        request_id="req-retry",
        logger=SimpleNamespace(
            info=lambda _message: None, warning=lambda _message: None
        ),
        commentary_presenter=flaky,
        commentary_enabled=lambda: True,
    )
    event = _event(
        DELIVERY_USER_COMMENTARY,
        "req-retry:commentary:1",
        kind=KIND_COMMENTARY,
    )

    await router.route(event)

    assert attempts == [event.event_id]


@pytest.mark.asyncio
async def test_distinct_scheduled_events_are_not_globally_deduplicated_by_text():
    presented = []
    router = HERMessageRouter(
        request_id="req-lease",
        logger=SimpleNamespace(
            info=lambda _message: None, warning=lambda _message: None
        ),
        technical_presenter=lambda event: presented.append(event.event_id),
        verbose_enabled=lambda: True,
    )
    first = _event(
        DELIVERY_TECHNICAL,
        "req-lease:technical:lease:1",
        summary="still working",
    )
    second = _event(
        DELIVERY_TECHNICAL,
        "req-lease:technical:lease:2",
        summary="still working",
    )

    await router.route(first)
    await router.route(second)

    assert presented == [first.event_id, second.event_id]


@pytest.mark.asyncio
async def test_missing_delivery_class_is_audited_but_not_presented():
    order = []
    router = HERMessageRouter(
        request_id="req-invalid",
        logger=SimpleNamespace(
            info=lambda _message: None, warning=lambda _message: None
        ),
        technical_presenter=lambda event: order.append(("present", event.kind)),
        verbose_enabled=lambda: True,
        persist_event=lambda event: order.append(("persist", event.kind)),
    )

    await router.route(StreamEvent(kind=KIND_PROGRESS, summary="legacy HER event"))

    assert order == [("persist", KIND_PROGRESS)]


@pytest.mark.asyncio
async def test_persistence_failure_does_not_block_direct_presentation():
    presented = []

    async def fail_persistence(_event):
        raise OSError("disk unavailable")

    router = HERMessageRouter(
        request_id="req-persist",
        logger=SimpleNamespace(
            info=lambda _message: None, warning=lambda _message: None
        ),
        commentary_presenter=lambda event: presented.append(event.event_id),
        commentary_enabled=lambda: True,
        persist_event=fail_persistence,
    )
    event = _event(
        DELIVERY_USER_COMMENTARY,
        "req-persist:ack:initial",
        kind=KIND_ACKNOWLEDGEMENT,
        summary="private Persona wording",
    )

    await router.route(event)

    assert presented == [event.event_id]
