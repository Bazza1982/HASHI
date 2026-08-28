"""Replaceable HER v2 stage and HASHI boundary interfaces."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable, Mapping, Protocol, Sequence

from .config import ProviderProfile
from .models import DeliveryRecord, StageRequest, StageResponse, TerminalState


class ProviderFailureCode(str, Enum):
    PROVIDER_UNKNOWN = "PROVIDER_UNKNOWN"
    PROVIDER_CONFIGURATION_ERROR = "PROVIDER_CONFIGURATION_ERROR"
    PROVIDER_BAD_REQUEST = "PROVIDER_BAD_REQUEST"
    PROVIDER_AUTHENTICATION_FAILED = "PROVIDER_AUTHENTICATION_FAILED"
    PROVIDER_PERMISSION_DENIED = "PROVIDER_PERMISSION_DENIED"
    PROVIDER_REQUEST_TIMEOUT = "PROVIDER_REQUEST_TIMEOUT"
    PROVIDER_RATE_LIMITED = "PROVIDER_RATE_LIMITED"
    PROVIDER_SERVER_ERROR = "PROVIDER_SERVER_ERROR"
    PROVIDER_CONNECTION_FAILED = "PROVIDER_CONNECTION_FAILED"
    PROVIDER_TLS_ERROR = "PROVIDER_TLS_ERROR"
    PROVIDER_RESPONSE_START_TIMEOUT = "PROVIDER_RESPONSE_START_TIMEOUT"
    PROVIDER_INCOMPLETE_STREAM = "PROVIDER_INCOMPLETE_STREAM"
    PROVIDER_INCOMPLETE_STREAM_TIMEOUT = "PROVIDER_INCOMPLETE_STREAM_TIMEOUT"
    PROVIDER_REASONING_ONLY_TIMEOUT = "PROVIDER_REASONING_ONLY_TIMEOUT"
    PROVIDER_STREAM_IDLE_TIMEOUT = "PROVIDER_STREAM_IDLE_TIMEOUT"
    PROVIDER_EMPTY_RESPONSE = "PROVIDER_EMPTY_RESPONSE"
    PROVIDER_MODALITY_UNSUPPORTED = "PROVIDER_MODALITY_UNSUPPORTED"
    INVALID_MULTIMODAL_CONTENT = "INVALID_MULTIMODAL_CONTENT"
    INLINE_MEDIA_PERSISTENCE_FORBIDDEN = "INLINE_MEDIA_PERSISTENCE_FORBIDDEN"
    MEDIA_PATH_NOT_AUTHORIZED = "MEDIA_PATH_NOT_AUTHORIZED"
    MEDIA_UNAVAILABLE = "MEDIA_UNAVAILABLE"
    MEDIA_INTEGRITY_CHANGED = "MEDIA_INTEGRITY_CHANGED"
    MEDIA_SIGNATURE_MISMATCH = "MEDIA_SIGNATURE_MISMATCH"
    MEDIA_SIGNATURE_UNVERIFIED = "MEDIA_SIGNATURE_UNVERIFIED"
    MEDIA_LIMIT_EXCEEDED = "MEDIA_LIMIT_EXCEEDED"
    MEDIA_TRANSPORT_UNSUPPORTED = "MEDIA_TRANSPORT_UNSUPPORTED"
    DUPLICATE_MEDIA_REFERENCE = "DUPLICATE_MEDIA_REFERENCE"
    STRUCTURED_OUTPUT_INVALID = "STRUCTURED_OUTPUT_INVALID"
    REPLAY_SAFETY_UNPROVEN = "REPLAY_SAFETY_UNPROVEN"
    SIDE_EFFECT_REPLAY_BLOCKED = "SIDE_EFFECT_REPLAY_BLOCKED"
    AUDIT_PERSISTENCE_FAILURE = "AUDIT_PERSISTENCE_FAILURE"


_SECRET_ERROR_PATTERNS = (
    re.compile(r"(?i)(authorization\s*[:=]\s*bearer\s+)[^\s,;]+"),
    re.compile(r"(?i)((?:api[_-]?key|token|secret)\s*[:=]\s*)[^\s,;]+"),
)


def sanitise_provider_error(value: object, *, limit: int = 1200) -> str:
    text = " ".join(str(value or "").split())
    for pattern in _SECRET_ERROR_PATTERNS:
        text = pattern.sub(r"\1[REDACTED]", text)
    return text[: max(1, int(limit))]


class StageInvocationError(RuntimeError):
    """Typed provider/stage failure preserved through retry and audit."""

    def __init__(
        self,
        message: str,
        *,
        retryable: bool = True,
        code: ProviderFailureCode | str = ProviderFailureCode.PROVIDER_UNKNOWN,
        human_description: str = "",
        http_status: int | None = None,
        provider_request_id: str = "",
        retry_after_s: float | None = None,
        side_effects_possible: bool = False,
        details: Mapping[str, Any] | None = None,
        attempts: int = 1,
    ):
        safe_message = sanitise_provider_error(message)
        super().__init__(safe_message)
        self.retryable = bool(retryable)
        try:
            self.code = (
                code
                if isinstance(code, ProviderFailureCode)
                else ProviderFailureCode(str(code))
            )
        except ValueError:
            # HASHI-owned boundary codes (for example typed context-capacity
            # rejection) intentionally survive without expanding this enum or
            # coupling generic StageRequest to a compaction concern.
            raw_code = str(code or "")
            self.code = (
                raw_code
                if raw_code.startswith("CONTEXT_")
                else ProviderFailureCode.PROVIDER_UNKNOWN
            )
        self.human_description = sanitise_provider_error(
            human_description or safe_message,
            limit=600,
        )
        self.http_status = int(http_status) if http_status is not None else None
        self.provider_request_id = sanitise_provider_error(
            provider_request_id, limit=200
        )
        self.retry_after_s = (
            max(0.0, float(retry_after_s)) if retry_after_s is not None else None
        )
        self.side_effects_possible = bool(side_effects_possible)
        self.details = dict(details or {})
        self.attempts = max(1, int(attempts))

    @property
    def error_code(self) -> str:
        return self.code.value if isinstance(self.code, ProviderFailureCode) else str(self.code)

    def audit_payload(self) -> dict[str, Any]:
        return {
            "error_code": self.error_code,
            "error_type": type(self).__name__,
            "error": sanitise_provider_error(self),
            "human_description": self.human_description,
            "retryable": self.retryable,
            "http_status": self.http_status,
            "provider_request_id": self.provider_request_id or None,
            "retry_after_s": self.retry_after_s,
            "side_effects_possible": self.side_effects_possible,
            "attempts": self.attempts,
            "details": dict(self.details),
        }

    def terminal_copy(
        self,
        message: str,
        *,
        attempts: int,
        code: ProviderFailureCode | None = None,
        human_description: str = "",
        side_effects_possible: bool | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> "StageInvocationError":
        merged_details = dict(self.details)
        merged_details.update(dict(details or {}))
        return StageInvocationError(
            message,
            retryable=False,
            code=code or self.code,
            human_description=human_description or self.human_description,
            http_status=self.http_status,
            provider_request_id=self.provider_request_id,
            retry_after_s=self.retry_after_s,
            side_effects_possible=(
                self.side_effects_possible
                if side_effects_possible is None
                else side_effects_possible
            ),
            details=merged_details,
            attempts=attempts,
        )


class StructuredOutputError(StageInvocationError):
    def __init__(self, message: str, *, retryable: bool = True, **kwargs: Any):
        super().__init__(
            message,
            retryable=retryable,
            code=ProviderFailureCode.STRUCTURED_OUTPUT_INVALID,
            human_description=(
                "The provider returned an invalid structured response envelope."
            ),
            **kwargs,
        )


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

    async def deliver_activity(
        self,
        *,
        kind: str,
        text: str,
        event_id: str,
        phase: str,
        metadata: Mapping[str, Any],
    ) -> bool: ...


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
    activity_records: list[dict[str, Any]] = field(default_factory=list)

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

    async def deliver_packaged_commentary(self, commentary: Any) -> bool:
        event_id = str(getattr(commentary, "source_event_id", "") or "")
        text = str(getattr(commentary, "text", "") or "")
        if not event_id or not text:
            return False
        kind = "draft" if getattr(commentary, "draft_response", False) else "commentary"
        if any(item.event_id == event_id for item in self.records):
            return True
        self.records.append(DeliveryRecord(kind=kind, text=text, event_id=event_id))
        return True

    async def deliver_activity(
        self,
        *,
        kind: str,
        text: str,
        event_id: str,
        phase: str,
        metadata: Mapping[str, Any],
    ) -> bool:
        if any(item.get("event_id") == event_id for item in self.activity_records):
            return True
        self.activity_records.append(
            {
                "kind": kind,
                "text": text,
                "event_id": event_id,
                "phase": phase,
                "metadata": dict(metadata),
            }
        )
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
            kind = resolution
            if resolution == "commentary" and self.records[index].kind != "draft":
                kind = "acknowledgement"
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
