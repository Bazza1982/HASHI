"""Shared Functions ownership of Telegram polling for one stable Agent route."""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Callable
from typing import Any
from pathlib import Path

from orchestrator.telegram_ingress_diagnostics import TelegramIngressDiagnostics

from telegram import Bot, Update

logger = logging.getLogger("BridgeU.TelegramIngress")
bridge_logger = logging.getLogger("BridgeU.Bridge")

TELEGRAM_LONG_POLL_SECONDS = 30
TELEGRAM_READ_TIMEOUT_SECONDS = 40
TELEGRAM_POLL_WATCHDOG_SECONDS = TELEGRAM_READ_TIMEOUT_SECONDS + 5
TELEGRAM_RETRY_SECONDS = 2.0


class TelegramIngressInitializationError(RuntimeError):
    """Safe initialization failure; detailed facts stay in the private sink."""


class CoreTelegramIngress:
    """Poll in shared Functions and deliver JSON updates through a stable handle.

    The historical class name is retained for source compatibility.
    The handle lookup happens for every update, so an atomic Function Worker
    pointer swap changes the recipient without restarting this transport.
    Offset advances only after the selected Worker accepts the update.
    """

    def __init__(
        self,
        *,
        agent_name: str,
        token: str,
        handle_lookup: Callable[[str], Any | None],
        status_callback: Callable[[bool], Any] | None = None,
        bot_factory: Callable[[str], Any] = Bot,
        checkpoint_callback: Callable[[int], Any] | None = None,
        diagnostic_home: Path | None = None,
        instance_id: str = "unknown",
        generation_id: str = "unknown",
        diagnostics: TelegramIngressDiagnostics | None = None,
    ) -> None:
        self.agent_name = str(agent_name)
        self.token = str(token)
        self.handle_lookup = handle_lookup
        self.status_callback = status_callback
        self.checkpoint_callback = checkpoint_callback
        self._status_propagation_failed = False
        self.diagnostics = diagnostics or TelegramIngressDiagnostics(bridge_home=diagnostic_home,
            instance_id=instance_id, agent=self.agent_name,
            generation_id=generation_id, token=self.token)
        self.diagnostics.begin("bot_initialization")
        try:
            self.bot = bot_factory(self.token)
        except Exception as exc:
            self.diagnostics.failure(exc, stage="bot_initialization", retry_seconds=None)
            raise TelegramIngressInitializationError(type(exc).__name__) from None
        self.offset: int | None = None
        self.task: asyncio.Task[None] | None = None
        self.connected = False
        self._stopping = False
        self._poll_task: asyncio.Task[Any] | None = None

    @property
    def is_running(self) -> bool:
        return bool(self.task is not None and not self.task.done())

    async def start(self, *, drop_pending_updates: bool) -> None:
        if self.is_running:
            return
        self._stopping = False
        stage = "bot_initialization"
        self.diagnostics.begin(stage)
        try:
            await self.bot.initialize()
            stage = "webhook_initialization"
            self.diagnostics.stage(stage)
            await self.bot.delete_webhook(
                drop_pending_updates=bool(drop_pending_updates)
            )
        except Exception as exc:
            self.diagnostics.failure(exc, stage=stage, retry_seconds=None)
            try:
                await self.bot.shutdown()
            except Exception:
                pass
            raise TelegramIngressInitializationError(type(exc).__name__) from None
        self.task = asyncio.create_task(
            self._run(),
            name=f"core-telegram-ingress:{self.agent_name}",
        )
        bridge_logger.info(
            "Core Telegram ingress started: agent=%s drop_pending=%s",
            self.agent_name,
            bool(drop_pending_updates),
        )

    async def _run(self) -> None:
        while not self._stopping:
            stage = "route_lookup"
            try:
                handle = self.handle_lookup(self.agent_name)
                if handle is not None and getattr(handle, "route_is_gated", False):
                    await asyncio.sleep(0.05)
                    continue
                stage = "get_updates"
                self.diagnostics.begin(stage, deadline=TELEGRAM_POLL_WATCHDOG_SECONDS)
                poll = asyncio.create_task(
                    self.bot.get_updates(
                        offset=self.offset,
                        timeout=TELEGRAM_LONG_POLL_SECONDS,
                        read_timeout=TELEGRAM_READ_TIMEOUT_SECONDS,
                        allowed_updates=Update.ALL_TYPES,
                    )
                )
                self._poll_task = poll
                try:
                    updates = await asyncio.wait_for(
                        poll, timeout=TELEGRAM_POLL_WATCHDOG_SECONDS
                    )
                except TimeoutError:
                    stage = "watchdog"
                    raise
                except asyncio.CancelledError:
                    if self._stopping and poll.cancelled():
                        break
                    raise
                finally:
                    self._poll_task = None
                self.diagnostics.poll_succeeded()
                status_ok = await self._set_connected(True)
                for update in updates:
                    if self._stopping:
                        break
                    handle = self.handle_lookup(self.agent_name)
                    if handle is None:
                        raise RuntimeError(
                            f"Agent route {self.agent_name!r} is unavailable"
                        )
                    if getattr(handle, "route_is_gated", False):
                        break
                    stage = "worker_delivery"
                    self.diagnostics.stage(stage)
                    await handle.deliver_telegram_update(update.to_dict())
                    self.offset = int(update.update_id) + 1
                    if self.checkpoint_callback is not None:
                        stage = "offset_checkpoint"
                        self.diagnostics.stage(stage)
                        self.checkpoint_callback(self.offset)
                if status_ok and not self._stopping:
                    self.diagnostics.recovered()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.diagnostics.failure(exc, stage=stage, retry_seconds=TELEGRAM_RETRY_SECONDS)
                await self._set_connected(False)
                if not self._stopping:
                    await asyncio.sleep(TELEGRAM_RETRY_SECONDS)

    async def pause(self) -> None:
        """Finish the accepted batch and retain its offset before handoff.

        Only an idle getUpdates request is cancelled. An accepted batch still
        drains and checkpoints before this method returns.
        """
        self._stopping = True
        if self._poll_task is not None and not self._poll_task.done():
            self._poll_task.cancel()
        if self.task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(self.task), timeout=45)
            except BaseException:
                self._stopping = False
                raise
        await self.stop(notify_status=False)

    async def stop(self, *, notify_status: bool = True) -> None:
        self._stopping = True
        task = self.task
        self.task = None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.diagnostics.stopped()
        await self._set_connected(False, notify_status=notify_status)
        try:
            await self.bot.shutdown()
        except Exception as exc:
            self.diagnostics.failure(exc, stage="bot_shutdown", retry_seconds=None)
        bridge_logger.info(
            "Core Telegram ingress stopped: agent=%s",
            self.agent_name,
        )

    async def _set_connected(
        self,
        connected: bool,
        *,
        notify_status: bool = True,
    ) -> bool:
        changed = self.connected != bool(connected)
        self.connected = bool(connected)
        if not changed or not notify_status or self.status_callback is None:
            return not self._status_propagation_failed
        try:
            result = self.status_callback(self.connected)
            if inspect.isawaitable(result):
                result = await result
            if result is False:
                self._status_propagation_failed = True
                return False
        except Exception as exc:
            self.diagnostics.failure(exc, stage="status_propagation", retry_seconds=None)


    def diagnostic_snapshot(self) -> dict[str, Any]:
        return self.diagnostics.snapshot()

    def read_diagnostics(self, *, limit: int = 20) -> dict[str, Any]:
        return self.diagnostics.read(limit=limit)
