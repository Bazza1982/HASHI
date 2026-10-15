"""HChat ingress freshness gate and confirmation management.

Functional owner: PAO / Functions layer
Engineering layer: Functions
Decision: Pre-admission gate for HChat messages across LAN protocol, Remote /hchat,
          Exchange ingress, and local Workbench API.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from orchestrator.message_freshness import (
    classify_freshness,
)
from orchestrator.ingress_confirmation import (
    IngressConfirmationService,
)

logger = logging.getLogger("HASHI.HChatFreshnessGate")

_HCHAT_OUTER_ENVELOPE = re.compile(
    r"^\[hchat from [^\]]+\]\s*(?P<body>.*)$", re.IGNORECASE | re.DOTALL
)
_HCHAT_AUTOREPLY_PREFIX = "HChat protocol note:"
_HCHAT_TERMINAL_PREFIX = "\n\nHChat terminal reply notice:"


def canonical_hchat_message_text(value: str | None) -> str:
    """Remove transport-only HChat instructions from one logical message body."""

    text = str(value or "").strip()
    envelope = _HCHAT_OUTER_ENVELOPE.match(text)
    if envelope is not None:
        text = str(envelope.group("body") or "").strip()
    if text.startswith(_HCHAT_AUTOREPLY_PREFIX):
        separator = text.find("\n\n")
        if separator >= 0:
            text = text[separator + 2 :].strip()
    terminal = text.find(_HCHAT_TERMINAL_PREFIX)
    if terminal >= 0:
        text = text[:terminal].rstrip()
    return text


def _resolve_hchat_owner(
    store: Any,
    *,
    to_agent: str,
    preferred_owner_id: str | None = None,
) -> str:
    """Resolve the owner whose primary conversation may show the decision card.

    Authenticated ingress may supply an already verified owner.  Network HChat
    transports do not carry a user identity, so they may bind only when the
    target Agent has exactly one authoritative conversation owner.  Ambiguity
    fails closed instead of exposing one owner's message to another owner.
    """

    preferred = str(preferred_owner_id or "").strip()
    if preferred:
        return preferred
    owners = [
        str(value).strip()
        for value in store.conversation_owner_ids(agent_id=to_agent)
        if str(value).strip()
    ]
    if len(owners) == 1:
        return owners[0]
    if not owners:
        raise ValueError(
            f"Cannot create HChat confirmation for {to_agent!r}: no conversation owner exists"
        )
    raise ValueError(
        f"Cannot create HChat confirmation for {to_agent!r}: conversation owner is ambiguous"
    )


def format_delay_display(delay_seconds: float) -> str:
    """Format delay seconds into plain Chinese string."""
    seconds = int(round(delay_seconds))
    if seconds < 60:
        return f"{seconds} 秒"
    minutes = seconds // 60
    rem_seconds = seconds % 60
    if rem_seconds > 0:
        return f"{minutes} 分 {rem_seconds} 秒"
    return f"{minutes} 分钟"


def is_hchat_terminal_reply(message_type: str | None, text: str | None) -> bool:
    """Determine whether an HChat message is a terminal reply or a request."""
    if message_type and str(message_type).strip().lower() == "agent_reply":
        return True
    norm_text = (text or "").lstrip().casefold()
    if norm_text.startswith("[hchat reply from "):
        return True
    return False


def check_hchat_freshness_and_register(
    *,
    store: Any,
    originated_at: str | datetime | None,
    received_at: datetime | None = None,
    is_remote: bool = True,
    to_agent: str,
    from_agent: str,
    from_instance: str,
    message_id: str,
    text: str,
    is_reply: bool,
    transport: str,
    connector_id: str = "hchat",
    owner_id: str | None = None,
    idempotency_key: str | None = None,
    payload_to_execute: dict[str, Any] | None = None,
    canonical_text: str | None = None,
    attachment_binding: Any = None,
    clock: Callable[[], float | datetime] | None = None,
) -> tuple[bool, dict[str, Any] | None]:
    """Check freshness of an incoming HChat message.

    Returns:
        (True, None) if the message is fresh.
        (False, confirmation_record) if the message is stale/missing and registered.
    """
    now_dt = received_at or datetime.now(timezone.utc)
    freshness = classify_freshness(
        entrypoint="hchat",
        originated_at=originated_at,
        is_remote=is_remote,
        received_at=now_dt,
        clock=clock,
    )

    if freshness.is_fresh:
        return True, None

    # Stale or invalid -> Register confirmation
    delay_str = format_delay_display(freshness.delay_seconds)
    orig_iso = freshness.originated_at_iso or "未知"
    rec_iso = freshness.received_at_iso
    exp_iso = freshness.expires_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    preview = (text or "")[:120].strip()
    action_label = "仍然显示" if is_reply else "仍然执行"
    message_kind = "hchat_reply" if is_reply else "hchat_request"

    if is_reply:
        card_text = (
            f"⚠️ 这条回复似乎延迟了 {delay_str}。为避免过期的回复干扰当前对话，已暂停显示。是否仍然显示？\n\n"
            f"📝 原内容：{preview}\n"
            f"👤 发送方：{from_agent}@{from_instance}\n"
            f"⏰ 发起时间：{orig_iso}\n"
            f"📥 到达时间：{rec_iso}\n"
            f"⏳ 确认有效期：15 分钟（至 {exp_iso}）"
        )
    else:
        card_text = (
            f"⚠️ 这条消息似乎延迟了 {delay_str}。为避免旧指令被误执行，消息已暂停。是否仍然执行？\n\n"
            f"📝 原内容：{preview}\n"
            f"👤 发送方：{from_agent}@{from_instance}\n"
            f"⏰ 发起时间：{orig_iso}\n"
            f"📥 到达时间：{rec_iso}\n"
            f"⏳ 确认有效期：15 分钟（至 {exp_iso}）"
        )

    resolved_owner = _resolve_hchat_owner(
        store, to_agent=to_agent, preferred_owner_id=owner_id
    )
    session = store.resolve_primary_session(
        owner_id=resolved_owner, agent_id=to_agent, establish=True
    )
    session_id = session["session_id"]
    context_gen = session.get("context_generation", 1)

    stable_idempotency_key = str(idempotency_key or f"hchat:{message_id}")
    # Every HChat transport belongs to one logical connector family.  A retry
    # that falls back from protocol to Workbench/Remote/Exchange must resolve
    # to the same immutable confirmation rather than create a second card and
    # a second executable decision.
    source_message_id = f"{from_instance}:{from_agent}:{message_id}"

    saved_payload = dict(payload_to_execute or {})
    saved_payload.update({
        "handler_kind": "hchat",
        "to_agent": to_agent,
        "from_agent": from_agent,
        "from_instance": from_instance,
        "message_id": message_id,
        "text": text,
        "is_reply": is_reply,
        "transport": transport,
        "connector_id": connector_id,
        "owner_id": resolved_owner,
        "session_id": session_id,
        "idempotency_key": stable_idempotency_key,
    })

    digest_payload = {
        "version": 1,
        "message_id": str(message_id),
        "originated_at": freshness.originated_at_iso
        or str(originated_at or ""),
        "from_agent": str(from_agent).strip().casefold(),
        "from_instance": str(from_instance).strip().upper(),
        "to_agent": str(to_agent).strip().casefold(),
        "is_reply": bool(is_reply),
        "text": canonical_hchat_message_text(
            canonical_text if canonical_text is not None else text
        ),
        "attachments": attachment_binding if attachment_binding is not None else [],
    }
    payload_digest = "sha256:" + hashlib.sha256(
        json.dumps(
            digest_payload,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()

    display_metadata = {
        "text": preview,
        "preview": preview,
        "from_agent": from_agent,
        "from_instance": from_instance,
        "to_agent": to_agent,
        "is_reply": is_reply,
        "action_label": action_label,
        "delay_str": delay_str,
        "card_text": card_text,
        "ingress_connector_id": connector_id,
    }

    service = IngressConfirmationService(store, clock=clock)
    conf = service.register_pending(
        session_id=session_id,
        owner_id=resolved_owner,
        agent_id=to_agent,
        connector_id="hchat",
        transport=transport,
        source_message_id=source_message_id,
        idempotency_key=stable_idempotency_key,
        payload_digest=payload_digest,
        message_kind=message_kind,
        context_generation=context_gen,
        originated_at=orig_iso if orig_iso != "未知" else None,
        received_at=rec_iso,
        threshold_seconds=freshness.threshold_seconds,
        reason=freshness.reason,
        delay_seconds=freshness.delay_seconds,
        expires_at=exp_iso,
        payload=saved_payload,
        display_metadata=display_metadata,
    )

    logger.info(
        "HChat %s from %s@%s delayed by %s (threshold %ss); registered confirmation %s",
        "reply" if is_reply else "message",
        from_agent,
        from_instance,
        delay_str,
        freshness.threshold_seconds,
        conf["confirmation_id"],
    )

    return False, conf


async def execute_confirmed_hchat(
    rec_dict: dict[str, Any],
    *,
    runtime_map_fn: Callable[[], dict[str, Any]],
    session_store: Any,
) -> str | None:
    """Release one immutable confirmed HChat request or terminal reply."""
    p = rec_dict.get("payload") or {}
    to_agent = str(p.get("to_agent") or "").strip()

    if bool(p.get("is_reply")):
        message = session_store.append_presentation_message(
            session_id=str(rec_dict.get("session_id") or p.get("session_id") or ""),
            owner_id=str(rec_dict.get("owner_id") or p.get("owner_id") or ""),
            agent_id=to_agent,
            role="assistant",
            text=str(p.get("text") or ""),
            source=str(p.get("source") or "hchat-reply"),
            idempotency_key=f"hchat-display:{p.get('message_id') or rec_dict['confirmation_id']}",
            presentation_channel="hchat-reply",
            history_eligible=False,
            message_context={
                "system_exchange": True,
                "system_exchange_kind": "reply",
                "system_exchange_terminal": True,
                "hchat_message_id": p.get("message_id"),
                "hchat_from_agent": p.get("from_agent"),
                "hchat_from_instance": p.get("from_instance"),
                "confirmation_id": rec_dict.get("confirmation_id"),
            },
        )
        if not message.get("message_id"):
            raise RuntimeError("Confirmed HChat reply was not persisted for display")
        return None

    runtime = runtime_map_fn().get(to_agent)
    if runtime is None:
        raise ValueError(f"Agent runtime {to_agent!r} not found for confirmed HChat execution")

    runtime_payload = p.get("runtime_payload")
    if not isinstance(runtime_payload, Mapping):
        runtime_payload = p
    text = str(runtime_payload.get("text") or p.get("prompt_text") or p.get("text") or "")
    source = str(runtime_payload.get("source") or p.get("source") or "hchat")
    request_metadata = dict(runtime_payload.get("request_metadata") or p.get("request_metadata") or {})
    idempotency_key = p.get("idempotency_key") or f"hchat-exec:{rec_dict['confirmation_id']}"

    request_id = await runtime.enqueue_api_text(
        text,
        source=source,
        deliver_to_telegram=True,
        request_metadata=request_metadata,
        request_content=runtime_payload.get("request_content"),
        idempotency_key=idempotency_key,
    )
    if not request_id:
        raise RuntimeError("Agent runtime did not accept confirmed HChat message")

    try:
        run = session_store.get_run_by_request(str(request_id))
        return str(run.get("run_id") or "") or None
    except Exception:
        return str(request_id)
