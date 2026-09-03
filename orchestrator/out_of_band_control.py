"""Independent Agent control lane for provider interruption.

The Telegram/runtime event loop owns orderly cleanup.  This module owns only
the minimal synchronous emergency signal so /stop, /steer, /focus, and /retry
can terminate local provider processes even while that loop is congested.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import queue
import threading
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ControlLaneResult:
    reason: str
    backend: str
    interrupted: int
    worker_thread_id: int


@dataclass
class _InterruptRequest:
    reason: str
    future: concurrent.futures.Future[ControlLaneResult]


class AgentControlLane:
    """One daemon thread per Agent, isolated from provider asyncio traffic."""

    def __init__(self, runtime: Any) -> None:
        self._runtime = runtime
        self._queue: queue.Queue[_InterruptRequest | None] = queue.Queue()
        self._closed = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name=f"hashi-control-{getattr(runtime, 'name', 'agent')}",
            daemon=True,
        )
        self._thread.start()

    async def interrupt(self, reason: str) -> ControlLaneResult:
        if self._closed.is_set():
            raise RuntimeError("Agent control lane is closed")
        future: concurrent.futures.Future[ControlLaneResult] = (
            concurrent.futures.Future()
        )
        self._queue.put(_InterruptRequest(str(reason or "USER_STOP"), future))
        return await asyncio.wrap_future(future)

    def close(self, timeout_s: float = 5.0) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        self._queue.put(None)
        self._thread.join(timeout=max(0.1, float(timeout_s)))
        if self._thread.is_alive():
            raise TimeoutError("Agent control lane did not stop")

    def _active_backend(self) -> Any:
        manager = getattr(self._runtime, "backend_manager", None)
        if manager is not None:
            return getattr(manager, "current_backend", None)
        return getattr(self._runtime, "backend", None)

    def _run(self) -> None:
        while True:
            request = self._queue.get()
            if request is None:
                return
            if request.future.cancelled():
                continue
            try:
                backend = self._active_backend()
                interrupt = getattr(backend, "interrupt_nowait", None)
                count = int(interrupt(request.reason) or 0) if callable(interrupt) else 0
                result = ControlLaneResult(
                    reason=request.reason,
                    backend=type(backend).__name__ if backend is not None else "none",
                    interrupted=count,
                    worker_thread_id=threading.get_ident(),
                )
            except Exception as exc:  # noqa: BLE001 - propagate backend failure
                request.future.set_exception(exc)
            else:
                request.future.set_result(result)
