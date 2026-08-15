"""Single presentation router for HASHI Engine Runtime stream events.

Raw persistence and local activity publication happen before this router makes
any display decision. Each event is then accepted by exactly one presentation
owner and tracked in a request-scoped delivery ledger.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import threading
import time
from collections import OrderedDict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from adapters.stream_events import (
    DELIVERY_CLASSES,
    DELIVERY_CONTROL,
    DELIVERY_FINAL,
    DELIVERY_INTERNAL,
    DELIVERY_REASONING,
    DELIVERY_TECHNICAL,
    DELIVERY_USER_COMMENTARY,
    StreamEvent,
)


Presenter = Callable[[StreamEvent], Awaitable[Any] | Any]
EnabledProbe = Callable[[], bool]
LedgerObserver = Callable[[dict[str, Any]], None]
_JOURNAL_LOCK = threading.Lock()


@dataclass
class HERDeliveryRecord:
    """Bounded evidence for one logical presentation decision."""

    event_id: str
    delivery_class: str
    purpose: str
    content_sha256: str
    status: str = "pending"
    attempts: int = 0
    created_at: float = 0.0
    updated_at: float = 0.0
    last_error: str = ""
    message_id: str = ""


class HERDeliveryJournal:
    """Append secret-free delivery transitions for restart-safe audit evidence."""

    def __init__(self, path: str | Path, *, request_id: str) -> None:
        self.path = Path(path)
        self.request_id = str(request_id or "unknown-request")

    def __call__(self, record: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "request_id": self.request_id,
            "recorded_at": time.time(),
            "delivery": record,
        }
        with _JOURNAL_LOCK:
            with self.path.open("a", encoding="utf-8") as journal:
                journal.write(
                    json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n"
                )


class HERDeliveryLedger:
    """Request-scoped event-id ledger with bounded retry semantics."""

    def __init__(
        self,
        request_id: str,
        *,
        max_attempts: int = 3,
        observer: LedgerObserver | None = None,
    ) -> None:
        self.request_id = str(request_id or "unknown-request")
        self.max_attempts = max(1, min(int(max_attempts), 5))
        self.observer = observer
        self._records: OrderedDict[str, HERDeliveryRecord] = OrderedDict()

    def _changed(self, record: HERDeliveryRecord) -> None:
        if self.observer is None:
            return
        try:
            self.observer(asdict(record))
        except Exception:
            # Delivery evidence is best effort and must not break presentation.
            return

    @staticmethod
    def _content_sha256(event: StreamEvent) -> str:
        content = f"{event.summary or ''}\0{event.detail or ''}"
        return hashlib.sha256(content.encode("utf-8", errors="replace")).hexdigest()

    def identity(self, event: StreamEvent) -> str:
        """Return the source identity or the documented legacy fingerprint."""

        explicit = str(getattr(event, "event_id", "") or "").strip()
        if explicit:
            return explicit
        delivery_class = str(getattr(event, "delivery_class", "") or "internal")
        phase = str(getattr(event, "phase", "") or "unspecified")
        revision = getattr(event, "revision", None)
        revision_label = "none" if revision is None else str(revision)
        digest = self._content_sha256(event)[:20]
        return (
            f"{self.request_id}:legacy:{delivery_class}:{event.kind}:"
            f"{phase}:{revision_label}:{digest}"
        )

    def _ensure(self, event: StreamEvent, *, purpose: str) -> HERDeliveryRecord:
        event_id = self.identity(event)
        record = self._records.get(event_id)
        if record is not None:
            self._records.move_to_end(event_id)
            return record
        now = time.time()
        record = HERDeliveryRecord(
            event_id=event_id,
            delivery_class=str(event.delivery_class or DELIVERY_INTERNAL),
            purpose=purpose,
            content_sha256=self._content_sha256(event),
            created_at=now,
            updated_at=now,
        )
        self._records[event_id] = record
        return record

    def begin(self, event: StreamEvent, *, purpose: str) -> HERDeliveryRecord | None:
        record = self._ensure(event, purpose=purpose)
        if record.status in {"sending", "delivered", "suppressed", "failed_final"}:
            return None
        if record.attempts >= self.max_attempts:
            record.status = "failed_final"
            record.updated_at = time.time()
            self._changed(record)
            return None
        record.status = "sending"
        record.attempts += 1
        record.updated_at = time.time()
        self._changed(record)
        return record

    def defer(self, event: StreamEvent, *, purpose: str) -> bool:
        event_id = self.identity(event)
        existing = self._records.get(event_id)
        if existing is not None and existing.status in {
            "pending",
            "sending",
            "delivered",
            "suppressed",
            "failed_final",
        }:
            return False
        record = self._ensure(event, purpose=purpose)
        record.status = "pending"
        record.updated_at = time.time()
        self._changed(record)
        return True

    def suppress(self, event: StreamEvent, *, purpose: str) -> None:
        record = self._ensure(event, purpose=purpose)
        if record.status == "delivered":
            return
        record.status = "suppressed"
        record.updated_at = time.time()
        self._changed(record)

    def delivered(self, record: HERDeliveryRecord, *, message_id: object = "") -> None:
        record.status = "delivered"
        record.last_error = ""
        if message_id is not None and str(message_id).strip():
            record.message_id = str(message_id).strip()[:240]
        record.updated_at = time.time()
        self._changed(record)

    def failed(self, record: HERDeliveryRecord, exc: BaseException) -> None:
        record.status = (
            "failed_retryable"
            if record.attempts < self.max_attempts
            else "failed_final"
        )
        record.last_error = type(exc).__name__
        record.updated_at = time.time()
        self._changed(record)

    def touch(self, record: HERDeliveryRecord) -> None:
        self._changed(record)

    def snapshot(self) -> list[dict[str, Any]]:
        return [asdict(record) for record in self._records.values()]


class HERMessageRouter:
    """Persist first, deduplicate, then dispatch to one HER presentation owner."""

    def __init__(
        self,
        *,
        request_id: str,
        logger: Any,
        technical_presenter: Presenter | None = None,
        reasoning_presenter: Presenter | None = None,
        commentary_presenter: Presenter | None = None,
        control_presenter: Presenter | None = None,
        verbose_enabled: EnabledProbe | None = None,
        think_enabled: EnabledProbe | None = None,
        commentary_enabled: EnabledProbe | None = None,
        persist_event: Presenter | None = None,
        ledger_observer: LedgerObserver | None = None,
        delivery_requested: bool = True,
        delivery_blocked: bool = False,
    ) -> None:
        self.request_id = str(request_id or "unknown-request")
        self.logger = logger
        self.technical_presenter = technical_presenter
        self.reasoning_presenter = reasoning_presenter
        self.commentary_presenter = commentary_presenter
        self.control_presenter = control_presenter
        self.verbose_enabled = verbose_enabled or (lambda: False)
        self.think_enabled = think_enabled or (lambda: False)
        self.commentary_enabled = commentary_enabled or (lambda: True)
        self.persist_event = persist_event
        self.delivery_requested = bool(delivery_requested)
        self.delivery_blocked = bool(delivery_blocked)
        self.ledger = HERDeliveryLedger(
            self.request_id,
            observer=ledger_observer,
        )
        self.deferred_final: StreamEvent | None = None

    async def _call(self, callback: Presenter | None, event: StreamEvent) -> Any:
        if callback is None:
            return None
        result = callback(event)
        if inspect.isawaitable(result):
            return await result
        return result

    def _log(self, level: str, message: str) -> None:
        callback = getattr(self.logger, level, None)
        if callable(callback):
            callback(message)

    async def _dispatch(
        self,
        event: StreamEvent,
        *,
        purpose: str,
        presenter: Presenter | None,
    ) -> None:
        record = self.ledger.begin(event, purpose=purpose)
        if record is None:
            self._log(
                "info",
                f"HER delivery duplicate suppressed: request={self.request_id} "
                f"event_id={self.ledger.identity(event)} purpose={purpose}",
            )
            return
        if presenter is None:
            self.ledger.suppress(event, purpose=purpose)
            return
        try:
            result = await self._call(presenter, event)
        except Exception as exc:
            self.ledger.failed(record, exc)
            self._log(
                "warning",
                f"HER delivery failed: request={self.request_id} "
                f"event_id={record.event_id} purpose={purpose} "
                f"error_type={type(exc).__name__}",
            )
            return
        self.ledger.delivered(
            record,
            message_id=getattr(result, "message_id", ""),
        )
        self._log(
            "info",
            f"HER delivery accepted: request={self.request_id} "
            f"event_id={record.event_id} purpose={purpose}",
        )

    async def route(self, event: StreamEvent) -> None:
        """Record every event, then hand it to exactly one presentation owner."""

        await self._call(self.persist_event, event)
        delivery_class = str(getattr(event, "delivery_class", "") or "")
        if delivery_class not in DELIVERY_CLASSES:
            self._log(
                "warning",
                f"HER stream event rejected from presentation: request={self.request_id} "
                f"kind={getattr(event, 'kind', '')} delivery_class={delivery_class or 'missing'}",
            )
            self.ledger.suppress(event, purpose="invalid_delivery_class")
            return

        if delivery_class == DELIVERY_INTERNAL:
            self.ledger.suppress(event, purpose="internal")
            return
        if delivery_class == DELIVERY_FINAL:
            if self.ledger.defer(event, purpose="response"):
                self.deferred_final = event
            return
        if delivery_class == DELIVERY_CONTROL:
            if not event.required or not self.delivery_requested:
                self.ledger.suppress(event, purpose="optional_control")
                return
            # Required control still enters the runtime sender while Telegram
            # failover is active so the existing outbox can accept it.
            await self._dispatch(
                event,
                purpose="control",
                presenter=self.control_presenter,
            )
            return
        if not self.delivery_requested or self.delivery_blocked:
            self.ledger.suppress(event, purpose=f"{delivery_class}_not_deliverable")
            return
        if delivery_class == DELIVERY_TECHNICAL:
            if not self.verbose_enabled():
                self.ledger.suppress(event, purpose="verbose_off")
                return
            await self._dispatch(
                event,
                purpose="technical",
                presenter=self.technical_presenter,
            )
            return
        if delivery_class == DELIVERY_REASONING:
            if not self.think_enabled():
                self.ledger.suppress(event, purpose="think_off")
                return
            await self._dispatch(
                event,
                purpose="reasoning",
                presenter=self.reasoning_presenter,
            )
            return
        if delivery_class == DELIVERY_USER_COMMENTARY:
            if not self.commentary_enabled():
                self.ledger.suppress(event, purpose="commentary_off")
                return
            await self._dispatch(
                event,
                purpose="task_commentary",
                presenter=self.commentary_presenter,
            )
            return

    def record_final_delivery(
        self,
        text: str,
        *,
        delivered: bool,
        error: str = "",
    ) -> None:
        """Join the mandatory post-generation final lane to this request ledger."""

        event = self.deferred_final or StreamEvent(
            kind="final",
            summary=str(text or ""),
            event_id=f"{self.request_id}:final",
            delivery_class=DELIVERY_FINAL,
            origin="runtime",
            phase="finalization",
            required=True,
        )
        record = self.ledger._ensure(event, purpose="response")
        record.content_sha256 = hashlib.sha256(
            str(text or "").encode("utf-8", errors="replace")
        ).hexdigest()
        record.attempts = max(1, record.attempts)
        record.updated_at = time.time()
        if delivered:
            self.ledger.delivered(record)
        else:
            record.status = "failed_retryable"
            record.last_error = (
                "final_delivery_failed" if error else "delivery_unconfirmed"
            )
            self.ledger.touch(record)
