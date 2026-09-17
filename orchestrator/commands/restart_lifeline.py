from __future__ import annotations

import asyncio
import logging
from typing import Any

from orchestrator import ui_language
from orchestrator.command_registry import RuntimeCommand
from orchestrator.commands import api_restart as legacy_restart
from orchestrator.restart_provider import (
    RestartProviderError,
    local_restart_provider,
    peer_restart_provider,
    restart_via_provider,
)

logger = logging.getLogger("BridgeU.RuntimeCommands.RestartLifeline")
_REMOTE_RESTART_INFLIGHT_ATTR = "_remote_restart_inflight"


def _target_argument(context: Any) -> str | None:
    args = [
        str(item).strip()
        for item in (getattr(context, "args", []) or [])
        if str(item).strip()
    ]
    if not args:
        return None
    target = args[0]
    if target.startswith("@"):
        target = target[1:]
    return target.strip().upper() or None


def _local_instance(runtime: Any) -> str:
    return str(legacy_restart._instance_id(runtime) or "HASHI").strip().upper()


async def _dispatch_remote_restart(
    runtime: Any,
    chat_id: int | None,
    provider: dict[str, Any],
    *,
    reason: str,
) -> None:
    target = str(provider.get("target_instance") or "HASHI").upper()
    try:
        try:
            code, payload = await asyncio.to_thread(
                restart_via_provider,
                provider,
                reason=reason,
                timeout=15,
            )
        except Exception as exc:
            logger.warning("Trusted Remote restart request for %s failed: %s", target, exc)
            if chat_id is not None:
                await runtime._send_text(
                    chat_id,
                    f"Hard restart failed for {target}: {exc}",
                )
            return
        if code != 0:
            detail = payload.get("error") or payload.get("detail") or "remote error"
            logger.warning("Trusted Remote restart rejected for %s: %s", target, detail)
            if chat_id is not None:
                await runtime._send_text(
                    chat_id,
                    f"Hard restart was rejected for {target}: {detail}",
                )
    finally:
        setattr(runtime, _REMOTE_RESTART_INFLIGHT_ATTR, False)


async def _dispatch_watchtower_fallback(runtime: Any, update: Any) -> None:
    available, error, _payload = await legacy_restart._watchtower_restart_available()
    if not available:
        await runtime._reply_text(
            update,
            legacy_restart._restart_status_text(
                error=error or ui_language.tr("api.restart.watchtower_unavailable")
            ),
            parse_mode="HTML",
        )
        return
    try:
        request_payload = legacy_restart._build_watchtower_restart_payload(
            runtime,
            human_source="telegram",
            reason="telegram /restart hard restart (WatchTower fallback)",
        )
    except Exception as exc:
        logger.warning("Failed to build WatchTower fallback restart payload: %s", exc)
        await runtime._reply_text(
            update,
            ui_language.tr("api.restart.setup_error", reason=str(exc)),
        )
        return

    setattr(runtime, "_watchtower_restart_inflight", True)
    chat_id = getattr(getattr(update, "effective_chat", None), "id", None)
    await runtime._reply_text(
        update,
        "Local supervised Hashi Remote is unavailable; WatchTower fallback restart requested.",
    )
    asyncio.create_task(
        legacy_restart._dispatch_watchtower_restart(runtime, chat_id, request_payload)
    )


async def restart_command(runtime: Any, update: Any, context: Any) -> None:
    """Cold restart self or a trusted peer through a surviving Hashi Remote.

    `/restart` restarts the local instance. `/restart INSTANCE` restarts a peer.
    Peer restart is only exposed after the local Remote records an accepted
    handshake and the target still advertises rescue_restart from a supervised
    Remote. The provider is revalidated immediately before the POST.
    """

    if not legacy_restart._authorized(runtime, update):
        return
    if getattr(runtime, _REMOTE_RESTART_INFLIGHT_ATTR, False) or getattr(
        runtime, "_watchtower_restart_inflight", False
    ):
        await runtime._reply_text(update, ui_language.tr("api.restart.in_progress"))
        return

    requested_target = _target_argument(context)
    local_instance = _local_instance(runtime)
    target = requested_target or local_instance

    try:
        if target == local_instance:
            provider = await asyncio.to_thread(local_restart_provider)
        else:
            provider = await asyncio.to_thread(peer_restart_provider, target)
    except RestartProviderError as exc:
        if target != local_instance:
            await runtime._reply_text(
                update,
                f"Hard restart unavailable for {target}: {exc}",
            )
            return
        logger.info("Local Remote restart provider unavailable; trying WatchTower: %s", exc)
        await _dispatch_watchtower_fallback(runtime, update)
        return
    except Exception as exc:
        if target != local_instance:
            await runtime._reply_text(
                update,
                f"Hard restart unavailable for {target}: {exc}",
            )
            return
        logger.info("Local Remote restart probe failed; trying WatchTower: %s", exc)
        await _dispatch_watchtower_fallback(runtime, update)
        return

    setattr(runtime, _REMOTE_RESTART_INFLIGHT_ATTR, True)
    chat_id = getattr(getattr(update, "effective_chat", None), "id", None)
    provider_kind = str(provider.get("kind") or "remote")
    await runtime._reply_text(
        update,
        f"Hard restart requested for {target} via trusted Hashi Remote ({provider_kind}).",
    )
    reason = f"telegram /restart {target} via trusted Hashi Remote"
    asyncio.create_task(
        _dispatch_remote_restart(
            runtime,
            chat_id,
            provider,
            reason=reason,
        )
    )


COMMANDS = [
    RuntimeCommand(
        name="restart",
        description="Cold restart self or trusted HASHI peer [INSTANCE]",
        callback=restart_command,
    )
]
