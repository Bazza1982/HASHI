"""Stable Core ownership of Telegram long polling for one Agent route."""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Callable
from typing import Any

from telegram import Bot, Update

logger = logging.getLogger("BridgeU.TelegramIngress")
bridge_logger = logging.getLogger("BridgeU.Bridge")

TELEGRAM_LONG_POLL_SECONDS = 30
TELEGRAM_READ_TIMEOUT_SECONDS = 40
TELEGRAM_RETRY_SECONDS = 2.0


class CoreTelegramIngress:
    """Poll Telegram in Core and deliver JSON updates through a stable handle.

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
    ) -> None:
        self.agent_name = str(agent_name)
        self.token = str(token)
        self.handle_lookup = handle_lookup
        self.status_callback = status_callback
        self.bot = bot_factory(self.token)
        self.offset: int | None = None
        self.task: asyncio.Task[None] | None = None
        self.connected = False
        self._stopping = False

    @property
    def is_running(self) -> bool:
        return bool(self.task is not None and not self.task.done())

    async def start(self, *, drop_pending_updates: bool) -> None:
        if self.is_running:
            return
        self._stopping = False
        try:
            await self.bot.initialize()
            await self.bot.delete_webhook(
                drop_pending_updates=bool(drop_pending_updates)
            )
        except Exception:
            try:
                await self.bot.shutdown()
            except Exception:
                pass
            raise
        await self._set_connected(True)
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
            try:
                updates = await self.bot.get_updates(
                    offset=self.offset,
                    timeout=TELEGRAM_LONG_POLL_SECONDS,
                    read_timeout=TELEGRAM_READ_TIMEOUT_SECONDS,
                    allowed_updates=Update.ALL_TYPES,
                )
                await self._set_connected(True)
                for update in updates:
                    handle = self.handle_lookup(self.agent_name)
                    if handle is None:
                        raise RuntimeError(
                            f"Agent route {self.agent_name!r} is unavailable"
                        )
                    await handle.deliver_telegram_update(update.to_dict())
                    self.offset = int(update.update_id) + 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await self._set_connected(False)
                logger.warning(
                    "Telegram ingress retry for %s after %s: %s",
                    self.agent_name,
                    type(exc).__name__,
                    exc,
                )
                await asyncio.sleep(TELEGRAM_RETRY_SECONDS)

    async def stop(self) -> None:
        self._stopping = True
        task = self.task
        self.task = None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await self._set_connected(False)
        try:
            await self.bot.shutdown()
        except Exception as exc:
            logger.warning(
                "Telegram ingress shutdown warning for %s: %s",
                self.agent_name,
                exc,
            )
        bridge_logger.info(
            "Core Telegram ingress stopped: agent=%s",
            self.agent_name,
        )

    async def _set_connected(self, connected: bool) -> None:
        changed = self.connected != bool(connected)
        self.connected = bool(connected)
        if not changed or self.status_callback is None:
            return
        try:
            result = self.status_callback(self.connected)
            if inspect.isawaitable(result):
                await result
        except Exception as exc:
            logger.warning(
                "Telegram ingress status propagation failed for %s: %s",
                self.agent_name,
                exc,
            )
