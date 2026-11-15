"""Functions-owned external connector lifecycle, separate from local activation."""
from __future__ import annotations

import asyncio
import hashlib
import logging
import random
import time
from typing import Any

from orchestrator.lifecycle_timing import span

logger = logging.getLogger("BridgeU.FunctionWorker")

TELEGRAM_CONNECT_RETRY_SECONDS = 5.0
TELEGRAM_CONNECT_RETRY_MAX_SECONDS = 300.0
TELEGRAM_CONNECT_RETRY_JITTER = 0.2


def _retry_delay(seconds: float) -> float:
    spread = max(0.0, float(seconds)) * TELEGRAM_CONNECT_RETRY_JITTER
    return max(0.0, random.uniform(float(seconds) - spread, float(seconds) + spread))


def _token_revision(runtime: Any) -> str:
    """Return a non-secret identity for the currently adopted Bot token."""

    token = str(getattr(runtime, "token", "") or "")
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]


def start_connector_task(host: Any) -> None:
    runtime = host._require_runtime()
    if not getattr(host, "connector_owner", False) or host.phase != "ACTIVE" or not host.accepting:
        return
    if runtime.telegram_connected:
        return
    if getattr(runtime, "_telegram_connector_permanent_failure", None):
        failed_revision = getattr(
            runtime, "_telegram_connector_failure_token_revision", None
        )
        if failed_revision == _token_revision(runtime):
            return
        # A new credential revision is the only event that re-arms a permanent
        # InvalidToken/Forbidden failure for this Worker.
        runtime._telegram_connector_permanent_failure = None
        runtime._telegram_connector_failure_token_revision = None
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
    delay = TELEGRAM_CONNECT_RETRY_SECONDS
    while (host.phase == "ACTIVE" and host.accepting
           and host.nonce == nonce and host.manifest.generation_id == generation
           and not host.stop_event.is_set()):
        try:
            runtime._telegram_connector_attempts = int(
                getattr(runtime, "_telegram_connector_attempts", 0) or 0
            ) + 1
            with span("worker.telegram_connect", agent=host.agent_name):
                connected = await asyncio.wait_for(host._prepare_telegram_application(), timeout=90)
            if connected:
                runtime._telegram_connector_permanent_failure = None
                runtime._telegram_connector_failure_token_revision = None
                runtime._telegram_delivery_connected_at = time.time()
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
            from orchestrator.telegram_ingress_diagnostics import safe_error

            runtime.telegram_connected = False
            failure = safe_error(exc, stage="bot_initialization")
            reason = str(failure.get("reason") or "unknown")
            permanent = reason in {"invalid_token", "forbidden"}
            if permanent:
                runtime._telegram_connector_permanent_failure = reason
                runtime._telegram_connector_failure_token_revision = (
                    _token_revision(runtime)
                )
            logger.warning(
                "Telegram unavailable for %s; local Worker remains ACTIVE "
                "(error_type=%s reason=%s retry_s=%s)",
                host.agent_name,
                type(exc).__name__,
                reason,
                "stopped" if permanent else f"{delay:.1f}",
            )
            try:
                await host.emit_metadata()
            except Exception:
                logger.warning("Telegram status publication deferred for %s", host.agent_name)
            if permanent:
                return
            wait_seconds = _retry_delay(delay)
            try:
                await asyncio.wait_for(host.stop_event.wait(), timeout=wait_seconds)
                return
            except TimeoutError:
                pass
            delay = min(TELEGRAM_CONNECT_RETRY_MAX_SECONDS, delay * 2)
# HASHI-Fingerprint: hfp_719abeca2f964d108a4fe50370aadac9
