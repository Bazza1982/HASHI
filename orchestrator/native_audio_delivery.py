"""Terminal projection for provider-neutral native audio output parts."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping, Sequence


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


async def dispatch_persisted_audio_event(
    runtime: Any,
    item: Any,
    *,
    source_event_id: str,
    purpose: str,
    include_text: bool,
) -> tuple[bool, bool]:
    """Claim a persisted audio Event and deliver it through the FC adapter.

    The first boolean says the Event is FC-managed.  Once managed, callers
    must never fall back to an untracked second transport attempt.
    """

    store = getattr(runtime, "session_store", None)
    session_id = str(getattr(item, "session_id", "") or "").strip()
    owner_id = str(getattr(item, "owner_id", "") or "").strip()
    request_id = str(getattr(item, "request_id", "") or "").strip()
    if store is None or not session_id or not owner_id or not request_id:
        return False, False
    target_resolver = getattr(store, "runtime_event_delivery_target", None)
    if not callable(target_resolver):
        return False, False
    target = target_resolver(
        source_event_id=str(source_event_id),
        request_id=request_id,
        owner_id=owner_id,
    )
    if not isinstance(target, Mapping):
        return False, False

    from orchestrator.frontend_connector_registry import endpoint_id_for

    endpoint_id = endpoint_id_for(
        "telegram",
        ingress_transport="telegram",
        channel_key=str(getattr(item, "chat_id", "")),
    )
    claims = store.claim_delivery_outbox(
        session_id=str(target["session_id"]),
        owner_id=owner_id,
        worker_id=(
            f"fc-telegram-audio-{getattr(runtime, 'name', 'agent')}-"
            f"{str(source_event_id)[:48]}"
        ),
        event_id=str(target["event_id"]),
        connector_id="telegram",
        endpoint_id=endpoint_id,
        limit=1,
    )
    if not claims:
        receipts = store.frontend_delivery_receipts(
            session_id=str(target["session_id"]),
            owner_id=owner_id,
            event_id=str(target["event_id"]),
        )
        return True, any(
            str(receipt.get("status") or "") in {"accepted", "delivered"}
            for receipt in receipts
        )

    from orchestrator.runtime_delivery import dispatch_claimed_telegram_event

    _elapsed, units = await dispatch_claimed_telegram_event(
        runtime,
        chat_id=int(getattr(item, "chat_id")),
        store=store,
        claim=claims[0],
        frontend_owner_id=owner_id,
        request_id=request_id,
        purpose=purpose,
        include_text=include_text,
    )
    return True, units > 0


async def send_native_audio_parts(
    runtime: Any,
    item: Any,
    content: Sequence[Mapping[str, Any]] | None,
    *,
    purpose: str,
) -> bool:
    """Persist complete audio assets and dispatch the resulting FC Event once."""

    reply_policy = native_reply_content_policy(runtime, item)
    if reply_policy == "text_only":
        return False
    parts = claim_audio_parts(runtime, item, content)
    if not parts:
        return False
    store = getattr(runtime, "session_store", None)
    session_id = str(getattr(item, "session_id", "") or "")
    owner_id = str(getattr(item, "owner_id", "") or "")
    if store is None or not session_id or not owner_id:
        return False
    request_id = str(getattr(item, "request_id", "") or "").strip()
    if not request_id:
        return False
    normalized_content = [
        dict(part) for part in content or () if isinstance(part, Mapping)
    ]
    digest = hashlib.sha256(
        json.dumps(
            normalized_content,
            sort_keys=True,
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    source_event_id = f"fc-native-{purpose}-{digest[:32]}"
    event = store.append_native_audio_runtime_event(
        request_id=request_id,
        source_event_id=source_event_id,
        event_kind="assistant_native_audio",
        summary=text_projection(normalized_content) or "Assistant audio output",
        phase="final",
        content=normalized_content,
    )
    if event is None:
        logger = getattr(runtime, "logger", None)
        if logger is not None:
            logger.warning(
                "Native audio FC publication unavailable: request=%s purpose=%s",
                request_id,
                purpose,
            )
        return False
    managed, accepted = await dispatch_persisted_audio_event(
        runtime,
        item,
        source_event_id=source_event_id,
        purpose=purpose,
        include_text=reply_policy != "audio_only",
    )
    return bool(managed and accepted)


__all__ = [
    "audio_parts",
    "claim_audio_parts",
    "dispatch_persisted_audio_event",
    "native_reply_content_policy",
    "send_native_audio_parts",
    "text_projection",
]
