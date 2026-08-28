"""Terminal projection for provider-neutral native audio output parts."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Mapping, Sequence
from uuid import uuid4

from telegram.error import TimedOut as TelegramTimedOut

from orchestrator.voice_synthesizer import convert_audio_to_ogg


def audio_parts(content: Sequence[Mapping[str, Any]] | None) -> tuple[dict[str, Any], ...]:
    return tuple(
        dict(part)
        for part in content or ()
        if isinstance(part, Mapping)
        and str(part.get("type") or "").casefold() == "audio"
        and str(part.get("asset_id") or "").strip()
    )


def text_projection(content: Sequence[Mapping[str, Any]] | None) -> str:
    return "\n".join(
        str(part.get("text") or "").strip()
        for part in content or ()
        if isinstance(part, Mapping)
        and str(part.get("type") or "").casefold() == "text"
        and str(part.get("text") or "").strip()
    ).strip()


def _terminal_id(item: Any) -> str:
    terminal = str(getattr(item, "session_surface", "") or "").strip()
    metadata = getattr(item, "request_metadata", None)
    if not terminal and isinstance(metadata, Mapping):
        terminal = str(metadata.get("session_surface") or "").strip()
    if not terminal:
        source = str(getattr(item, "source", "") or "").strip().casefold()
        terminal = "telegram" if source in {"voice", "audio"} else source
    return terminal.casefold()


def claim_audio_parts(runtime: Any, item: Any, content) -> tuple[dict[str, Any], ...]:
    parts = audio_parts(content)
    store = getattr(runtime, "session_store", None)
    session_id = str(getattr(item, "session_id", "") or "")
    owner_id = str(getattr(item, "owner_id", "") or "")
    if store is None or not session_id or not owner_id:
        return parts
    for part in parts:
        store.claim_output_audio_asset(
            session_id=session_id,
            owner_id=owner_id,
            request_id=str(getattr(item, "request_id", "") or ""),
            asset_id=str(part["asset_id"]),
        )
    return parts


def native_reply_content_policy(runtime: Any, item: Any = None) -> str:
    manager = getattr(runtime, "voice_manager", None)
    policy_resolver = getattr(manager, "native_policy_for_terminal", None)
    policy = (
        policy_resolver(_terminal_id(item))
        if callable(policy_resolver)
        else getattr(manager, "native_policy", None)
    )
    value = (
        policy.get("reply_content", "audio_and_text")
        if isinstance(policy, Mapping)
        else "audio_and_text"
    )
    metadata = getattr(item, "request_metadata", None)
    preferences = (
        metadata.get("response_preferences")
        if isinstance(metadata, Mapping)
        else None
    )
    if isinstance(preferences, Mapping):
        explicit = preferences.get("reply_content")
        if explicit is not None:
            value = explicit
        else:
            wants_audio = preferences.get("assistant_audio")
            if wants_audio is None:
                wants_audio = preferences.get("audio_for_voice_input")
            wants_text = preferences.get("assistant_text")
            if wants_audio is False and wants_text is not False:
                value = "text_only"
            elif wants_text is False and wants_audio is not False:
                value = "audio_only"
            elif wants_audio is True and wants_text is True:
                value = "audio_and_text"
    normalized = str(value or "audio_and_text").strip().casefold()
    return (
        normalized
        if normalized in {"audio_and_text", "audio_only", "text_only"}
        else "audio_and_text"
    )


async def send_native_audio_parts(
    runtime: Any,
    item: Any,
    content: Sequence[Mapping[str, Any]] | None,
    *,
    purpose: str,
) -> bool:
    """Claim and send complete audio assets once; never stream raw deltas."""

    if native_reply_content_policy(runtime, item) == "text_only":
        return False
    parts = claim_audio_parts(runtime, item, content)
    if not parts:
        return False
    store = getattr(runtime, "session_store", None)
    session_id = str(getattr(item, "session_id", "") or "")
    owner_id = str(getattr(item, "owner_id", "") or "")
    if store is None or not session_id or not owner_id:
        return False
    sent_any = False
    for part in parts:
        asset_id = str(part["asset_id"])
        derivative: Path | None = None
        store.acquire_audio_asset(
            session_id=session_id, owner_id=owner_id, asset_id=asset_id
        )
        try:
            metadata, source_path = store.audio_asset_path(
                session_id=session_id,
                owner_id=owner_id,
                asset_id=asset_id,
            )
            audio_format = str(metadata.get("format") or "").casefold()
            upload_path = source_path
            if audio_format not in {"ogg", "opus", "mp3", "m4a"}:
                derivative = Path(runtime.media_dir) / (
                    f"native_reply_{uuid4().hex}.ogg"
                )
                await convert_audio_to_ogg(
                    str(getattr(runtime.voice_manager, "ffmpeg_cmd", "ffmpeg")),
                    source_path,
                    derivative,
                )
                upload_path = derivative
            with upload_path.open("rb") as handle:
                await runtime.app.bot.send_voice(
                    chat_id=item.chat_id,
                    voice=handle,
                    read_timeout=30,
                    write_timeout=30,
                    connect_timeout=15,
                )
            sent_any = True
            logger = getattr(runtime, "telegram_logger", None)
            if logger is not None:
                logger.info(
                    "Sent native audio asset %s for request_id=%s purpose=%s",
                    asset_id,
                    item.request_id,
                    purpose,
                )
        except TelegramTimedOut:
            # Delivery may have reached Telegram; retrying could duplicate it.
            raise
        finally:
            if derivative is not None:
                derivative.unlink(missing_ok=True)
            try:
                store.release_audio_asset(
                    session_id=session_id,
                    owner_id=owner_id,
                    asset_id=asset_id,
                )
            except Exception as exc:
                logger = getattr(runtime, "logger", None)
                if logger is not None:
                    logger.warning(
                        "Native audio lease release failed: asset=%s error_type=%s",
                        asset_id,
                        type(exc).__name__,
                    )
        await asyncio.sleep(0)
    return sent_any


__all__ = [
    "audio_parts",
    "claim_audio_parts",
    "native_reply_content_policy",
    "send_native_audio_parts",
    "text_projection",
]
