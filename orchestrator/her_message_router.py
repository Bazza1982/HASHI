"""Direct presentation router for HASHI Engine Runtime stream events.

Persistence is best effort and presentation is handled once by the owner of the
event's delivery class.  Transport reliability belongs to the runtime sender;
this router intentionally does not maintain a second request-scoped ledger.
"""

from __future__ import annotations

import inspect
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


class HERMessageRouter:
    """Persist best-effort, then route each event to its presentation owner."""

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
        if presenter is None:
            return
        try:
            accepted = await self._call(presenter, event)
        except Exception as exc:
            self._log(
                "warning",
                f"HER delivery failed: request={self.request_id} "
                f"event_id={getattr(event, 'event_id', '')} purpose={purpose} "
                f"error_type={type(exc).__name__}",
            )
            return
        if accepted is False:
            self._log(
                "warning",
                f"HER delivery not accepted: request={self.request_id} "
                f"event_id={getattr(event, 'event_id', '')} purpose={purpose}",
            )
            return
        self._log(
            "info",
            f"HER delivery accepted: request={self.request_id} "
            f"event_id={getattr(event, 'event_id', '')} purpose={purpose}",
        )

    async def route(self, event: StreamEvent) -> None:
        """Hand one stream event to exactly one presentation owner."""

        try:
            await self._call(self.persist_event, event)
        except Exception as exc:
            self._log(
                "warning",
                f"HER stream persistence failed safely: request={self.request_id} "
                f"kind={getattr(event, 'kind', '')} error_type={type(exc).__name__}",
            )

        delivery_class = str(getattr(event, "delivery_class", "") or "")
        if delivery_class not in DELIVERY_CLASSES:
            self._log(
                "warning",
                f"HER stream event rejected from presentation: request={self.request_id} "
                f"kind={getattr(event, 'kind', '')} delivery_class={delivery_class or 'missing'}",
            )
            return
        if delivery_class == DELIVERY_INTERNAL:
            return
        if delivery_class == DELIVERY_FINAL:
            self.deferred_final = event
            return
        if delivery_class == DELIVERY_CONTROL:
            if event.required and self.delivery_requested:
                await self._dispatch(
                    event,
                    purpose="control",
                    presenter=self.control_presenter,
                )
            return
        if not self.delivery_requested or self.delivery_blocked:
            return
        if delivery_class == DELIVERY_TECHNICAL:
            if self.verbose_enabled():
                await self._dispatch(
                    event,
                    purpose="technical",
                    presenter=self.technical_presenter,
                )
            return
        if delivery_class == DELIVERY_REASONING:
            if self.think_enabled():
                await self._dispatch(
                    event,
                    purpose="reasoning",
                    presenter=self.reasoning_presenter,
                )
            return
        if delivery_class == DELIVERY_USER_COMMENTARY and self.commentary_enabled():
            await self._dispatch(
                event,
                purpose="task_commentary",
                presenter=self.commentary_presenter,
            )
