"""Replaceable HER v2 stage and HASHI boundary interfaces."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Protocol, Sequence

from .config import ProviderProfile
from .models import DeliveryRecord, StageRequest, StageResponse, TerminalState


class StageInvocationError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool = True):
        super().__init__(message)
        self.retryable = retryable


class StructuredOutputError(StageInvocationError):
    pass


class TurnStopped(RuntimeError):
    def __init__(self, reason: str):
        super().__init__(f"HER v2 turn stopped: {reason}")
        self.reason = reason


class StageProvider(Protocol):
    async def invoke(
        self, profile: ProviderProfile, request: StageRequest
    ) -> StageResponse: ...


@dataclass(frozen=True)
class DeliveryReceipt:
    """Transport acknowledgement without conflating acceptance and delivery."""

    accepted: bool
    delivered: bool
    disposition: str


class DeliveryPort(Protocol):
    async def deliver(
        self,
        *,
        kind: str,
        text: str,
        event_id: str,
        required: bool = False,
        phase: str = "",
        provenance: str = "",
        detail: str = "",
        delivery_id: str = "",
    ) -> bool | DeliveryReceipt: ...

    async def resolve_initial(
        self,
        *,
        resolution: str,
        text: str,
        target_event_id: str,
        event_id: str,
        delivery_id: str = "",
    ) -> bool | DeliveryReceipt: ...


class HabitAdvisor(Protocol):
    async def retrieve(self, *, goal: str, turn_id: str) -> Sequence[str]: ...


class MeditationRunner(Protocol):
    async def meditate(
        self,
        *,
        turn_id: str,
        goal: str,
        summary: str,
        evidence_refs: Sequence[str],
        limitations: Sequence[str],
        terminal_state: TerminalState,
    ) -> None: ...


class DreamMaintainer(Protocol):
    async def maintain(self, *, catalogue_ref: str) -> Sequence[str]: ...


@dataclass
class RecordingDelivery:
    records: list[DeliveryRecord] = field(default_factory=list)
    fail_kinds: set[str] = field(default_factory=set)

    async def deliver(
        self,
        *,
        kind: str,
        text: str,
        event_id: str,
        required: bool = False,
        phase: str = "",
        provenance: str = "",
        detail: str = "",
        delivery_id: str = "",
    ) -> bool:
        del required, phase, provenance, detail, delivery_id
        if kind in self.fail_kinds:
            return False
        if any(item.event_id == event_id for item in self.records):
            return True
        self.records.append(DeliveryRecord(kind=kind, text=text, event_id=event_id))
        return True

    async def resolve_initial(
        self,
        *,
        resolution: str,
        text: str,
        target_event_id: str,
        event_id: str,
        delivery_id: str = "",
    ) -> DeliveryReceipt:
        del event_id, delivery_id
        index = next(
            (
                index
                for index, record in enumerate(self.records)
                if record.event_id == target_event_id
            ),
            None,
        )
        if index is None:
            return DeliveryReceipt(False, False, "provisional_not_found")
        if resolution == "discard":
            self.records.pop(index)
        else:
            kind = "acknowledgement" if resolution == "commentary" else resolution
            self.records[index] = DeliveryRecord(kind, text, target_event_id)
        return DeliveryReceipt(True, True, f"provisional_{resolution}")


@dataclass
class NullHabitAdvisor:
    async def retrieve(self, *, goal: str, turn_id: str) -> Sequence[str]:
        del goal, turn_id
        return ()


@dataclass
class NullMeditationRunner:
    calls: int = 0

    async def meditate(
        self,
        *,
        turn_id: str,
        goal: str,
        summary: str,
        evidence_refs: Sequence[str],
        limitations: Sequence[str],
        terminal_state: TerminalState,
    ) -> None:
        del turn_id, goal, summary, evidence_refs, limitations, terminal_state
        self.calls += 1


@dataclass
class NullDreamMaintainer:
    calls: int = 0

    async def maintain(self, *, catalogue_ref: str) -> Sequence[str]:
        del catalogue_ref
        self.calls += 1
        return ()


@dataclass
class TurnControl:
    turn_id: str
    stop_event: asyncio.Event = field(default_factory=asyncio.Event)
    reason: str = ""
    _active_tasks: set[asyncio.Task] = field(default_factory=set)

    @property
    def stopped(self) -> bool:
        return self.stop_event.is_set()

    def stop(self, reason: str) -> None:
        self.reason = str(reason or "USER_STOP")
        self.stop_event.set()
        for task in tuple(self._active_tasks):
            task.cancel()

    async def wait_stopped(self) -> None:
        tasks = tuple(self._active_tasks)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def run_cancellable(self, operation: Awaitable[StageResponse]) -> StageResponse:
        if self.stopped:
            raise TurnStopped(self.reason)
        task = asyncio.create_task(operation)
        self._active_tasks.add(task)
        stop_wait = asyncio.create_task(self.stop_event.wait())
        try:
            done, _pending = await asyncio.wait(
                {task, stop_wait}, return_when=asyncio.FIRST_COMPLETED
            )
            if stop_wait in done and self.stopped:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                raise TurnStopped(self.reason)
            return await task
        except asyncio.CancelledError:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            raise
        finally:
            stop_wait.cancel()
            await asyncio.gather(stop_wait, return_exceptions=True)
            self._active_tasks.discard(task)


StageValidator = Callable[[StageResponse], object]
