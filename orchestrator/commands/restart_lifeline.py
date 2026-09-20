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
    request_source: str,
) -> None:
    target = str(provider.get("target_instance") or "HASHI").upper()
    try:
        try:
            code, payload = await asyncio.to_thread(
                restart_via_provider,
                provider,
                reason=reason,
                requester_agent=str(getattr(runtime, "name", "") or ""),
                request_source=request_source,
                timeout=25,
            )
        except Exception as exc:
            logger.warning("Trusted Remote restart request for %s failed: %s", target, exc)
            if chat_id is not None:
                await runtime._send_text(
                    chat_id,
                    ui_language.tr("api.restart.failed", reason=str(exc)),
                )
            return
        if code != 0:
            detail = payload.get("error") or payload.get("detail") or "remote error"
            logger.warning("Trusted Remote restart rejected for %s: %s", target, detail)
            if chat_id is not None:
                await runtime._send_text(
                    chat_id,
                    ui_language.tr("api.restart.failed", reason=str(detail)),
                )
            return
        verified, detail = legacy_restart._verified_restart_receipt(
            payload,
            expected_target=target,
        )
        if not verified:
            logger.warning(
                "Trusted Remote returned an unverified restart result for %s: %s",
                target,
                detail,
            )
            if chat_id is not None:
                await runtime._send_text(
                    chat_id,
                    ui_language.tr("api.restart.failed", reason=str(detail)),
                )
            return
        if chat_id is not None:
            await runtime._send_text(
                chat_id,
                legacy_restart._restart_completed_text(payload),
            )
    finally:
        setattr(runtime, _REMOTE_RESTART_INFLIGHT_ATTR, False)


async def restart_command(runtime: Any, update: Any, context: Any) -> None:
    """Cold restart self or a trusted peer through a surviving Hashi Remote.

    `/restart` restarts the local instance. `/restart INSTANCE` restarts a peer.
    Peer restart is only exposed after the local Remote records an accepted
    handshake and the running target still advertises rescue_restart. Child or
    supervised mode is diagnostic, not an authorization gate. The provider is
    revalidated immediately before the POST.
    """

    if not legacy_restart._authorized(runtime, update):
        return
    if getattr(runtime, _REMOTE_RESTART_INFLIGHT_ATTR, False) or getattr(
        runtime, legacy_restart._RESTART_INFLIGHT_ATTR, False
    ):
        await runtime._reply_text(update, ui_language.tr("api.restart.in_progress"))
        return

    requested_target = _target_argument(context)
    local_instance = _local_instance(runtime)
    hashi_root = legacy_restart._hashi_root(runtime)
    target = requested_target or local_instance
    request_source = legacy_restart._restart_request_source(context)

    try:
        if target == local_instance:
            provider = await asyncio.to_thread(
                local_restart_provider,
                instance_id=local_instance,
                hashi_root=hashi_root,
            )
        else:
            provider = await asyncio.to_thread(
                peer_restart_provider,
                target,
                source_instance=local_instance,
                hashi_root=hashi_root,
            )
    except RestartProviderError as exc:
        if target != local_instance:
            await runtime._reply_text(
                update,
                ui_language.tr("api.restart.failed", reason=str(exc)),
            )
            return
        logger.info("Local Remote restart provider unavailable: %s", exc)
        await runtime._reply_text(
            update,
            ui_language.tr("api.restart.remote_unavailable"),
        )
        return
    except Exception as exc:
        if target != local_instance:
            await runtime._reply_text(
                update,
                ui_language.tr("api.restart.failed", reason=str(exc)),
            )
            return
        logger.info("Local Remote restart probe failed: %s", exc)
        await runtime._reply_text(
            update,
            ui_language.tr("api.restart.remote_unavailable"),
        )
        return

    setattr(runtime, _REMOTE_RESTART_INFLIGHT_ATTR, True)
    chat_id = getattr(getattr(update, "effective_chat", None), "id", None)
    await runtime._reply_text(
        update,
        ui_language.tr("api.restart.requested", instance=target),
    )
    reason = f"{request_source} /restart {target} via trusted Hashi Remote"
    asyncio.create_task(
        _dispatch_remote_restart(
            runtime,
            chat_id,
            provider,
            reason=reason,
            request_source=request_source,
        )
    )


COMMANDS = [
    RuntimeCommand(
        name="restart",
        description="Hard restart this HASHI or a trusted peer [INSTANCE]",
        callback=restart_command,
    )
]
