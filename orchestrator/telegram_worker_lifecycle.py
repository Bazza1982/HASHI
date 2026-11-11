"""Functions-owned external connector lifecycle, separate from local activation."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from orchestrator.lifecycle_timing import span

logger = logging.getLogger("BridgeU.FunctionWorker")


def start_connector_task(host: Any) -> None:
    runtime = host._require_runtime()
    if not getattr(host, "connector_owner", False) or host.phase != "ACTIVE" or not host.accepting:
        return
    if runtime.telegram_connected:
        return
    task = getattr(host, "connector_task", None)
    if task is not None and not task.done():
        return
    host.connector_task = asyncio.create_task(
        _connect(host), name=f"telegram-connector:{host.agent_name}:{host.nonce[:8]}"
    )


async def cancel_connector_tasks(host: Any) -> None:
    tasks = [getattr(host, name, None) for name in ("connector_task", "command_menu_task")]
    for task in tasks:
        if task is not None:
            task.cancel()
    if any(task is not None for task in tasks):
        await asyncio.gather(*(task for task in tasks if task is not None), return_exceptions=True)
    host.connector_task = None
    host.command_menu_task = None


async def _connect(host: Any) -> None:
    runtime = host._require_runtime()
    # The capability is granted by the committed Functions route owner. No
    # candidate may restore cards, start background notices or consume ingress.
    nonce, generation = host.nonce, host.manifest.generation_id
    delay = 2.0
    while (host.phase == "ACTIVE" and host.accepting
           and host.nonce == nonce and host.manifest.generation_id == generation
           and not host.stop_event.is_set()):
        try:
            with span("worker.telegram_connect", agent=host.agent_name):
                connected = await asyncio.wait_for(host._prepare_telegram_application(), timeout=90)
            if connected:
                if host.phase != "ACTIVE" or not host.accepting:
                    runtime.telegram_connected = False
                    return
                try:
                    await host.emit_metadata()
                except Exception:
                    logger.warning("Telegram metadata publication deferred for %s", host.agent_name)
                return
            # A deliberately tokenless Agent has no connector to retry.
            return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            runtime.telegram_connected = False
            logger.warning(
                "Telegram unavailable for %s; local Worker remains ACTIVE (error_type=%s retry_s=%.1f)",
                host.agent_name, type(exc).__name__, delay,
            )
            try:
                await host.emit_metadata()
            except Exception:
                logger.warning("Telegram status publication deferred for %s", host.agent_name)
            await asyncio.sleep(delay)
            delay = min(30.0, delay * 2)
# HASHI-Fingerprint: hfp_719abeca2f964d108a4fe50370aadac9
